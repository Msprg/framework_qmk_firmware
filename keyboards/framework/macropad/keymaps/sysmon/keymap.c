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
// Host -> macropad protocol (FROZEN WIRE PROTOCOL v2)
//
// The firmware is a generic, parameterized render engine: it holds NO
// application policy. The host decides what every row means (which metric,
// which colors, which animation) and sends a fully-described frame. The
// firmware only draws what it is told.
//
// Transport: VIA "custom set value" raw-HID report on private channel 0x42.
// Each report is 32 bytes. The host writes a leading 0x00 HID report id which
// the firmware never sees, so data[0] is the VIA command id.
//
//   data[0] = 0x07 (id_custom_set_value)
//   data[1] = SYSMON_CHANNEL (0x42)
//   data[2] = value_id: 0x01 = FRAME A (rows 0,1,2)
//                       0x02 = FRAME B (rows 3,4,5 + globals)
//   data[3..] = payload
//
// Two FRAME reports are sent every tick. Each carries three 8-byte row
// descriptors back-to-back:
//
//   row descriptor (8 bytes):
//     +0 level         0..255  fraction of the 4-segment bar (0=empty,255=full)
//     +1 row_val       0..255  base row brightness (normally 255)
//     +2 hue0          0..255  solid color, or gradient start
//     +3 hue1          0..255  gradient end hue (ignored unless gradient set)
//     +4 flags         bit0    color_gradient: 0=solid hue0, 1=gradient
//                      bits1-2 envelope_type: 0=none, 1=blink, 2=breathe
//                      bits3-4 overlay_shape: 0=none,1=trap-rise,2=trap-fall,3=sine
//                      bit5    overlay_dir: 0=upward from boundary, 1=downward
//                      bits6-7 reserved (0)
//     +5 overlay_count 0=off; 1..4 = segments from the boundary
//     +6 period        units of 32ms; period of whichever timed effect runs
//     +7 floor         0..255  trough floor for breathe / amplitude floor for sine
//
//   FRAME A payload (data[3..26]) = row0[8] ++ row1[8] ++ row2[8]
//   FRAME B payload (data[3..26]) = row3[8] ++ row4[8] ++ row5[8], then:
//     data[27] = brightness  0..255  (overall; drives GCC via the slew)
//     data[28] = glide       units of 4ms   (per-row level glide; default 75)
//     data[29] = timeout     units of 100ms (no-data fallback; default 50)
//
// VIA echoes the buffer back, which serves as the ACK.
// ---------------------------------------------------------------------------

#define SYSMON_CHANNEL 0x42
#define SYSMON_FRAME_A 0x01 // rows 0,1,2
#define SYSMON_FRAME_B 0x02 // rows 3,4,5 + globals

#define SYSMON_ROW_BYTES 8 // per-row descriptor size in the payload

// flags field bit layout
#define SYSMON_FLAG_GRADIENT (1 << 0)      // bit0: solid hue0 vs hue0->hue1
#define SYSMON_ENV_SHIFT 1                 // bits1-2: envelope_type
#define SYSMON_ENV_MASK (0x3 << SYSMON_ENV_SHIFT)
#define SYSMON_OVL_SHIFT 3                 // bits3-4: overlay_shape
#define SYSMON_OVL_MASK (0x3 << SYSMON_OVL_SHIFT)
#define SYSMON_DIR_BIT (1 << 5)            // bit5: overlay direction

// envelope_type values
#define SYSMON_ENV_NONE 0
#define SYSMON_ENV_BLINK 1
#define SYSMON_ENV_BREATHE 2

// overlay_shape values
#define SYSMON_OVL_NONE 0
#define SYSMON_OVL_TRAP_RISE 1
#define SYSMON_OVL_TRAP_FALL 2
#define SYSMON_OVL_SINE 3

// Defaults applied until the first FRAME B arrives, so an early FRAME A still
// renders sensibly. Brightness 255 lands on the stock GCC; glide/timeout match
// the host defaults (300ms glide => 75, 5000ms timeout => 50).
#define SYSMON_DEFAULT_GLIDE_UNITS 75
#define SYSMON_DEFAULT_TIMEOUT_UNITS 50

// Generic per-row descriptor: exactly the 8 wire bytes, no semantics attached.
typedef struct {
    uint8_t level;         // 0..255 bar fraction
    uint8_t row_val;       // 0..255 base brightness
    uint8_t hue0;          // solid / gradient start
    uint8_t hue1;          // gradient end
    uint8_t flags;         // gradient / envelope / overlay bits
    uint8_t overlay_count; // 0=off, 1..4 segments from boundary
    uint8_t period;        // x32ms
    uint8_t floor;         // breathe trough / sine floor
} sysmon_row_t;

static sysmon_row_t sysmon_rows[6];
static uint8_t      sysmon_brightness     = 255;
static uint8_t      sysmon_glide_units    = SYSMON_DEFAULT_GLIDE_UNITS;
static uint8_t      sysmon_timeout_units  = SYSMON_DEFAULT_TIMEOUT_UNITS;
static uint32_t     sysmon_last_update    = 0;

// Decode one 8-byte descriptor at src into sysmon_rows[row].
static void sysmon_decode_row(uint8_t row, const uint8_t *src) {
    sysmon_rows[row].level         = src[0];
    sysmon_rows[row].row_val       = src[1];
    sysmon_rows[row].hue0          = src[2];
    sysmon_rows[row].hue1          = src[3];
    sysmon_rows[row].flags         = src[4];
    sysmon_rows[row].overlay_count = src[5];
    sysmon_rows[row].period        = src[6];
    sysmon_rows[row].floor         = src[7];
}

void via_custom_value_command_kb(uint8_t *data, uint8_t length) {
    uint8_t *command_id = &(data[0]);
    uint8_t *channel_id = &(data[1]);
    uint8_t *value_id   = &(data[2]);
    uint8_t *payload    = &(data[3]);

    if (*channel_id == SYSMON_CHANNEL) {
        if (*command_id == id_custom_set_value && *value_id == SYSMON_FRAME_A) {
            sysmon_decode_row(0, payload + 0 * SYSMON_ROW_BYTES);
            sysmon_decode_row(1, payload + 1 * SYSMON_ROW_BYTES);
            sysmon_decode_row(2, payload + 2 * SYSMON_ROW_BYTES);
            sysmon_last_update = timer_read32();
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_FRAME_B) {
            sysmon_decode_row(3, payload + 0 * SYSMON_ROW_BYTES);
            sysmon_decode_row(4, payload + 1 * SYSMON_ROW_BYTES);
            sysmon_decode_row(5, payload + 2 * SYSMON_ROW_BYTES);
            // globals follow the three row descriptors (payload[24..26])
            sysmon_brightness    = payload[3 * SYSMON_ROW_BYTES + 0];
            sysmon_glide_units   = payload[3 * SYSMON_ROW_BYTES + 1];
            sysmon_timeout_units = payload[3 * SYSMON_ROW_BYTES + 2];
            sysmon_last_update   = timer_read32();
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

// Lift a sub-full PWM brightness onto a GCC-dependent visibility floor.
// Emitted light goes as PWM x GCC, and at global current 1-2 any PWM below
// roughly half scale is not visible at all (measured empirically — even
// ~100 vanishes while a full segment reads fine), so dim fills disappear.
// Mapping [0..255] onto [floor..255] keeps every nonzero level both visible
// and distinguishable; at high GCC the floor decays to ~1 and the map
// becomes proportional again. Used both for partially-filled bar segments
// (the fractional fade) and for the battery row's steady dim fills, which
// would otherwise drop out entirely at low screen brightness.
static uint16_t sysmon_visible_pwm(uint16_t pwm) {
    uint16_t fl = 128 / sysmon_gcc;
    if (fl == 0) fl = 1;
    return fl + pwm * (255 - fl) / 255;
}

// Color mode for the bar fill: a single solid hue, or a per-segment gradient
// sweeping hue0 -> hue1 across the four segments.
typedef enum {
    SYSMON_COLOR_SOLID = 0,
    SYSMON_COLOR_GRADIENT,
} sysmon_color_mode_t;

// Hue of bar segment i (0..3) under the chosen color mode. For a gradient,
//   hue_i = hue0 + (hue1 - hue0) * i / 3
// matching the legacy temperature gradient: GRADIENT(85->0) => {85,57,29,0}.
static uint8_t sysmon_seg_hue(sysmon_color_mode_t mode, uint8_t hue0, uint8_t hue1, uint8_t i) {
    if (mode == SYSMON_COLOR_SOLID) return hue0;
    return (uint8_t)((int16_t)hue0 + ((int16_t)hue1 - (int16_t)hue0) * (int16_t)i / 3);
}

// Paint one row as a 4-segment bar from a 0..255 level (0=empty, 255=full).
// level*4 spans 0..1020 across the four segments; full segments read 255,
// the fractional boundary segment fades in through sysmon_visible_pwm so a
// dim row_val (e.g. an AC-idle dim fill) stays visible and distinguishable.
// Colored solid hue0 or as a hue0->hue1 gradient, at brightness row_val.
static void sysmon_render_bar(uint8_t row, uint8_t level, sysmon_color_mode_t mode,
                              uint8_t hue0, uint8_t hue1, uint8_t row_val) {
    uint16_t lvl = (uint16_t)level * 4; // 0..1020

    for (uint8_t i = 0; i < 4; i++) {
        uint16_t fill = 0; // 0..255: how much of this segment is lit
        if (lvl >= (uint16_t)(i + 1) * 255) {
            fill = 255;
        } else if (lvl > (uint16_t)i * 255) {
            fill = lvl - (uint16_t)i * 255;
        }
        // Scale the lit fraction by the row brightness, then lift any nonzero
        // result onto the GCC visibility floor so dim fills stay visible at low
        // global current — both the fractional boundary segment and a low
        // row_val (e.g. the battery AC-idle dim green, which would otherwise
        // drop out entirely). Full-scale values are unaffected (visible_pwm
        // maps 255->255); off segments stay off.
        uint8_t bright = scale8(row_val, (uint8_t)fill);
        if (bright) bright = (uint8_t)sysmon_visible_pwm(bright);
        uint8_t hue = sysmon_seg_hue(mode, hue0, hue1, i);
        RGB rgb = hsv_to_rgb((HSV){hue, 255, bright});
        rgb_matrix_set_color(sysmon_row_leds[row][i], rgb.r, rgb.g, rgb.b);
    }
}

// Boundary segment (0..3) for a given level — where filling/draining happens.
static uint8_t sysmon_boundary_seg(uint8_t level) {
    uint8_t pct = (uint8_t)((uint16_t)level * 100 / 255);
    uint8_t seg = pct * 4 / 100;
    if (seg > 3) seg = 3;
    return seg;
}

// Trapezoidal pulse paced through a sqrt curve, period in ms. Linear rise,
// hold bright, linear fall, rest fully off. With s ~ sqrt(t) the dwell on
// each PWM level scales with the level, so the ramps sweep the coarse bottom
// levels quickly and ease through the fine bright ones; resting at off reads
// as intentional rather than frozen. trap-rise runs rise-first; trap-fall is
// the same trapezoid time-reversed. RAMP/HOLD scale with the period so the
// shape is preserved at any cadence (legacy: 4096 period => 1024 ramp/hold).
static uint8_t sysmon_pulse(uint16_t period_ms, bool inverted) {
    if (period_ms == 0) period_ms = 1;
    uint16_t ramp = period_ms / 4; // legacy 4096 -> 1024
    uint16_t hold = period_ms / 4; // legacy 4096 -> 1024
    if (ramp == 0) ramp = 1;

    uint16_t t = timer_read32() % period_ms;
    if (inverted) t = period_ms - 1 - t;

    uint8_t s; // envelope 0-255
    if (t < ramp) {
        s = (uint8_t)((uint32_t)t * 255 / ramp);
    } else if (t < ramp + hold) {
        s = 255;
    } else if (t < 2 * ramp + hold) {
        s = (uint8_t)(255 - (uint32_t)(t - ramp - hold) * 255 / ramp);
    } else {
        s = 0;
    }
    return sqrt16((uint16_t)s * 255); // sqrt pacing; maps 0..255 onto 0..255
}

// Square-wave blink at the given period: full row_val for the first half of
// the cycle, off for the second. Reproduces the legacy ~1Hz critical blink
// (period 1024ms => the on/off toggle lands on timer_read32() & 512).
static uint8_t sysmon_blink(uint16_t period_ms, uint8_t row_val) {
    if (period_ms == 0) period_ms = 1;
    return (timer_read32() % period_ms) < (period_ms / 2) ? row_val : 0;
}

// Breathe between floor..row_val on a sin8 ramp at the given period.
static uint8_t sysmon_breathe(uint16_t period_ms, uint8_t row_val, uint8_t floor) {
    if (period_ms == 0) period_ms = 1;
    // sin8 takes a 0..255 phase that wraps once per 256 units; advancing the
    // phase by 256 over period_ms gives one full breath per period.
    uint8_t phase = (uint8_t)((timer_read32() % period_ms) * 256 / period_ms);
    uint8_t env   = sin8(phase); // 0..255
    if (floor > row_val) floor = row_val;
    return (uint8_t)(floor + scale8(env, row_val - floor));
}

// Per-row glide state: linearly interpolate the displayed level toward the
// latest host-sent target over glide_ms so bars sweep instead of snapping.
typedef struct {
    uint8_t  shown;
    uint8_t  start;
    uint8_t  target;
    uint32_t t0;
} sysmon_glide_t;

static uint8_t sysmon_glide(sysmon_glide_t *g, uint8_t target, uint32_t glide_ms) {
    if (glide_ms == 0) glide_ms = 1;
    uint32_t now = timer_read32();
    if (target != g->target) {
        g->start  = g->shown;
        g->target = target;
        g->t0     = now;
    }
    uint32_t elapsed = now - g->t0;
    if (elapsed >= glide_ms) {
        g->shown = g->target;
    } else {
        g->shown = (uint8_t)(g->start + ((int16_t)g->target - g->start) * (int32_t)elapsed / (int32_t)glide_ms);
    }
    return g->shown;
}

// Slew brightness toward the host-sent target (1 level per 4ms) so a frame
// landing mid-pulse can't step the whole display's brightness.
static uint8_t sysmon_slewed_val(void) {
    static uint8_t  val  = 0;
    static uint32_t last = 0;

    uint32_t now  = timer_read32();
    uint32_t step = (now - last) / 4;
    if (step) {
        last = now;
        if (val < sysmon_brightness) {
            val += MIN(step, (uint32_t)(sysmon_brightness - val));
        } else {
            val -= MIN(step, (uint32_t)(val - sysmon_brightness));
        }
    }
    return val;
}

// Generic render of one row: glide the level, fill the bar, apply the
// envelope to the whole row's effective brightness, then paint any overlay
// segments from the boundary. No metric-specific logic lives here — every
// behavior is described by the row descriptor the host sent.
static void sysmon_render_row(uint8_t row, sysmon_glide_t *g, uint32_t glide_ms) {
    const sysmon_row_t *r = &sysmon_rows[row];

    uint8_t level = sysmon_glide(g, r->level, glide_ms);

    sysmon_color_mode_t mode = (r->flags & SYSMON_FLAG_GRADIENT) ? SYSMON_COLOR_GRADIENT
                                                                 : SYSMON_COLOR_SOLID;
    uint16_t period_ms = (uint16_t)r->period * 32;

    // Envelope modulates the whole row's effective brightness over time.
    uint8_t row_val = r->row_val;
    switch ((r->flags & SYSMON_ENV_MASK) >> SYSMON_ENV_SHIFT) {
        case SYSMON_ENV_BLINK:
            row_val = sysmon_blink(period_ms, r->row_val);
            break;
        case SYSMON_ENV_BREATHE:
            row_val = sysmon_breathe(period_ms, r->row_val, r->floor);
            break;
        default:
            break; // SYSMON_ENV_NONE
    }

    sysmon_render_bar(row, level, mode, r->hue0, r->hue1, row_val);

    // Overlay paints overlay_count segments from the boundary, overriding the
    // base fill there. Colored with the row hue (hue0).
    if (r->overlay_count > 0) {
        uint8_t shape = (r->flags & SYSMON_OVL_MASK) >> SYSMON_OVL_SHIFT;
        bool    down  = r->flags & SYSMON_DIR_BIT;
        uint8_t seg   = sysmon_boundary_seg(level);

        uint8_t bright;
        switch (shape) {
            // The trapezoid pulse is a 0..255 envelope that rests fully off, so
            // scale the row brightness by it (the rest-at-off is intentional —
            // no visibility lift). Matches the legacy charge/discharge flare.
            case SYSMON_OVL_TRAP_RISE:
                bright = scale8(r->row_val, sysmon_pulse(period_ms, false));
                break;
            case SYSMON_OVL_TRAP_FALL:
                bright = scale8(r->row_val, sysmon_pulse(period_ms, true));
                break;
            // The sine tell is an absolute floor..row_val level (not a 0..255
            // envelope), so lift it onto the visibility floor like a dim base
            // fill so the faint breathe stays visible. Matches the legacy
            // battery cap-drift tell.
            case SYSMON_OVL_SINE: {
                uint8_t b = sysmon_breathe(period_ms, r->row_val, r->floor);
                bright = b ? (uint8_t)sysmon_visible_pwm(b) : 0;
                break;
            }
            default:
                return; // SYSMON_OVL_NONE: nothing to paint
        }

        RGB rgb = hsv_to_rgb((HSV){r->hue0, 255, bright});
        for (uint8_t k = 0; k < r->overlay_count; k++) {
            int8_t idx = down ? (int8_t)seg - (int8_t)k : (int8_t)seg + (int8_t)k;
            if (idx < 0 || idx > 3) continue;
            rgb_matrix_set_color(sysmon_row_leds[row][idx], rgb.r, rgb.g, rgb.b);
        }
    }
}

bool rgb_matrix_indicators_advanced_user(uint8_t led_min, uint8_t led_max) {
    (void)led_min;
    (void)led_max;

    uint32_t timeout_ms = (uint32_t)sysmon_timeout_units * 100;
    if (sysmon_last_update == 0 || timer_elapsed32(sysmon_last_update) > timeout_ms) {
        // No fresh data from the host; restore the stock global current and
        // let the normal RGB effect run at its own brightness
        sysmon_set_gcc(255);
        return true;
    }

    sysmon_set_gcc(sysmon_slewed_val());

    uint32_t glide_ms = (uint32_t)sysmon_glide_units * 4;

    static sysmon_glide_t glides[6];
    for (uint8_t row = 0; row < 6; row++) {
        sysmon_render_row(row, &glides[row], glide_ms);
    }

    return false;
}

#endif // RGB_MATRIX_ENABLE
