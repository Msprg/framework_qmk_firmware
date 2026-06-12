// Copyright 2026 Msprg
// SPDX-License-Identifier: GPL-2.0-or-later
//
// System monitor keymap for the Framework 16 RGB Macropad.
//
// A host-side script (see host/fw16_sysmon.py) periodically sends system
// stats over the VIA raw HID endpoint, on a private channel that VIA
// itself never uses. Each of the six key rows becomes a 4-segment bar:
//
//   row 0 (top, small keys): battery level + charge/discharge effects
//   row 1: CPU usage
//   row 2: memory usage
//   row 3: iGPU usage
//   row 4: dGPU usage
//   row 5: system temperature
//
// Overall brightness is dictated by the host (it follows the laptop's
// screen brightness). When no stats arrive for SYSMON_TIMEOUT_MS the
// macropad falls back to the regular RGB matrix effect.

#include QMK_KEYBOARD_H
#include "factory.h"
#include "via.h"

const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
    // Same layout as the default keymap; see keymaps/default for diagrams
    [_NUMLOCK] = LAYOUT(
        KC_ESC,  KC_CALC, KC_EQL,  KC_BSPC,
        KC_NUM,  KC_PSLS, KC_PAST, KC_PMNS,
        KC_P7,   KC_P8,   KC_P9,   KC_PPLS,
        KC_P4,   KC_P5,   KC_P6,   KC_PPLS,
        KC_P1,   KC_P2,   KC_P3,   KC_PENT,
        KC_P0,   KC_P0,   KC_PDOT, KC_PENT
    ),
    [_FN] = LAYOUT(
        RGB_TOG, RGB_SPI, RGB_HUI, RGB_SAI,
        _______, RGB_SPD, RGB_HUD, RGB_SAD,
        _______, _______, _______, RGB_MOD,
        _______, _______, _______, RGB_RMOD,
        _______, _______, _______, BL_STEP,
        _______, _______, _______, BL_STEP
    ),
    [_FACTORY] = LAYOUT(
        KC_A,    KC_B,    KC_C,    KC_D,
        KC_E,    KC_F,    KC_G,    KC_H,
        KC_I,    KC_J,    KC_K,    KC_L,
        KC_M,    KC_N,    KC_O,    KC_P,
        KC_Q,    KC_R,    KC_S,    KC_T,
        KC_U,    KC_V,    KC_W,    KC_X
    ),
};

bool led_update_user(led_t led_state) {
    // Change layer if numlock state changes, either triggered by OS or
    // by numlock key on this keyboard
    if (led_state.num_lock) {
        layer_off(_FN);
    } else {
        layer_on(_FN);
    }
    return true;
}

void enable_factory_mode(bool enable) {
    if (enable)
        layer_on(_FACTORY);
    else
        layer_off(_FACTORY);
}

#if defined(RGB_MATRIX_ENABLE)

#include <lib/lib8tion/lib8tion.h>
#include "is31flcommon.h"

// ---------------------------------------------------------------------------
// Host -> macropad protocol
//
// The host sends a VIA "custom set value" packet:
//   data[0] = 0x07 (id_custom_set_value)
//   data[1] = SYSMON_CHANNEL
//   data[2] = SYSMON_VALUE_STATS
//   data[3] = flags  (SYSMON_FLAG_*)
//   data[4] = battery percentage 0-100
//   data[5] = CPU usage 0-100
//   data[6] = memory usage 0-100
//   data[7] = iGPU usage 0-100
//   data[8] = dGPU usage 0-100
//   data[9] = temperature in degrees C
//   data[10] = LED brightness 0-255 (host derives it from screen brightness)
// VIA echoes the buffer back, which serves as the ACK.
// ---------------------------------------------------------------------------

#define SYSMON_CHANNEL 0x42
#define SYSMON_VALUE_STATS 0x01
#define SYSMON_TIMEOUT_MS 5000

#define SYSMON_FLAG_AC_CONNECTED (1 << 0)
#define SYSMON_FLAG_BATTERY (1 << 1)
#define SYSMON_FLAG_DGPU (1 << 2)
#define SYSMON_FLAG_TEMP (1 << 3)
#define SYSMON_FLAG_CHARGING (1 << 4)
#define SYSMON_FLAG_DISCHARGING (1 << 5)

// Fixed row colors (QMK hue 0-255)
#define SYSMON_HUE_CPU 0    // red
#define SYSMON_HUE_MEM 36   // gold
#define SYSMON_HUE_IGPU 0   // red
#define SYSMON_HUE_DGPU 85  // green

typedef struct {
    uint8_t flags;
    uint8_t battery;
    uint8_t cpu;
    uint8_t mem;
    uint8_t igpu;
    uint8_t dgpu;
    uint8_t temp_c;
    uint8_t brightness;
} sysmon_stats_t;

static sysmon_stats_t sysmon;
static uint32_t       sysmon_last_update = 0;

void via_custom_value_command_kb(uint8_t *data, uint8_t length) {
    uint8_t *command_id = &(data[0]);
    uint8_t *channel_id = &(data[1]);
    uint8_t *value_id   = &(data[2]);
    uint8_t *payload    = &(data[3]);

    if (*channel_id == SYSMON_CHANNEL) {
        if (*command_id == id_custom_set_value && *value_id == SYSMON_VALUE_STATS) {
            sysmon.flags       = payload[0];
            sysmon.battery     = payload[1];
            sysmon.cpu         = payload[2];
            sysmon.mem         = payload[3];
            sysmon.igpu        = payload[4];
            sysmon.dgpu        = payload[5];
            sysmon.temp_c      = payload[6];
            sysmon.brightness  = payload[7];
            sysmon_last_update = timer_read32();
        }
        return;
    }

    *command_id = id_unhandled;
}

// LED index of each visual row, left to right. Derived from the physical
// position table in macropad.c (rows share a y coordinate, sorted by x).
static const uint8_t sysmon_row_leds[6][4] = {
    {5, 2, 22, 17}, // top row of small keys
    {4, 0, 20, 18},
    {7, 1, 21, 16},
    {6, 3, 23, 19},
    {9, 11, 15, 13},
    {8, 10, 14, 12},
};

// Overall brightness rides the IS31FL3743A's global current register (GCC)
// instead of scaling the PWM values: PWM then keeps its full 8-bit
// resolution at any brightness, instead of the few dozen levels left after
// scale8() at a dim host setting. The board's ISSI_GLOBALCURRENT (185) is a
// current-budget cap, so the host value scales within it, never above —
// val=255 lands exactly on the stock value, which is also the idle state.
// Safe to call mid-frame: the driver re-selects the PWM page on every flush.
//
// The map is val squared, not linear: PWM dimming is linear in light output,
// but GCC dims by LED current, and LEDs convert low currents to light more
// efficiently, so a linear map runs visibly too bright at the dim end.
static uint8_t sysmon_gcc = ISSI_GLOBALCURRENT; // last value written to the chip

static void sysmon_set_gcc(uint8_t val) {
    uint8_t gcc = (uint8_t)((uint32_t)val * val * ISSI_GLOBALCURRENT / (255UL * 255));
    if (gcc == 0 && val > 0) gcc = 1; // host wants light; GCC 0 is fully off
    if (gcc == sysmon_gcc) return;
    sysmon_gcc = gcc;
    IS31FL_unlock_register(DRIVER_ADDR_1, ISSI_PAGE_FUNCTION);
    IS31FL_write_single_register(DRIVER_ADDR_1, ISSI_REG_GLOBALCURRENT, gcc);
}

// Partially-filled bar segments fade in with the fraction, but emitted
// light goes as PWM x GCC: at global current 1-2, PWM below roughly half
// scale is not visible at all (measured empirically — even ~100 vanishes
// while a full segment reads fine), so low-percentage stats disappear.
// Instead of fading from 0, fade from a GCC-dependent floor: the partial
// range compresses into [floor..255], keeping every nonzero fraction both
// visible and distinguishable. At high GCC the floor decays to ~1 and the
// fade becomes proportional again.
static uint16_t sysmon_seg_visible(uint16_t seg) {
    uint16_t fl = 128 / sysmon_gcc;
    if (fl == 0) fl = 1;
    return fl + seg * (255 - fl) / 255;
}

// Paint one row as a 4-segment bar. pct 0-100, val is max brightness.
// The fractional segment fades in for sub-quarter resolution.
static void sysmon_render_bar(uint8_t row, uint8_t pct, uint8_t hue, uint8_t val) {
    if (pct > 100) pct = 100;
    uint16_t level = (uint16_t)pct * 4 * 255 / 100; // 0..1020

    for (uint8_t i = 0; i < 4; i++) {
        uint16_t seg = 0;
        if (level >= (uint16_t)(i + 1) * 255) {
            seg = 255;
        } else if (level > (uint16_t)i * 255) {
            seg = sysmon_seg_visible(level - i * 255);
        }
        RGB rgb = hsv_to_rgb((HSV){hue, 255, scale8(val, seg)});
        rgb_matrix_set_color(sysmon_row_leds[row][i], rgb.r, rgb.g, rgb.b);
    }
}

static void sysmon_render_off(uint8_t row) {
    for (uint8_t i = 0; i < 4; i++) {
        rgb_matrix_set_color(sysmon_row_leds[row][i], 0, 0, 0);
    }
}

// Charge/discharge pulse (~4s cycle): a trapezoid — linear rise, hold
// bright, linear fall, rest fully off — paced through a sqrt curve.
// With s ~ sqrt(t), the dwell time on each PWM level scales with the level
// itself, so the ramps sweep the coarse bottom levels quickly (adjacent
// steps there are huge relative jumps that read as stutter) and ease
// through the fine bright ones. Resting at off matters too: holding a
// barely-lit level reads as a freeze, while dark reads as intentional.
// Charging runs rise-first ("energy arriving"); discharging is
// time-reversed.
#define SYSMON_PULSE_PERIOD 4096 // ms; power of two so the modulo is cheap
#define SYSMON_PULSE_RAMP 1024   // ms of each ramp
#define SYSMON_PULSE_HOLD 1024   // ms held bright; the remainder rests off

static uint8_t sysmon_pulse(bool inverted) {
    uint16_t t = timer_read32() % SYSMON_PULSE_PERIOD;
    if (inverted) t = SYSMON_PULSE_PERIOD - 1 - t;

    uint8_t s; // envelope 0-255
    if (t < SYSMON_PULSE_RAMP) {
        s = (uint32_t)t * 255 / SYSMON_PULSE_RAMP;
    } else if (t < SYSMON_PULSE_RAMP + SYSMON_PULSE_HOLD) {
        s = 255;
    } else if (t < 2 * SYSMON_PULSE_RAMP + SYSMON_PULSE_HOLD) {
        s = 255 - (uint32_t)(t - SYSMON_PULSE_RAMP - SYSMON_PULSE_HOLD) * 255 / SYSMON_PULSE_RAMP;
    } else {
        s = 0;
    }
    return sqrt16((uint16_t)s * 255); // sqrt pacing; maps 0..255 onto 0..255
}

// Slew brightness toward the host-sent target (1 level per 4ms) so a stats
// packet landing mid-pulse can't step the whole display's brightness.
static uint8_t sysmon_slewed_val(void) {
    static uint8_t  val  = 0;
    static uint32_t last = 0;

    uint32_t now  = timer_read32();
    uint32_t step = (now - last) / 4;
    if (step) {
        last = now;
        if (val < sysmon.brightness) {
            val += MIN(step, (uint32_t)(sysmon.brightness - val));
        } else {
            val -= MIN(step, (uint32_t)(val - sysmon.brightness));
        }
    }
    return val;
}

static void sysmon_render_battery(uint8_t val) {
    uint8_t pct         = sysmon.battery > 100 ? 100 : sysmon.battery;
    bool    ac          = sysmon.flags & SYSMON_FLAG_AC_CONNECTED;
    bool    charging    = sysmon.flags & SYSMON_FLAG_CHARGING;
    bool    discharging = sysmon.flags & SYSMON_FLAG_DISCHARGING;
    if (!ac && !charging) discharging = true; // unplugged always drains

    if (discharging && pct < 15) {
        // critical battery: blink the whole row red at ~1Hz
        if (timer_read32() & 512) {
            sysmon_render_off(0);
        } else {
            sysmon_render_bar(0, 100, 0, val);
        }
        return;
    }

    // segment at the charge boundary, where filling/draining happens
    uint8_t seg = pct * 4 / 100;
    if (seg > 3) seg = 3;

    if (charging) {
        // level bar, red at empty through green at full; the segment
        // currently filling pulses bright then rests (~4s cycle)
        uint8_t hue   = (uint8_t)((uint16_t)pct * 85 / 100);
        uint8_t pulse = sysmon_pulse(false);
        sysmon_render_bar(0, pct, hue, val);
        RGB rgb = hsv_to_rgb((HSV){hue, 255, scale8(val, pulse)});
        rgb_matrix_set_color(sysmon_row_leds[0][seg], rgb.r, rgb.g, rgb.b);
    } else if (discharging) {
        // red level bar; the boundary segment flares and fades to show the drain
        uint8_t pulse = sysmon_pulse(true);
        sysmon_render_bar(0, pct, 0, val);
        RGB rgb = hsv_to_rgb((HSV){0, 255, scale8(val, pulse)});
        rgb_matrix_set_color(sysmon_row_leds[0][seg], rgb.r, rgb.g, rgb.b);
    } else {
        // on AC, held at the charge cap: whole row steady dim green
        RGB rgb = hsv_to_rgb((HSV){85, 255, scale8(val, 80)});
        for (uint8_t i = 0; i < 4; i++) {
            rgb_matrix_set_color(sysmon_row_leds[0][i], rgb.r, rgb.g, rgb.b);
        }
    }
}

static void sysmon_render_temp(uint8_t val) {
    if (!(sysmon.flags & SYSMON_FLAG_TEMP)) {
        sysmon_render_off(5);
        return;
    }
    // bar length scales 30C..85C; colors are a fixed green-to-deep-red
    // gradient across the four segments regardless of temperature
    uint8_t t = sysmon.temp_c;
    if (t < 30) t = 30;
    if (t > 85) t = 85;
    uint8_t  pct   = (uint8_t)((uint16_t)(t - 30) * 100 / 55);
    uint16_t level = (uint16_t)pct * 4 * 255 / 100;

    for (uint8_t i = 0; i < 4; i++) {
        uint16_t seg = 0;
        if (level >= (uint16_t)(i + 1) * 255) {
            seg = 255;
        } else if (level > (uint16_t)i * 255) {
            seg = sysmon_seg_visible(level - i * 255);
        }
        uint8_t hue = 85 - i * 85 / 3; // 85, 57, 28, 0
        RGB rgb = hsv_to_rgb((HSV){hue, 255, scale8(val, seg)});
        rgb_matrix_set_color(sysmon_row_leds[5][i], rgb.r, rgb.g, rgb.b);
    }
}

bool rgb_matrix_indicators_advanced_user(uint8_t led_min, uint8_t led_max) {
    if (sysmon_last_update == 0 || timer_elapsed32(sysmon_last_update) > SYSMON_TIMEOUT_MS) {
        // No fresh data from the host; restore the stock global current and
        // let the normal RGB effect run at its own brightness
        sysmon_set_gcc(255);
        return true;
    }

    sysmon_set_gcc(sysmon_slewed_val());
    uint8_t val = 255; // full PWM scale; GCC carries the brightness

    if (sysmon.flags & SYSMON_FLAG_BATTERY) {
        sysmon_render_battery(val);
    } else {
        sysmon_render_off(0);
    }

    sysmon_render_bar(1, sysmon.cpu, SYSMON_HUE_CPU, val);
    sysmon_render_bar(2, sysmon.mem, SYSMON_HUE_MEM, val);
    sysmon_render_bar(3, sysmon.igpu, SYSMON_HUE_IGPU, val);

    if (sysmon.flags & SYSMON_FLAG_DGPU) {
        sysmon_render_bar(4, sysmon.dgpu, SYSMON_HUE_DGPU, val);
    } else {
        sysmon_render_off(4);
    }

    sysmon_render_temp(val);

    return false;
}

#endif // RGB_MATRIX_ENABLE
