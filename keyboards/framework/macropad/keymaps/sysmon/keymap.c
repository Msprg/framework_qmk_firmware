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
//   row 3: iGPU usage (NPU usage as a dim ghost bar behind it)
//   row 4: dGPU usage
//   row 5: system temperature
//
// Overall brightness is dictated by the host (it follows the laptop's
// screen brightness). When no stats arrive for SYSMON_TIMEOUT_MS the
// macropad falls back to the regular RGB matrix effect.
//
// From power-up until the host first connects, a dim diagonal rainbow is
// shown instead, introduced by a short boot animation (see "Boot rainbow").

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
// An optional third report, FRAME C (value_id 0x03), adds a second, dimmer
// "ghost" bar to any row: drawn in its own hue behind the main bar, so it only
// shows where it reaches past the main bar's fill. Six 4-byte ghost
// descriptors, one per row, back-to-back:
//
//   ghost descriptor (4 bytes):
//     +0 level   0..255  fraction of the 4-segment bar (0 = no ghost)
//     +1 hue     0..255  ghost color
//     +2 val     0..255  ghost brightness (the row envelope scales it too)
//     +3 flags   reserved (0)
//
//   FRAME C payload (data[3..26]) = ghost0[4] ++ ... ++ ghost5[4]
//   FRAME C echo: data[27] is set to SYSMON_GHOST_ACK, so the host can tell
//   ghost-capable firmware from older firmware (which echoes it unchanged).
//
// FRAME C is independent of A/B: it doesn't keep the display alive, and its
// ghosts fade out on their own if FRAME C stops arriving within the timeout,
// so an older host that never sends it can't leave a stale ghost on screen.
// Older firmware ignores value_id 0x03 and just draws the main bars.
//
// BOOT REPLAY (value_id 0x10) replays boot animations, for previewing and
// tuning them. Numbered playback shows N white dots before variant N.
//   data[3] = first variant, 1-based (0 = all of them; 0xFE = play the next
//             power-up pick, unnumbered, exactly as a boot would; any other
//             value past the last variant stops a running replay)
//   data[4] = last variant (0 = only the first)
//   data[5] = rounds (0 = 1)
//   (0xFD in data[3] changes nothing: a status query)
// The echo carries status in data[11..]: the power-up counter (u32 LE, as
// RAM holds it: already advanced past this boot's pick), the variant this
// power-up picked (1-based, 0 = none yet), and the counter as stored in
// EEPROM (u32 LE; lags by one until this boot's animation has finished).
// It never counts as host data, so it doesn't end the pre-connect rainbow.
//
// VIA echoes the buffer back, which serves as the ACK.
// ---------------------------------------------------------------------------

#define SYSMON_CHANNEL 0x42
#define SYSMON_FRAME_A 0x01 // rows 0,1,2
#define SYSMON_FRAME_B 0x02 // rows 3,4,5 + globals
#define SYSMON_FRAME_C 0x03 // ghost bars, rows 0..5
#define SYSMON_BOOT_REPLAY 0x10 // replay the boot animation (preview/tuning)

#define SYSMON_ROW_BYTES 8   // per-row descriptor size in the payload
#define SYSMON_GHOST_BYTES 4 // per-row ghost descriptor size in FRAME C
#define SYSMON_GHOST_ACK 0x01 // written into the FRAME C echo at data[27]

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

// Optional per-row ghost bar (FRAME C): the 4 wire bytes, no semantics attached.
typedef struct {
    uint8_t level; // 0..255 ghost bar fraction (0 = none)
    uint8_t hue;   // ghost color
    uint8_t val;   // ghost brightness
    uint8_t flags; // reserved
} sysmon_ghost_t;

static sysmon_row_t   sysmon_rows[6];
static sysmon_ghost_t sysmon_ghosts[6];
static uint8_t        sysmon_brightness     = 255;
static uint8_t        sysmon_glide_units    = SYSMON_DEFAULT_GLIDE_UNITS;
static uint8_t        sysmon_timeout_units  = SYSMON_DEFAULT_TIMEOUT_UNITS;
static uint32_t       sysmon_last_update    = 0;
static uint32_t       sysmon_ghost_update   = 0; // last FRAME C; 0 = never

// Defined with the boot rainbow below.
static void boot_replay(const uint8_t *args);
static void boot_status(uint8_t *out);

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
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_FRAME_C) {
            for (uint8_t row = 0; row < 6; row++) {
                const uint8_t *src = payload + row * SYSMON_GHOST_BYTES;
                sysmon_ghosts[row].level = src[0];
                sysmon_ghosts[row].hue   = src[1];
                sysmon_ghosts[row].val   = src[2];
                sysmon_ghosts[row].flags = src[3];
            }
            sysmon_ghost_update = timer_read32();
            // VIA echoes this buffer back: mark it so the host can detect
            // ghost support (payload[24] = data[27], past the descriptors).
            payload[6 * SYSMON_GHOST_BYTES] = SYSMON_GHOST_ACK;
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_BOOT_REPLAY) {
            // Deliberately leaves sysmon_last_update alone: a preview is not
            // the host connecting.
            boot_replay(payload);
            boot_status(payload + 8);
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

// How much of segment i (0..3) a bar of length lvl (0..1020) covers, 0..255.
static uint16_t sysmon_seg_fill(uint16_t lvl, uint8_t i) {
    if (lvl >= (uint16_t)(i + 1) * 255) return 255;
    if (lvl > (uint16_t)i * 255) return lvl - (uint16_t)i * 255;
    return 0;
}

// Paint one row as a 4-segment bar from a 0..255 level (0=empty, 255=full).
// level*4 spans 0..1020 across the four segments; full segments read 255,
// the fractional boundary segment fades in through sysmon_visible_pwm so a
// dim row_val (e.g. an AC-idle dim fill) stays visible and distinguishable.
// Colored solid hue0 or as a hue0->hue1 gradient, at brightness row_val.
//
// A ghost bar (ghost_level > 0) is drawn behind the main bar in ghost_hue at
// ghost_val: each segment adds whatever part of it the ghost covers *beyond*
// the main fill, so the ghost only shows past the main bar's end, and a
// segment the two share mixes both colors in proportion to their coverage.
static void sysmon_render_bar(uint8_t row, uint8_t level, sysmon_color_mode_t mode,
                              uint8_t hue0, uint8_t hue1, uint8_t row_val,
                              uint8_t ghost_level, uint8_t ghost_hue, uint8_t ghost_val) {
    uint16_t lvl  = (uint16_t)level * 4;       // 0..1020
    uint16_t glvl = (uint16_t)ghost_level * 4; // 0..1020

    for (uint8_t i = 0; i < 4; i++) {
        uint16_t fill = sysmon_seg_fill(lvl, i); // 0..255: how much is lit
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

        uint16_t gfill = sysmon_seg_fill(glvl, i);
        if (gfill > fill) {
            uint16_t gpart = gfill - fill; // 1..255: ghost coverage past the main fill
            uint8_t  gb    = scale8(ghost_val, (uint8_t)gpart);
            // Visibility lift, weighted by the ghost's share of the segment's
            // coverage (gpart / gfill). An otherwise-dark segment (fill = 0)
            // gets the full lift, like any dim fill. A ghost sliver past a
            // nearly-full main segment gets almost none, so it can't tint that
            // segment heavily at low global current. A tiny main sliver under a
            // mostly-ghost segment lifts the ghost nearly fully; unlifted, that
            // segment would read darker than the ghost-only ones beside it.
            if (gb) {
                uint16_t lift = sysmon_visible_pwm(gb) - gb;
                gb = (uint8_t)(gb + (uint32_t)lift * gpart / gfill);
            }
            if (gb) {
                RGB g = hsv_to_rgb((HSV){ghost_hue, 255, gb});
                rgb.r = qadd8(rgb.r, g.r);
                rgb.g = qadd8(rgb.g, g.g);
                rgb.b = qadd8(rgb.b, g.b);
            }
        }
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
static void sysmon_render_row(uint8_t row, sysmon_glide_t *g, sysmon_glide_t *gg,
                              uint32_t glide_ms, uint32_t timeout_ms) {
    const sysmon_row_t   *r  = &sysmon_rows[row];
    const sysmon_ghost_t *gh = &sysmon_ghosts[row];

    uint8_t level = sysmon_glide(g, r->level, glide_ms);

    // A ghost glides like the main bar, and glides out to empty once FRAME C
    // goes stale (or never arrived), keeping its last color on the way out.
    bool    ghost_fresh = sysmon_ghost_update != 0 && timer_elapsed32(sysmon_ghost_update) <= timeout_ms;
    uint8_t ghost_level = sysmon_glide(gg, ghost_fresh ? gh->level : 0, glide_ms);

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

    // The envelope belongs to the whole row, ghost included: scale the ghost
    // by the same fraction it took off the main bar (a blinking row must not
    // leave a steady ghost behind).
    uint8_t ghost_val = gh->val;
    if (r->row_val) ghost_val = (uint8_t)((uint16_t)ghost_val * row_val / r->row_val);

    sysmon_render_bar(row, level, mode, r->hue0, r->hue1, row_val,
                      ghost_level, gh->hue, ghost_val);

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

// ---------------------------------------------------------------------------
// Boot rainbow
//
// From power-up until the host first connects, the macropad shows a static,
// dim rainbow running diagonally from the top-left corner (BOOT_HUE_START) to
// the bottom-right one (BOOT_HUE_END), introduced by a short animation picked
// from boot_anims[]. Once the host has connected the rainbow is done for this
// power cycle: a later host dropout falls back to the regular RGB effect, as
// before. Pressing any RGB key also dismisses it, so the regular effect stays
// reachable without a host.
//
// Like the sysmon bars, it dims through the global current with PWM at full
// scale, so the animations keep all 256 PWM levels at a low brightness.
//
// Every animation is a pure function of the time since it started: the
// indicator hook runs several times per frame (once per LED chunk), so no
// state may advance per call. Each one ends on the exact static frame.
//
// The pick is a shuffle bag over a boot counter kept in the EEPROM user word:
// every BOOT_ANIM_COUNT boots play each animation once, in a fresh order, and
// never the same one twice in a row. (Nothing here seeds random8(), so it
// would replay the same "random" sequence on every boot.) The counter is
// written back only after the animation, clear of USB enumeration.
// ---------------------------------------------------------------------------

#define BOOT_BRIGHTNESS 64 // 0..255, same scale as the host's brightness byte
#define BOOT_HUE_START 0   // top-left: red
#define BOOT_HUE_END 205   // bottom-right: violet (191 reads as blue on these LEDs)

// Numbered playback (replay previews): N white dots name variant N, then a
// short dark gap, the animation, and a hold on the finished rainbow.
#define BOOT_DOTS_MS 900
#define BOOT_GAP_MS 300
#define BOOT_HOLD_MS 1800

#define BOOT_N RGB_MATRIX_LED_COUNT

// Arrival fields, 0..255 per key: the order in which a sweep or a sparkle
// reaches it. boot_pos is also the rainbow coordinate.
static uint8_t boot_nx[BOOT_N];     // left -> right
static uint8_t boot_ny[BOOT_N];     // top -> bottom
static uint8_t boot_pos[BOOT_N];    // diagonal, top-left -> bottom-right
static uint8_t boot_center[BOOT_N]; // pad center -> outward
static uint8_t boot_rise[BOOT_N];   // bottom -> top
static uint8_t boot_ends[BOOT_N];   // both diagonal corners -> the middle
static uint8_t boot_hue[BOOT_N];    // finished rainbow hue

// Per-playback randomness, reseeded every time an animation starts.
static uint32_t boot_seed;
static uint32_t boot_base_seed; // per playback; each slot reseeds from it
static uint8_t  boot_seed_slot; // slot boot_seed was derived for
static uint8_t  boot_jitter[BOOT_N]; // 0..255
static uint8_t  boot_rank[BOOT_N];   // the keys in a shuffled order, 0..255

static bool     boot_armed     = true;  // start playing on the first frame
static bool     boot_playing   = false;
static bool     boot_dismissed = false; // an RGB key was pressed
static uint32_t boot_t0;
static uint8_t  boot_first;    // first variant of the playlist (0-based)
static uint8_t  boot_span;     // variants in the playlist
static uint8_t  boot_count;    // playlist slots (span x rounds)
static bool     boot_numbered; // dots + hold around each slot

static bool     boot_counter_loaded = false;
static bool     boot_counter_dirty  = false;
static uint32_t boot_counter; // power-ups so far (EEPROM user word)
static uint8_t  boot_powerup_pick = 0; // 1-based variant this power-up played

// lowbias32: a cheap, well-mixed 32-bit integer hash.
static uint32_t boot_hash32(uint32_t x) {
    x ^= x >> 16;
    x *= 0x7feb352dU;
    x ^= x >> 15;
    x *= 0x846ca68bU;
    x ^= x >> 16;
    return x;
}

// Fisher-Yates shuffle of 0..n-1, fully determined by seed.
static void boot_shuffle(uint8_t *a, uint8_t n, uint32_t seed) {
    for (uint8_t i = 0; i < n; i++) a[i] = i;
    for (uint8_t i = n - 1; i > 0; i--) {
        uint8_t j = (uint8_t)(boot_hash32(seed + i) % (i + 1));
        uint8_t x = a[i];
        a[i]      = a[j];
        a[j]      = x;
    }
}

static void boot_reseed(uint32_t seed) {
    uint8_t order[BOOT_N];
    boot_seed = seed;
    for (uint8_t i = 0; i < BOOT_N; i++) boot_jitter[i] = (uint8_t)boot_hash32(seed ^ (0x5bd1e995U * (i + 1)));
    boot_shuffle(order, BOOT_N, boot_hash32(seed + 0x2545F491U));
    for (uint8_t k = 0; k < BOOT_N; k++) boot_rank[order[k]] = (uint8_t)((uint16_t)k * 255 / (BOOT_N - 1));
}

static uint8_t boot_hue_at(int16_t pos) {
    if (pos < 0) pos = 0;
    if (pos > 255) pos = 255;
    return BOOT_HUE_START + (uint8_t)((int16_t)(BOOT_HUE_END - BOOT_HUE_START) * pos / 255);
}

// Stretch raw values over 0..255 (min -> 0, max -> 255).
static void boot_normalize(uint8_t *out, const uint16_t *raw) {
    uint16_t lo = 0xFFFF, hi = 0;
    for (uint8_t i = 0; i < BOOT_N; i++) {
        if (raw[i] < lo) lo = raw[i];
        if (raw[i] > hi) hi = raw[i];
    }
    if (hi == lo) hi = lo + 1;
    for (uint8_t i = 0; i < BOOT_N; i++) out[i] = (uint8_t)((uint32_t)(raw[i] - lo) * 255 / (hi - lo));
}

// Normalize both axes to 0..255 first: QMK's LED coordinates squeeze the
// macropad's 4x6 grid into 224x64, and the top row is half-height, so raw
// coordinates (or row/column indices) would tilt the stripes. Averaging the
// normalized axes puts the two off-diagonal corners on the same hue. The
// distance from the center uses true proportions instead (the outer column
// centers are 3 key pitches apart, the outer row centers ~4.74), so the
// center sparkle grows as a circle, not an ellipse.
static void boot_geometry_init(void) {
    uint8_t max_x = 1, max_y = 1;
    for (uint8_t i = 0; i < BOOT_N; i++) {
        if (g_led_config.point[i].x > max_x) max_x = g_led_config.point[i].x;
        if (g_led_config.point[i].y > max_y) max_y = g_led_config.point[i].y;
    }
    uint16_t center[BOOT_N], ends[BOOT_N];
    for (uint8_t i = 0; i < BOOT_N; i++) {
        boot_nx[i]   = (uint8_t)((uint16_t)g_led_config.point[i].x * 255 / max_x);
        boot_ny[i]   = (uint8_t)((uint16_t)g_led_config.point[i].y * 255 / max_y);
        boot_pos[i]  = (uint8_t)(((uint16_t)boot_nx[i] + boot_ny[i]) / 2);
        boot_rise[i] = 255 - boot_ny[i];
        boot_hue[i]  = boot_hue_at(boot_pos[i]);

        int32_t dx = ((int32_t)boot_nx[i] * 300 / 255 - 150) / 2; // half key-pitch/100 units
        int32_t dy = ((int32_t)boot_ny[i] * 474 / 255 - 237) / 2;
        center[i]  = sqrt16((uint16_t)(dx * dx + dy * dy));
        ends[i]    = boot_pos[i] < 128 ? boot_pos[i] : 255 - boot_pos[i];
    }
    boot_normalize(boot_center, center);
    boot_normalize(boot_ends, ends);
}

static void boot_set(uint8_t i, uint8_t hue, uint8_t sat, uint8_t val) {
    RGB rgb = hsv_to_rgb((HSV){hue, sat, val});
    rgb_matrix_set_color(i, rgb.r, rgb.g, rgb.b);
}

static void boot_render_static(void) {
    for (uint8_t i = 0; i < BOOT_N; i++) boot_set(i, boot_hue[i], 255, 255);
}

// Progress of t through the window [start, start+len) as 0..255, clamped.
static uint8_t boot_prog(int32_t t, int32_t start, int32_t len) {
    if (t <= start) return 0;
    if (t >= start + len) return 255;
    return (uint8_t)((t - start) * 255 / len);
}

// Fade-in level paced through sqrt (as sysmon_pulse): races through the coarse
// bottom PWM levels, eases through the fine bright ones.
static uint8_t boot_fade(uint8_t x) {
    return sqrt16((uint16_t)x * 255);
}

// a * b / 255, exact at both ends. scale8() is not: scale8(255, 255) = 254,
// which left keys a sweep hadn't reached yet at PWM 1 instead of off, and on
// some LEDs (the CS7-CS9 group: numpad 1, 2, 0, 0) PWM 1 shows as faint red.
static uint8_t boot_mul(uint8_t a, uint8_t b) {
    return (uint8_t)((uint16_t)a * b / 255);
}

static uint8_t boot_ease_out(uint8_t x) {
    uint8_t r = 255 - x;
    return 255 - boot_mul(r, r);
}

// a -> b by f/255, exact at both ends (lerp8by8 stops one short of b, the
// same PWM-1 residue as scale8).
static uint8_t boot_lerp(uint8_t a, uint8_t b, uint8_t f) {
    return (uint8_t)(a + ((int16_t)b - a) * f / 255);
}

static RGB boot_rgb_lerp(RGB a, RGB b, uint8_t f) {
    return (RGB){boot_lerp(a.r, b.r, f), boot_lerp(a.g, b.g, f), boot_lerp(a.b, b.b, f)};
}

// Smooth value noise, 0..255: hashed random values every period_ms, eased
// between, so a flicker glides instead of snapping. Varies per playback.
static uint8_t boot_noise(uint32_t key, uint16_t t, uint16_t period_ms) {
    uint32_t slot = t / period_ms;
    uint8_t  f    = (uint8_t)((uint32_t)(t % period_ms) * 255 / period_ms);
    uint8_t  a    = (uint8_t)boot_hash32(boot_seed ^ key ^ (slot * 0x9E3779B9U));
    uint8_t  b    = (uint8_t)boot_hash32(boot_seed ^ key ^ ((slot + 1) * 0x9E3779B9U));
    return lerp8by8(a, b, ease8InOutQuad(f));
}

// How far a front has passed a key at field value pos: 0 = not reached yet,
// 255 = w or more past it.
static uint8_t boot_behind(int16_t front, int16_t pos, int16_t w) {
    int16_t d = front - pos;
    if (d <= 0) return 0;
    if (d >= w) return 255;
    return (uint8_t)(d * 255 / w);
}

// --- building blocks -------------------------------------------------------

// A soft front of width w sweeping an arrival field over len ms. rag nudges
// each key's arrival by up to +-rag (per-playback jitter), for a ragged edge
// instead of a ruler-straight one. white makes the edge a white flare that
// cools into the colors behind it.
static void boot_sweep(uint16_t t, uint16_t len, const uint8_t *field, int16_t w, int16_t rag, bool white) {
    int16_t reach = w + rag;
    int16_t f     = (int16_t)(-reach + (int32_t)(255 + 2 * reach) * t / len);
    for (uint8_t i = 0; i < BOOT_N; i++) {
        int16_t at = field[i];
        if (rag) at += ((int16_t)boot_jitter[i] - 128) * rag / 128;
        uint8_t a = boot_behind(f, at, w);
        boot_set(i, boot_hue[i], white ? boot_mul(a, a) : 255, boot_ease_out(a));
    }
}

// One key's spark, dt ms after it ignites: dark before, an 80ms white flare,
// then 500ms cooling into its rainbow color.
static void boot_spark(uint8_t i, int16_t dt) {
    if (dt < 0) {
        boot_set(i, 0, 0, 0);
    } else if (dt < 80) {
        boot_set(i, boot_hue[i], 0, (uint8_t)(dt * 255 / 80));
    } else {
        boot_set(i, boot_hue[i], boot_prog(dt, 80, 500), 255);
    }
}

// Keys ignite in the order of an arrival field spread over spread_ms, each
// one nudged by up to 500ms of per-playback jitter. That jitter is what keeps
// it from looking mechanical, and it differs on every boot.
#define BOOT_TWINKLE_MS 2400
static void boot_twinkle(uint16_t t, const uint8_t *field, uint16_t spread_ms) {
    for (uint8_t i = 0; i < BOOT_N; i++) {
        int16_t at = (int16_t)((uint32_t)field[i] * spread_ms / 255 + (uint16_t)boot_jitter[i] * 500 / 255);
        boot_spark(i, (int16_t)t - at);
    }
}

// --- the variants ----------------------------------------------------------

// 1 Wipe: a soft diagonal edge sweeps top-left -> bottom-right, revealing it.
#define BOOT_WIPE_MS 1400
static void boot_anim_wipe(uint16_t t) {
    boot_sweep(t, BOOT_WIPE_MS, boot_pos, 80, 0, false);
}

// 2 Shine: like Wipe, but the edge is white and cools into the colors behind.
#define BOOT_SHINE_MS 1700
static void boot_anim_shine(uint16_t t) {
    boot_sweep(t, BOOT_SHINE_MS, boot_pos, 110, 0, true);
}

// 3 Spin: the rainbow flows along the diagonal two full turns, slowing down
// until it freezes in place.
#define BOOT_SPIN_MS 2400
static void boot_anim_spin(uint16_t t) {
    uint8_t  inv = 255 - boot_prog(t, 0, BOOT_SPIN_MS);
    uint32_t c   = (uint32_t)inv * inv * inv; // cubic deceleration
    uint8_t  off = (uint8_t)(c / 32385);      // 512 hue units at t = 0
    uint8_t  v   = boot_fade(boot_prog(t, 0, 400));
    for (uint8_t i = 0; i < BOOT_N; i++) boot_set(i, boot_hue[i] + off, 255, v);
}

// 4 Bloom: the pad fades in plain white, then the rainbow colors fade in
// (saturation rises) until the white is gone.
#define BOOT_BLOOM_MS 2000
static void boot_anim_bloom(uint16_t t) {
    uint8_t v = boot_fade(boot_prog(t, 0, 400));
    uint8_t s = ease8InOutCubic(boot_prog(t, 550, BOOT_BLOOM_MS - 550));
    for (uint8_t i = 0; i < BOOT_N; i++) boot_set(i, boot_hue[i], s, v);
}

// 5 Twinkle: keys ignite as white sparks roughly along the diagonal.
static void boot_anim_twinkle(uint16_t t) {
    boot_twinkle(t, boot_pos, 1300);
}

// 6 Rotate: the gradient's direction spins one and a half turns around the
// pad's center, slowing down until it settles on the diagonal.
#define BOOT_ROTATE_MS 2600
static void boot_anim_rotate(uint16_t t) {
    uint8_t inv   = 255 - boot_prog(t, 0, BOOT_ROTATE_MS);
    uint8_t theta = (uint8_t)(32 + (uint32_t)384 * inv * inv / 65025); // 32 = 45 deg
    int16_t c     = (int16_t)cos8(theta) - 128;
    int16_t s     = (int16_t)sin8(theta) - 128;
    uint8_t v     = boot_fade(boot_prog(t, 0, 400));
    for (uint8_t i = 0; i < BOOT_N; i++) {
        int32_t proj = ((int32_t)((int16_t)boot_nx[i] - 128) * c + (int32_t)((int16_t)boot_ny[i] - 128) * s) / 128;
        boot_set(i, boot_hue_at((int16_t)(128 + proj * 181 / 256)), 255, v);
    }
}

// 7 Twinkle, center: the sparks start in the middle of the pad and spread out.
static void boot_anim_twinkle_center(uint16_t t) {
    boot_twinkle(t, boot_center, 1300);
}

// 8 Twinkle, scatter: sparks land anywhere, in a fresh random order each time.
static void boot_anim_twinkle_scatter(uint16_t t) {
    boot_twinkle(t, boot_rank, 1300);
}

// 9 Fire: classic red/orange/yellow flames climb from the bottom row to the
// top, their tips flickering at uneven heights, while red embers break off the
// flames and shoot up well ahead of them, drifting sideways and burning out on
// the way. The flames are a band: behind it each key cools from flame yellow
// into its rainbow color and then holds still, so the burned part of the pad
// is already the static rainbow while the fire is still climbing.
//
// Heights are boot_rise units (0 = bottom row, 255 = top row; rows ~52 apart).
#define BOOT_FIRE_MS 2300
#define BOOT_FIRE_SPREAD_MS 2200 // every key has cooled by now
#define BOOT_FIRE_FLAME 110      // flame band: tip (red) -> base (yellow)
#define BOOT_FIRE_COOL 60        // behind the base: flame yellow -> rainbow, still
#define BOOT_EMBERS 14

// Starts low enough that no key is lit on the first frame, even with the
// worst-case column wobble (+35) and key jitter (-30); reaches 490 (every key
// cooled: 255 + 30 + 35 + FLAME + COOL) at BOOT_FIRE_SPREAD_MS.
static int16_t boot_fire_front(int32_t t) {
    return (int16_t)(-80 + 570 * t / BOOT_FIRE_SPREAD_MS);
}

static void boot_anim_fire(uint16_t t) {
    RGB     px[BOOT_N];
    int16_t front = boot_fire_front(t);

    for (uint8_t i = 0; i < BOOT_N; i++) {
        uint8_t col = (uint8_t)((boot_nx[i] + 42) / 85); // 0..3
        // Each column's flames reach their own, wandering height; each key
        // catches a little early or late.
        int16_t tip = front + ((int16_t)boot_noise(0xF1A0u + col, t, 90) - 128) * 35 / 128;
        int16_t h   = boot_rise[i] + ((int16_t)boot_jitter[i] - 128) * 30 / 128;
        int16_t d   = tip - h; // how far the fire has passed this key
        if (d <= 0) {
            px[i] = (RGB){0, 0, 0};
            continue;
        }
        RGB own = hsv_to_rgb((HSV){boot_hue[i], 255, 255});
        if (d >= BOOT_FIRE_FLAME + BOOT_FIRE_COOL) {
            px[i] = own; // burned out: the still rainbow
            continue;
        }
        // Flame: hue red (tip) -> orange -> yellow (base), paling a little
        // where it is hottest, with a small dancing hue wobble.
        uint8_t a   = d >= BOOT_FIRE_FLAME ? 255 : (uint8_t)(d * 255 / BOOT_FIRE_FLAME);
        int16_t hue = a * 40 / 255 + ((int16_t)boot_noise(0x77u + i, t, 110) - 128) * 5 / 128;
        if (hue < 0) hue = 0;
        RGB     c   = hsv_to_rgb((HSV){(uint8_t)hue, (uint8_t)(255 - a * 45 / 255), 255});
        uint8_t amp = 120; // flicker depth
        // Brightness: the tip fades in.
        uint8_t v = boot_ease_out(a < 90 ? (uint8_t)(a * 255 / 90) : 255);
        if (d > BOOT_FIRE_FLAME) {
            // Cooling: blend to the rainbow color as the flicker dies away.
            uint8_t k = (uint8_t)((d - BOOT_FIRE_FLAME) * 255 / BOOT_FIRE_COOL);
            c         = boot_rgb_lerp(c, own, k);
            amp       = boot_mul(amp, 255 - k);
        }
        v     = boot_mul(v, 255 - boot_mul(amp, boot_noise(i, t, 70)));
        px[i] = (RGB){boot_mul(c.r, v), boot_mul(c.g, v), boot_mul(c.b, v)};
    }

    // Embers: born at the flame tips at staggered, jittered moments, each
    // rising ~3x faster than the flames with its own speed, lifespan and
    // sideways drift; orange at birth, cooling to red as they fade.
    for (uint8_t e = 0; e < BOOT_EMBERS; e++) {
        uint32_t r1 = boot_hash32(boot_seed ^ (0xE0B0E000U + e));
        uint32_t r2 = boot_hash32(r1);
        uint16_t ts = 80 + (uint16_t)((uint32_t)e * 1250 / BOOT_EMBERS) + (r1 & 0x7F);
        if (t < ts) continue;
        uint16_t speed  = 650 + (uint16_t)((r1 >> 8) % 350);  // height units / s
        uint16_t life   = 150 + (uint16_t)((r1 >> 16) % 130); // height units of travel
        int16_t  travel = (int16_t)((uint32_t)(t - ts) * speed / 1000);
        if (travel >= (int16_t)life) continue;
        int16_t y0 = boot_fire_front(ts) - 25;
        if (y0 < 0) y0 = 0;
        int16_t y = y0 + travel;

        int16_t x = (int16_t)((r2 & 3) * 256) + ((int16_t)((r2 >> 8) & 0xFF) - 128) * 180 / 128 * travel / 255;
        if (x < 0) x = 0;
        if (x > 768) x = 768;

        uint8_t heat = (uint8_t)(255 - (int32_t)travel * 255 / life);
        RGB     ec   = hsv_to_rgb((HSV){(uint8_t)(heat * 16 / 255), 255, 255});

        for (uint8_t i = 0; i < BOOT_N; i++) {
            int16_t dx = (int16_t)((boot_nx[i] + 42) / 85) * 256 - x;
            if (dx < 0) dx = -dx;
            if (dx >= 256) continue;
            int16_t hk = boot_rise[i];
            uint8_t wy;
            if (hk <= y) { // the key below the head: a short fading tail
                if (y - hk >= 70) continue;
                wy = (uint8_t)(255 - (y - hk) * 255 / 70);
            } else {       // the key above: the head spills into it slightly
                if (hk - y >= 35) continue;
                wy = (uint8_t)(255 - (hk - y) * 255 / 35);
            }
            uint8_t w = boot_mul(boot_mul((uint8_t)((256 - dx) * 255 / 256), wy), heat);
            px[i].r   = qadd8(px[i].r, boot_mul(ec.r, w));
            px[i].g   = qadd8(px[i].g, boot_mul(ec.g, w));
            px[i].b   = qadd8(px[i].b, boot_mul(ec.b, w));
        }
    }

    for (uint8_t i = 0; i < BOOT_N; i++) rgb_matrix_set_color(i, px[i].r, px[i].g, px[i].b);
}

// 10 Ripple: a white, slightly ragged ring grows from the center outward and
// cools into the colors behind it.
#define BOOT_RIPPLE_MS 1800
static void boot_anim_ripple(uint16_t t) {
    boot_sweep(t, BOOT_RIPPLE_MS, boot_center, 110, 40, true);
}

// 11 Converge: two white, slightly ragged edges start at opposite corners and
// meet across the middle.
#define BOOT_CONVERGE_MS 1600
static void boot_anim_converge(uint16_t t) {
    boot_sweep(t, BOOT_CONVERGE_MS, boot_ends, 110, 30, true);
}

// 12 Flicker: each key sputters on like a warming neon tube, at its own random
// moment (drifting loosely along the diagonal), then holds.
#define BOOT_FLICKER_MS 2200
static void boot_anim_flicker(uint16_t t) {
    for (uint8_t i = 0; i < BOOT_N; i++) {
        int16_t dt = (int16_t)t - (int16_t)((uint16_t)boot_jitter[i] * 900 / 255 + (uint16_t)boot_pos[i] * 300 / 255);
        if (dt < 0) {
            boot_set(i, 0, 0, 0);
        } else if (dt >= 800) {
            boot_set(i, boot_hue[i], 255, 255);
        } else {
            // Each 40ms slot is on or off by a hash; on grows likelier as the
            // tube warms up. Some flickers only reach half brightness.
            uint32_t h    = boot_hash32(boot_seed ^ ((uint32_t)i << 16) ^ (uint32_t)(dt / 40));
            uint8_t  p_on = (uint8_t)(50 + (int32_t)dt * 205 / 800);
            if ((h & 0xFF) < p_on) {
                boot_set(i, boot_hue[i], 255, (h & 0x100) ? 255 : 120);
            } else {
                boot_set(i, 0, 0, 0);
            }
        }
    }
}

// 13 Lock-in: the whole pad sweeps once through the spectrum as one color, and
// each key freezes on its own color as the sweep reaches it, until the last
// one (violet) locks. The sweep starts a little before red, from pink, so even
// the red corner visibly arrives rather than being locked from the start.
#define BOOT_LOCKIN_MS 2400
#define BOOT_LOCKIN_PRE 32         // hue units of pre-roll before BOOT_HUE_START
#define BOOT_LOCKIN_SWEEP_AT 200   // the sweep runs from here...
#define BOOT_LOCKIN_SWEEP_MS 2100  // ...for this long (ends 100ms before the end)
static void boot_anim_lockin(uint16_t t) {
    int32_t s = (int32_t)t - BOOT_LOCKIN_SWEEP_AT;
    if (s < 0) s = 0;
    if (s > BOOT_LOCKIN_SWEEP_MS) s = BOOT_LOCKIN_SWEEP_MS;
    int16_t sweep = (int16_t)(BOOT_HUE_START - BOOT_LOCKIN_PRE +
                              (int32_t)(BOOT_HUE_END - BOOT_HUE_START + BOOT_LOCKIN_PRE) * s / BOOT_LOCKIN_SWEEP_MS);
    uint8_t v = boot_fade(boot_prog(t, 0, 300));
    for (uint8_t i = 0; i < BOOT_N; i++) {
        int16_t h = sweep < boot_hue[i] ? sweep : boot_hue[i]; // locked once reached
        boot_set(i, (uint8_t)h, 255, v);                        // (pre-roll wraps to pink)
    }
}

typedef struct {
    void (*render)(uint16_t t);
    uint16_t duration_ms;
} boot_anim_t;

static const boot_anim_t boot_anims[] = {
    {boot_anim_wipe, BOOT_WIPE_MS},                 // 1
    {boot_anim_shine, BOOT_SHINE_MS},               // 2
    {boot_anim_spin, BOOT_SPIN_MS},                 // 3
    {boot_anim_bloom, BOOT_BLOOM_MS},               // 4
    {boot_anim_twinkle, BOOT_TWINKLE_MS},           // 5
    {boot_anim_rotate, BOOT_ROTATE_MS},             // 6
    {boot_anim_twinkle_center, BOOT_TWINKLE_MS},    // 7
    {boot_anim_twinkle_scatter, BOOT_TWINKLE_MS},   // 8
    {boot_anim_fire, BOOT_FIRE_MS},                 // 9
    {boot_anim_ripple, BOOT_RIPPLE_MS},             // 10
    {boot_anim_converge, BOOT_CONVERGE_MS},         // 11
    {boot_anim_flicker, BOOT_FLICKER_MS},           // 12
    {boot_anim_lockin, BOOT_LOCKIN_MS},             // 13
};
#define BOOT_ANIM_COUNT ((uint8_t)(sizeof(boot_anims) / sizeof(boot_anims[0])))

// --- picking ---------------------------------------------------------------

// Which variant the n-th power-up plays: position n % COUNT of the shuffle
// for cycle n / COUNT. A cycle whose first pick repeats the previous cycle's
// last swaps its first two, so no variant ever plays twice in a row.
static uint8_t boot_bag_pick(uint32_t n) {
    uint8_t  cur[BOOT_ANIM_COUNT];
    uint32_t cycle = n / BOOT_ANIM_COUNT;
    boot_shuffle(cur, BOOT_ANIM_COUNT, boot_hash32(cycle));
    if (cycle > 0) {
        uint8_t prev[BOOT_ANIM_COUNT];
        boot_shuffle(prev, BOOT_ANIM_COUNT, boot_hash32(cycle - 1));
        if (cur[0] == prev[BOOT_ANIM_COUNT - 1]) {
            cur[0] = cur[1];
            cur[1] = prev[BOOT_ANIM_COUNT - 1];
        }
    }
    return cur[n % BOOT_ANIM_COUNT];
}

// Draw the next power-up pick and advance the counter (saved after playback).
static uint8_t boot_next_pick(void) {
    if (!boot_counter_loaded) {
        boot_counter_loaded = true;
        boot_counter        = eeconfig_read_user();
    }
    uint8_t v = boot_bag_pick(boot_counter);
    boot_base_seed = boot_hash32(boot_counter ^ 0xB0075EEDU);
    boot_counter++;
    boot_counter_dirty = true;
    return v;
}

// --- playback --------------------------------------------------------------

static void boot_start(uint8_t first, uint8_t span, uint8_t rounds, bool numbered) {
    if (span == 0) span = 1;
    if (rounds == 0) rounds = 1;
    if (rounds > 255 / span) rounds = 255 / span; // boot_count is 8-bit
    boot_first    = first;
    boot_span     = span;
    boot_count    = span * rounds;
    boot_numbered = numbered;
    boot_t0       = timer_read32();
    boot_playing  = true;
    boot_seed_slot = 0xFF; // reseed on the first frame
}

// BOOT REPLAY command: args[0] = first variant (1-based; 0 = all of them,
// 0xFE = a power-up pick, anything else past the end = stop), args[1] = last
// variant (0 = just the first), args[2] = rounds (0 = 1).
static void boot_replay(const uint8_t *args) {
    if (args[0] == 0xFD) return; // status query only
    boot_armed = false; // a replay before the first frame replaces the boot play
    if (args[0] == 0xFE) {
        boot_start(boot_next_pick(), 1, 1, false);
        return;
    }
    boot_base_seed = boot_hash32(timer_read32());
    uint8_t first = args[0], last = args[1] ? args[1] : args[0];
    if (first == 0) {
        first = 1;
        last  = BOOT_ANIM_COUNT;
    }
    if (last > BOOT_ANIM_COUNT) last = BOOT_ANIM_COUNT;
    if (first > BOOT_ANIM_COUNT || last < first) {
        boot_playing = false;
        return;
    }
    boot_start(first - 1, last - first + 1, args[2], true);
}

static void boot_put32(uint8_t *out, uint32_t v) {
    for (uint8_t b = 0; b < 4; b++) out[b] = (uint8_t)(v >> (8 * b));
}

// Status for the BOOT REPLAY echo (9 bytes; layout in the protocol notes).
static void boot_status(uint8_t *out) {
    boot_put32(out, boot_counter);
    out[4] = boot_powerup_pick;
    boot_put32(out + 5, eeconfig_read_user());
}

// Variant n's name tag: n white dots in reading order from the top-left key.
static void boot_render_dots(uint8_t n) {
    for (uint8_t i = 0; i < BOOT_N; i++) boot_set(i, 0, 0, 0);
    for (uint8_t k = 0; k < n && k < 24; k++) boot_set(sysmon_row_leds[k / 4][k % 4], 0, 0, 255);
}

// Length of one playlist slot showing variant v.
static uint32_t boot_slot_ms(uint8_t v) {
    uint32_t len = boot_anims[v].duration_ms;
    if (boot_numbered) len += BOOT_DOTS_MS + BOOT_GAP_MS + BOOT_HOLD_MS;
    return len;
}

// Render the current playlist position. False once it has finished.
static bool boot_render(void) {
    if (!boot_playing) return false;
    uint32_t t = timer_elapsed32(boot_t0);
    for (uint8_t k = 0; k < boot_count; k++) {
        uint8_t v = boot_first + k % boot_span;
        if (t < boot_slot_ms(v) && boot_seed_slot != k) {
            // Fresh randomness for every slot, so repeats differ. Keyed to
            // the slot, not the call: the hook runs several times a frame.
            boot_seed_slot = k;
            boot_reseed(boot_hash32(boot_base_seed + k));
        }
        if (boot_numbered) {
            if (t < BOOT_DOTS_MS) {
                boot_render_dots(v + 1);
                return true;
            }
            t -= BOOT_DOTS_MS;
            if (t < BOOT_GAP_MS) {
                for (uint8_t i = 0; i < BOOT_N; i++) boot_set(i, 0, 0, 0);
                return true;
            }
            t -= BOOT_GAP_MS;
        }
        uint16_t len = boot_anims[v].duration_ms;
        if (t < len) {
            boot_anims[v].render((uint16_t)t);
            return true;
        }
        t -= len;
        if (boot_numbered) {
            if (t < BOOT_HOLD_MS) {
                boot_render_static();
                return true;
            }
            t -= BOOT_HOLD_MS;
        }
    }
    boot_playing = false;
    return false;
}

// Any RGB key means the user wants the regular effect: drop the rainbow.
bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    if (record->event.pressed && (IS_RGB_KEYCODE(keycode) || keycode == BL_STEP)) {
        boot_dismissed = true;
        boot_playing   = false;
    }
    return true;
}

// Display priority, highest first:
//   1. a playing boot animation (it is short; finish it even if the host
//      connects meanwhile, and a replay preview must show over live data)
//   2. fresh host data: the sysmon bars
//   3. the host has never connected: the static boot rainbow
//   4. otherwise (host gone, or rainbow dismissed): the regular RGB effect
bool rgb_matrix_indicators_advanced_user(uint8_t led_min, uint8_t led_max) {
    (void)led_min;
    (void)led_max;

    static bool geometry_ready = false;
    if (!geometry_ready) {
        geometry_ready = true;
        boot_geometry_init();
    }
    if (boot_armed) {
        boot_armed        = false;
        boot_powerup_pick = boot_next_pick() + 1;
        boot_start(boot_powerup_pick - 1, 1, 1, false);
    }

    if (boot_render()) {
        sysmon_set_gcc(BOOT_BRIGHTNESS);
        return false;
    }
    if (boot_counter_dirty) {
        // Deferred until the animation is over, clear of USB enumeration
        // (a flash write stalls the MCU).
        boot_counter_dirty = false;
        eeconfig_update_user(boot_counter);
    }

    uint32_t timeout_ms = (uint32_t)sysmon_timeout_units * 100;
    if (sysmon_last_update == 0 || timer_elapsed32(sysmon_last_update) > timeout_ms) {
        if (sysmon_last_update == 0 && !boot_dismissed) {
            sysmon_set_gcc(BOOT_BRIGHTNESS);
            boot_render_static();
            return false;
        }
        // No fresh data from the host; restore the stock global current and
        // let the normal RGB effect run at its own brightness
        sysmon_set_gcc(255);
        return true;
    }

    sysmon_set_gcc(sysmon_slewed_val());

    uint32_t glide_ms = (uint32_t)sysmon_glide_units * 4;

    static sysmon_glide_t glides[6];
    static sysmon_glide_t ghost_glides[6];
    for (uint8_t row = 0; row < 6; row++) {
        sysmon_render_row(row, &glides[row], &ghost_glides[row], glide_ms, timeout_ms);
    }

    return false;
}

#endif // RGB_MATRIX_ENABLE
