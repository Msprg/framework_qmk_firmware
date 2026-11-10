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
//   row 4: dGPU usage (dimly lit from its last key while the dGPU is powered off)
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
#include "usb_util.h" // usb_connected_state: the boot counter is saved once USB is configured

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
#include <math.h>
#include "is31flcommon.h"
#include "hardware/flash.h" // the saved trace: erasing and programming the flash
#include "hardware/sync.h"

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
// Two FRAME reports are sent every tick, A then B. Each carries three 8-byte
// row descriptors back-to-back. FRAME B is what keeps the display alive: the
// bars show from the first FRAME B on, drawn with its globals.
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
//     +3 flags   bit0    anchor: 0 = fills from the left like the main bar,
//                        1 = from the right end (level 63 = the last key alone)
//                bits1-7 reserved (0)
//
//   FRAME C payload (data[3..26]) = ghost0[4] ++ ... ++ ghost5[4]
//   FRAME C echo: data[27] is set to SYSMON_GHOST_ACK, so the host can tell
//   ghost-capable firmware from older firmware (which echoes it unchanged),
//   and data[28] to SYSMON_GHOST_FLAGS_ACK: this firmware reads the flags
//   (firmware with ghosts but before the flags draws every ghost from the left).
//
// FRAME C is independent of A/B: it doesn't keep the display alive, and its
// ghosts fade out on their own if FRAME C stops arriving within the timeout,
// so an older host that never sends it can't leave a stale ghost on screen.
// Older firmware ignores value_id 0x03 and just draws the main bars.
//
// GOODBYE (value_id 0x04, no payload): the host is stopping on purpose (service
// stop, shutdown, Ctrl+C). The firmware hands over to the regular RGB effect at
// once. Without it, frames simply stop: after the timeout the macropad shows a
// disconnect warning (caution stripes) for SYSMON_WARN_MS, then falls back to
// the regular effect. The next FRAME B cancels either state.
//
// MESSAGE (value_id 0x05): a line of text for the user to read -- which limit
// holds a component back, say. The bars dim out, the text scrolls through on
// dark like an LED ticker, and the bars fade back in. It waits until the bars
// show and have settled, and is dropped if the host goes away first; it never
// plays over the boot rainbow or the disconnect warning. A new one replaces
// one waiting or playing.
//   data[3]     style: 0 = ticker (others reserved; played as the ticker)
//   data[4..5]  hue and saturation of the text (saturation 0 = white)
//   data[6]     speed, columns per second (0 = SYSMON_MSG_CPS)
//   data[7]     sequence number, 1..255, chosen by the host (0 = none)
//   data[8..30] text, NUL-terminated, up to 23 characters (the boot text's
//               font: A-Z, 0-9, space, - ! .). Empty: nothing happens (a probe).
//   echo: data[31] = SYSMON_MSG_ACK, so the host can tell message-capable
//   firmware from older firmware (which echoes it unchanged). Exactly 0x01:
//   hosts from before the outcome reports test it for equality. The probe's
//   echo also carries data[9] = SYSMON_MSG_SEQ_ACK (inside its empty text):
//   this firmware reports what became of each message (FRAME B, below).
// The FRAME B echo reports in data[31]: bit0 a message is waiting or playing,
// bit1 the replay key (keypad 5 with NumLock off) was pressed since the last
// FRAME B -- the host answers that with a MESSAGE --, bit2 the RGB matrix is
// switched off (RGB_TOG), so nothing the host sends shows, bit4 the pad is
// suspended (the keyboard's idle handling, lid closed): nothing shows either.
// data[30] is the sequence number of the last message played to its end,
// data[29] that of the last one dropped unplayed (replaced by a newer one,
// dropped as the pad went dark or the host went away); 0 = none since
// power-up. (Both overwrite host bytes the echo doesn't need back.) Older
// firmware echoes 0 in data[31] and the host's own data[29..30].
//
// REBOOT (value_id 0x11, no payload): restart the macropad as a power-up
// would, boot animation included -- for trying the startup without
// replugging. It goes about 300 ms after the echo; a boot pick still playing
// is saved first, so the next start moves on to the next one.
//
// TRACE (value_id 0x12): a flight recorder of what the LED chip was sent,
// every frame, for checking the pad's output without seeing it (see "LED
// trace" below; read out by bootanim_preview.py trace).
//   data[3] = 0 status, 1 freeze, 2 read, 3 restart (clear and record again),
//             4 read saved, 5 save (as holding the save key does)
//   read:   data[4..7] = byte offset (u32 LE) into the frozen frames, oldest
//           first; the echo carries the next 24 bytes in data[8..31]
//   read saved: data[4..7] = byte offset (u32 LE) into the saved traces (slot
//           k at k x 64 KiB: a 256-byte header, then its frames, oldest
//           first); the echo carries the next 24 bytes in data[8..31]
//   others: the echo carries frames held (u16 LE) in data[8..9], the frame
//           size in data[10], frozen in data[11], frames the ring holds
//           (u16 LE) in data[12..13], save slots in data[14], a save under
//           way in data[15], the slot size in KiB in data[16]
//
// BOOT REPLAY (value_id 0x10) replays boot animations, for previewing and
// tuning them. Each animation plays from dark and holds its finished rainbow
// a moment; a replay of several first names each with dots (N white dots
// before variant N), a replay of a single one (however many rounds) doesn't.
//   data[3] = first variant, 1-based (0 = all of them; 0xFE = play the next
//             power-up pick, unnumbered, exactly as a boot would; any other
//             value past the last variant stops a running replay)
//   data[4] = last variant (0 = only the first)
//   data[5] = rounds (0 = 1)
//   data[6..7] = speed, percent of normal (u16 LE; 0 = 100; 10..1000): the
//             animations run that much faster or slower (the dots, the dark
//             gap before and the hold after them don't)
//   (0xFD in data[3] changes nothing: a status query)
// The echo carries status in data[11..]: the power-up counter (u32 LE, as
// RAM holds it: already advanced past this boot's pick), the variant this
// power-up picked (1-based, 0 = none yet), and the counter as stored in
// EEPROM (u32 LE; lags by one until this boot's animation has finished).
// data[20] is what the display shows: 0 = regular RGB effect, 1 = boot
// rainbow, 2 = sysmon bars, 3 = disconnect warning, 4 = nothing (the lighting
// off, or the pad suspended). data[21] is 1 while an
// animation plays, data[22] while a MESSAGE waits or plays. data[23] is the
// firmware's capabilities (SYSMON_CAPS_*; 0 from firmware before them) and
// data[24] is 1 while the lighting is on, 0 while it is off (sysmon_lights_off).
// It never counts as host data, so it doesn't end the pre-connect rainbow;
// the 0xFD query changes nothing at all, which makes it the self-test's probe.
//
// VIA echoes the buffer back, which serves as the ACK.
// ---------------------------------------------------------------------------

#define SYSMON_CHANNEL 0x42
#define SYSMON_FRAME_A 0x01 // rows 0,1,2
#define SYSMON_FRAME_B 0x02 // rows 3,4,5 + globals
#define SYSMON_FRAME_C 0x03 // ghost bars, rows 0..5
#define SYSMON_GOODBYE 0x04 // host stopping on purpose: no disconnect warning
#define SYSMON_MESSAGE 0x05 // a line of text scrolled over the bars
#define SYSMON_BOOT_REPLAY 0x10 // replay the boot animation (preview/tuning)
#define SYSMON_REBOOT 0x11 // restart the macropad (testing the startup)
#define SYSMON_TRACE 0x12  // LED trace: freeze / read / restart

#define SYSMON_ROW_BYTES 8   // per-row descriptor size in the payload
#define SYSMON_GHOST_BYTES 4 // per-row ghost descriptor size in FRAME C
#define SYSMON_GHOST_ACK 0x01 // written into the FRAME C echo at data[27]
#define SYSMON_GHOST_FLAGS_ACK 0x01 // ...and this at data[28]: ghost flags are read
#define SYSMON_GHOST_RIGHT (1 << 0) // ghost flags bit0: the ghost fills from the right end
#define SYSMON_MSG_ACK 0x01   // written into the MESSAGE echo at data[31]
#define SYSMON_MSG_SEQ_ACK 0x02 // in the probe's echo at data[9]: FRAME B reports message outcomes
#define SYSMON_STATUS_MSG 0x01 // FRAME B echo data[31]: a message is waiting or playing
#define SYSMON_STATUS_ASK 0x02 // ... and the replay key was pressed since the last one
#define SYSMON_STATUS_DARK 0x04 // ... and the lighting is off (RGB_TOG, or VIA's All Off)
#define SYSMON_STATUS_SUSPENDED 0x10 // ... and the pad is suspended (lid closed): nothing shows
// BOOT REPLAY status echo data[23]: what this firmware can do, so a self-test
// can tell without sending frames (which would show). Bit 7 marks the byte as
// written: older firmware echoes it as sent, 0.
#define SYSMON_CAPS_VALID 0x80
#define SYSMON_CAPS_GHOSTS 0x01       // draws FRAME C ghost bars
#define SYSMON_CAPS_GHOST_FLAGS 0x02  // ...and reads their flags
#define SYSMON_CAPS_MESSAGES 0x04     // shows a MESSAGE
#define SYSMON_CAPS (SYSMON_CAPS_VALID | SYSMON_CAPS_GHOSTS | SYSMON_CAPS_GHOST_FLAGS | SYSMON_CAPS_MESSAGES)

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

// Defaults until the first FRAME B arrives (the bars don't show before it);
// they match the host defaults (300ms glide => 75, 5000ms timeout => 50).
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
    uint8_t flags; // SYSMON_GHOST_RIGHT; the rest reserved
} sysmon_ghost_t;

static sysmon_row_t   sysmon_rows[6];
static sysmon_ghost_t sysmon_ghosts[6];
static uint8_t        sysmon_brightness     = 255;
static uint8_t        sysmon_glide_units    = SYSMON_DEFAULT_GLIDE_UNITS;
static uint8_t        sysmon_timeout_units  = SYSMON_DEFAULT_TIMEOUT_UNITS;
// The timer now, as a timestamp where 0 means "never": 0 itself becomes the
// millisecond before it (UINT32_MAX), never one after. (Not timer_read32() | 1
// -- nor 0 -> 1 --, which is 1 ms in the future: an elapsed time read in that
// same millisecond wraps to ~4.29e9.)
static uint32_t sysmon_stamp(void) {
    uint32_t t = timer_read32();
    return t ? t : UINT32_MAX;
}

static uint32_t       sysmon_last_update    = 0;
static uint32_t       sysmon_ghost_update   = 0; // last FRAME C; 0 = never
static bool           sysmon_goodbye        = false; // host said it is stopping
static bool           sysmon_quiet          = false; // suspended since the last frame

// What the display showed last, so one can blend into the next.
#define DISP_EFFECT 0 // the regular RGB effect
#define DISP_BOOT 1   // boot animation / static rainbow
#define DISP_SYSMON 2 // the bars
#define DISP_WARN 3   // the disconnect warning
#define DISP_DARK 4   // dark: a message's end, or frames that stopped (the matrix
                      // switched off, a suspend); only ever where a fade into the bars starts
static uint8_t sysmon_disp = DISP_EFFECT;
static bool    sysmon_went_dark = false; // frames stopped since the last one (see sysmon_indicators)
static uint32_t usb_up_since     = 0;     // USB went ACTIVE then (0: not now); see housekeeping

// The lighting is off: the matrix switched off -- RGB_TOG, or VIA's "All Off"
// (which disables it too; kept across restarts once VIA saves). QMK then skips the
// indicator hook, where all of the display lives. (A "none" mode can't be
// current: QMK keeps the mode at 1 or above.)
static bool sysmon_lights_off(void) {
    return !rgb_matrix_is_enabled();
}

// Nothing is drawn: the lighting is off, or the pad is suspended.
static bool sysmon_dark(void) {
    return sysmon_lights_off() || rgb_matrix_get_suspend_state();
}

// MESSAGE: the text and how it looks; it is drawn with the bars, further down.
static char     msg_text[24];
static uint8_t  msg_hue, msg_sat, msg_cps;
static bool     msg_waiting = false; // received; plays once the bars have settled
static bool     msg_playing = false;
static uint32_t msg_t0;
static bool     sysmon_ask = false; // the replay key was pressed, not yet reported
static uint8_t  msg_seq         = 0; // the waiting or playing message's sequence number
static uint8_t  msg_played_seq  = 0; // the last one played to its end
static uint8_t  msg_dropped_seq = 0; // the last one dropped unplayed

// The message waiting or playing goes unplayed (dropped, or replaced): report
// its sequence number in the FRAME B echo, so the host names that limit again.
static void sysmon_msg_report_drop(void) {
    if ((msg_waiting || msg_playing) && msg_seq) msg_dropped_seq = msg_seq;
}

// Something else takes the display for a while: a message waits it out, and
// one cut off mid-play restarts once the bars are back.
static void sysmon_msg_hold(void) {
    if (msg_playing) {
        msg_playing = false;
        msg_waiting = true;
    }
}

static void sysmon_msg_drop(void) {
    sysmon_msg_report_drop();
    msg_waiting = msg_playing = false;
    msg_seq                   = 0;
}
static void     sysmon_message(const uint8_t *p);

#define SYSMON_WARN_STYLES 5
static uint8_t warn_prev_first = 0, warn_prev_last = 0; // warning-style preview; 0 = none

// Defined with the boot rainbow below.
static void boot_replay(const uint8_t *args);
static void boot_status(uint8_t *out);
static bool warn_prev_started;
static uint32_t sysmon_reboot_at = 0; // nonzero: REBOOT asked for at this time
static void     sysmon_trace_cmd(uint8_t *p); // with the LED trace, below
#define SYSMON_SAVED_BY_KEY 1  // why a trace was saved: the save key,
#define SYSMON_SAVED_BY_HOST 2 // ...or TRACE 5
static void     sysmon_trace_save_start(uint8_t why);
static void     sysmon_trace_save_step(bool usb_settled);

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
            // Rows only: the bars wait for FRAME B, which the host sends right
            // after it with the brightness, glide and timeout they are drawn
            // with. (Shown on FRAME A alone, a frame drawn in the ms before
            // FRAME B started the bars at full brightness, and they slid down
            // to the host's through the fade, swelling and sinking.)
            sysmon_decode_row(0, payload + 0 * SYSMON_ROW_BYTES);
            sysmon_decode_row(1, payload + 1 * SYSMON_ROW_BYTES);
            sysmon_decode_row(2, payload + 2 * SYSMON_ROW_BYTES);
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_FRAME_B) {
            sysmon_decode_row(3, payload + 0 * SYSMON_ROW_BYTES);
            sysmon_decode_row(4, payload + 1 * SYSMON_ROW_BYTES);
            sysmon_decode_row(5, payload + 2 * SYSMON_ROW_BYTES);
            // globals follow the three row descriptors (payload[24..26])
            sysmon_brightness    = payload[3 * SYSMON_ROW_BYTES + 0];
            sysmon_glide_units   = payload[3 * SYSMON_ROW_BYTES + 1];
            sysmon_timeout_units = payload[3 * SYSMON_ROW_BYTES + 2];
            sysmon_last_update   = sysmon_stamp(); // 0 means never
            sysmon_goodbye       = false;
            sysmon_quiet         = false;
            // A message is for the bars only. With nothing drawn one would
            // wait to play, stale, when the pad is lit again (and hold off
            // every message behind it): drop it (the host is told).
            if (sysmon_dark()) sysmon_msg_drop();
            // Status for the host, in the echo (data[31]); a key press is
            // reported once. The message outcomes go in data[29..30], past
            // the globals already read above.
            payload[28] = (uint8_t)((msg_waiting || msg_playing ? SYSMON_STATUS_MSG : 0) | (sysmon_ask ? SYSMON_STATUS_ASK : 0) |
                                    (sysmon_lights_off() ? SYSMON_STATUS_DARK : 0) |
                                    (rgb_matrix_get_suspend_state() ? SYSMON_STATUS_SUSPENDED : 0));
            payload[26] = msg_dropped_seq; // data[29]
            payload[27] = msg_played_seq;  // data[30]
            sysmon_ask  = false;
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_MESSAGE) {
            sysmon_message(payload);
            payload[28] = SYSMON_MSG_ACK; // data[31], past the longest text
            if (!payload[5]) payload[6] = SYSMON_MSG_SEQ_ACK; // the probe's: data[9], in its empty text
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_FRAME_C) {
            for (uint8_t row = 0; row < 6; row++) {
                const uint8_t *src = payload + row * SYSMON_GHOST_BYTES;
                sysmon_ghosts[row].level = src[0];
                // A ghost glides out as it was: going to 0, it keeps its color
                // and anchor (a right-anchored one would jump to the left end).
                if (src[0]) {
                    sysmon_ghosts[row].hue   = src[1];
                    sysmon_ghosts[row].val   = src[2];
                    sysmon_ghosts[row].flags = src[3];
                }
            }
            sysmon_ghost_update = sysmon_stamp(); // 0 means never
            // VIA echoes this buffer back: mark it so the host can detect
            // ghost support (payload[24] = data[27], past the descriptors),
            // and that the ghost flags are read (payload[25] = data[28]).
            payload[6 * SYSMON_GHOST_BYTES]     = SYSMON_GHOST_ACK;
            payload[6 * SYSMON_GHOST_BYTES + 1] = SYSMON_GHOST_FLAGS_ACK;
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_GOODBYE) {
            sysmon_goodbye = true;
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_REBOOT) {
            sysmon_reboot_at = sysmon_stamp(); // restart once the echo is out
        } else if (*command_id == id_custom_set_value && *value_id == SYSMON_TRACE) {
            sysmon_trace_cmd(payload);
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
//
// The map is val squared, not linear: PWM dimming is linear in light output,
// but GCC dims by LED current, and LEDs convert low currents to light more
// efficiently, so a linear map runs visibly too bright at the dim end.
//
// A GCC write takes effect at once, but a PWM frame takes about 5 ms to
// stream out (198 registers over 400 kHz I2C), after the frame has been
// drawn. Written at the start of a frame, as it
// used to be, a rising GCC lit the previous frame at the new current until
// the new PWM arrived: a visible flash when the dim boot rainbow handed over
// to brighter bars. So a frame only requests its GCC
// (sysmon_write_gcc), and it goes out together with the frame's PWM once the
// frame is drawn (sysmon_gcc_commit), ordered so the in-between state is a
// brief dip, never a flash.
static uint8_t sysmon_gcc      = ISSI_GLOBALCURRENT; // on the chip
static uint8_t sysmon_gcc_next = ISSI_GLOBALCURRENT; // goes out with this frame
static uint8_t sysmon_gcc_bars = ISSI_GLOBALCURRENT; // what the bars are drawn for

static uint8_t sysmon_gcc_for(uint8_t val) {
    uint8_t gcc = (uint8_t)((uint32_t)val * val * ISSI_GLOBALCURRENT / (255UL * 255));
    if (gcc == 0 && val > 0) gcc = 1; // host wants light; GCC 0 is fully off
    return gcc;
}

static void sysmon_write_gcc(uint8_t gcc) {
    sysmon_gcc_next = gcc;
}

// Called once the frame is fully drawn. Rising: send the PWM (drawn for the
// higher current, so dimmer) first, then raise the current. Falling: lower
// the current first, then send the PWM. Either way, keys whose PWM is out of
// step with the current for those few ms run dim, never bright. The regular
// flush that follows finds nothing left to send.
static uint8_t sysmon_trace_change; // this frame's GCC change, for the trace: 1 rising, 2 falling
static bool    sysmon_gcc_flushed;  // this frame's commit sent the PWM itself

static void sysmon_gcc_commit(void) {
    uint8_t g = sysmon_gcc_next;
    if (g == sysmon_gcc) return;
    bool rising         = g > sysmon_gcc;
    sysmon_trace_change = rising ? 1 : 2;
    sysmon_gcc_flushed  = true;
    if (rising) rgb_matrix_driver.flush();
    sysmon_gcc = g;
    IS31FL_unlock_register(DRIVER_ADDR_1, ISSI_PAGE_FUNCTION);
    IS31FL_write_single_register(DRIVER_ADDR_1, ISSI_REG_GLOBALCURRENT, g);
    if (!rising) rgb_matrix_driver.flush();
}

// One frame's step of the chip's current toward target: about an eighth of
// the way there, at least 1, so each step's brief dip stays small.
static uint8_t sysmon_gcc_toward(uint8_t target) {
    uint8_t g = sysmon_gcc, step = MAX(g / 8, 1);
    if (target > g) return target - g > step ? g + step : target;
    if (target < g) return g - target > step ? g - step : target;
    return g;
}

static void sysmon_set_gcc(uint8_t val) {
    sysmon_write_gcc(sysmon_gcc_for(val));
}

// The bars render into this frame first, so the frame can be blended with
// whatever the display showed before (see sysmon_flush) instead of cutting.
static RGB sysmon_fb[RGB_MATRIX_LED_COUNT];

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
    uint16_t fl = 128 / MAX(sysmon_gcc_bars, 1);
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
// A right-anchored ghost (ghost_right) fills from the row's last segment
// leftward, so it only shows in what the main bar leaves unfilled at that end.
static void sysmon_render_bar(uint8_t row, uint8_t level, sysmon_color_mode_t mode,
                              uint8_t hue0, uint8_t hue1, uint8_t row_val,
                              uint8_t ghost_level, uint8_t ghost_hue, uint8_t ghost_val,
                              bool ghost_right) {
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

        // From the left, the main fill hides the first `fill` of the ghost's
        // coverage; from the right, the two meet from opposite ends, so the
        // ghost keeps what the main fill leaves of the segment.
        uint16_t gfill = sysmon_seg_fill(glvl, ghost_right ? 3 - i : i);
        uint16_t gpart = ghost_right ? MIN(gfill, 255 - fill) : (gfill > fill ? gfill - fill : 0);
        if (gpart) { // 1..255: ghost coverage past the main fill
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
        sysmon_fb[sysmon_row_leds[row][i]] = rgb;
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

// For the first SYSMON_SNAP_MS of a fade into the bars, every bar and ghost
// jumps to its level, so the fade heads for what the bars actually show
// instead of for bars still growing from empty (or from where they stood
// before the warning). A window, not one frame: FRAME C's ghosts arrive a
// few ms after the bars have started. By its end the fade is 5% under way.
#define SYSMON_SNAP_MS 100
static uint32_t sysmon_entered;    // when the bars last took over the display
static bool     sysmon_glide_snap; // set while drawing inside that window

static uint8_t sysmon_glide(sysmon_glide_t *g, uint8_t target, uint32_t glide_ms) {
    if (glide_ms == 0) glide_ms = 1;
    uint32_t now = timer_read32();
    if (sysmon_glide_snap) {
        g->shown = g->start = g->target = target;
        g->t0                           = now;
        return target;
    }
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
// landing mid-pulse can't step the whole display's brightness. jump: go
// straight there instead (the bars are just taking over: a slide would run
// through the fade into them, the bars swelling or sinking as it went).
static uint8_t sysmon_slewed_val(bool jump) {
    static uint8_t  val  = 0;
    static uint32_t last = 0;

    uint32_t now = timer_read32();
    if (jump) {
        val  = sysmon_brightness;
        last = now;
        return val;
    }
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
                      ghost_level, gh->hue, ghost_val, gh->flags & SYSMON_GHOST_RIGHT);

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
            sysmon_fb[sysmon_row_leds[row][idx]] = rgb;
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
// before. Pressing any key also dismisses it (and cuts its animation short),
// so without a host the pad goes to the regular effect at the first touch.
//
// Like the sysmon bars, it dims through the global current with PWM at full
// scale, so the animations keep all 256 PWM levels at a low brightness.
//
// Every animation is a pure function of the time since it started, so a
// replay at another speed, a dropped frame or a trace replay all show the same
// thing. Each one ends on the exact static frame.
//
// The pick is a shuffle bag over a boot counter kept in the EEPROM user word:
// every BOOT_POOL_COUNT boots play each pooled animation once, in a fresh order, and
// never the same one twice in a row. (Nothing here seeds random8(), so it
// would replay the same "random" sequence on every boot.) The counter is
// written back once the animation is over and USB is configured, clear of
// USB enumeration (see housekeeping_task_user).
// ---------------------------------------------------------------------------

// 0..255, same scale as the host's brightness byte. The scale is squared on the
// way to the global current: 33 is GCC 3, about half as bright to the eye as
// the earlier 64 (GCC 11). (32 would truncate to GCC 2.)
#define BOOT_BRIGHTNESS 33
#define BOOT_SAVE_SETTLE_MS 2000 // the counter is saved this long after USB came up, at the soonest
#define BOOT_SAVE_NO_USB_MS 30000 // ...or, with USB never configured, this long after power-up
#define BOOT_HUE_START 0   // top-left: red
#define BOOT_HUE_END 205   // bottom-right: violet (191 reads as blue on these LEDs)

// Replay previews: a short dark gap, the animation, and a hold on the
// finished rainbow; a replay of several first names each with N white dots.
#define BOOT_DOTS_MS 900
#define BOOT_GAP_MS 300
#define BOOT_HOLD_MS 1800

#define BOOT_N RGB_MATRIX_LED_COUNT

// Arrival fields, 0..255 per key: the order in which a sweep or a sparkle
// reaches it. boot_pos is also the rainbow coordinate.
static uint8_t boot_nx[BOOT_N];     // left -> right
static uint8_t boot_ny[BOOT_N];     // top -> bottom
static uint8_t boot_pos[BOOT_N];    // diagonal, top-left -> bottom-right
static uint8_t boot_rise[BOOT_N];   // bottom -> top
static uint8_t boot_ends[BOOT_N];   // both diagonal corners -> the middle
static uint8_t boot_hue[BOOT_N];    // finished rainbow hue
static RGB     boot_own[BOOT_N];    // ...and as RGB

// Grid and true-proportion position of each key: column 0..3, row 0..5 (top
// = 0), and x/y in hundredths of a key pitch (x 0..300, y 0..474; the top row
// is half-height, so it sits closer to row 1).
static uint8_t  boot_row[BOOT_N];
static uint8_t  boot_col[BOOT_N];
static uint16_t boot_px[BOOT_N];
static uint16_t boot_py[BOOT_N];
#define BOOT_KEY(r, c) (sysmon_row_leds[(r)][(c)])

// Per-playback randomness, reseeded every time an animation starts.
static uint32_t boot_seed;
static uint32_t boot_base_seed; // per playback; each slot reseeds from it
static uint8_t  boot_seed_slot; // slot boot_seed was derived for
static uint8_t  boot_jitter[BOOT_N]; // 0..255
static uint8_t  boot_rank[BOOT_N];   // the keys in a shuffled order, 0..255

static bool     boot_armed     = true;  // start playing on the first frame
static bool     boot_playing   = false;
static bool     boot_dismissed = false; // a key was pressed
static uint32_t boot_t0;
static uint8_t  boot_first;    // first variant of the playlist (0-based)
static uint8_t  boot_span;     // variants in the playlist
static uint8_t  boot_count;    // playlist slots (span x rounds)
static bool     boot_framed;   // a dark gap before and a hold after each slot
static bool     boot_dots;     // and, before the gap, dots naming the variant
static uint16_t boot_speed = 100; // a replay's animation speed, percent of normal

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
    uint16_t ends[BOOT_N];
    for (uint8_t i = 0; i < BOOT_N; i++) {
        boot_nx[i]   = (uint8_t)((uint16_t)g_led_config.point[i].x * 255 / max_x);
        boot_ny[i]   = (uint8_t)((uint16_t)g_led_config.point[i].y * 255 / max_y);
        boot_pos[i]  = (uint8_t)(((uint16_t)boot_nx[i] + boot_ny[i]) / 2);
        boot_rise[i] = 255 - boot_ny[i];
        boot_hue[i]  = boot_hue_at(boot_pos[i]);
        ends[i]      = boot_pos[i] < 128 ? boot_pos[i] : 255 - boot_pos[i];
    }
    boot_normalize(boot_ends, ends);

    for (uint8_t r = 0; r < 6; r++) {
        for (uint8_t c = 0; c < 4; c++) {
            uint8_t i   = BOOT_KEY(r, c);
            boot_row[i] = r;
            boot_col[i] = c;
        }
    }
    for (uint8_t i = 0; i < BOOT_N; i++) {
        boot_px[i]  = (uint16_t)((uint32_t)boot_nx[i] * 300 / 255);
        boot_py[i]  = (uint16_t)((uint32_t)boot_ny[i] * 474 / 255);
        boot_own[i] = hsv_to_rgb((HSV){boot_hue[i], 255, 255});
    }
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

// A key's color at saturation s (0 = white) and value v, for a white flash
// cooling into it. Warm hues (red to yellow) cool the way glowing metal does,
// through cream and orange, and fast: what is left of the extra green goes as
// (1-s)^4, of the blue as (1-s)^8 (so blue always leads). The plain way (the
// other channels evenly, linearly) took red through pink, and red and orange
// keys looked pale and late next to the green ones: those keep their (very
// bright) green and only lose red and blue, so they look colored almost at
// once, while a red or orange key looks whitish until nearly all its extra
// green is gone. Other hues go the plain way: a pale tint of themselves,
// which reads as the same color. Full saturation is exactly the key's
// rainbow color either way.
static RGB boot_cool(uint8_t hue, uint8_t s, uint8_t v) {
    RGB c = hsv_to_rgb((HSV){hue, 255, 255});
    if (c.r == 255 && c.b == 0) {
        uint8_t left2 = boot_mul(255 - s, 255 - s), left4 = boot_mul(left2, left2);
        c.g           = (uint8_t)(c.g + boot_mul(255 - c.g, left4));
        c.b           = boot_mul(left4, left4);
    } else {
        c = hsv_to_rgb((HSV){hue, s, 255});
    }
    return (RGB){.r = boot_mul(c.r, v), .g = boot_mul(c.g, v), .b = boot_mul(c.b, v)};
}

// a -> b by f/255, exact at both ends (lerp8by8 stops one short of b, the
// same PWM-1 residue as scale8).
static uint8_t boot_lerp(uint8_t a, uint8_t b, uint8_t f) {
    return (uint8_t)(a + ((int16_t)b - a) * f / 255);
}

// QMK's RGB is laid out g, r, b (the WS2812 byte order), so an RGB must be
// built with named fields: a positional (RGB){r, g, b} swaps red and green.
static RGB boot_rgb_lerp(RGB a, RGB b, uint8_t f) {
    return (RGB){.r = boot_lerp(a.r, b.r, f), .g = boot_lerp(a.g, b.g, f), .b = boot_lerp(a.b, b.b, f)};
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
        RGB c = boot_cool(boot_hue[i], boot_prog(dt, 80, 500), 255);
        rgb_matrix_set_color(i, c.r, c.g, c.b);
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

// 7 Twinkle, scatter: sparks land anywhere, in a fresh random order each time.
static void boot_anim_twinkle_scatter(uint16_t t) {
    boot_twinkle(t, boot_rank, 1300);
}

// 8 Fire: classic red/orange/yellow flames climb from the bottom row to the
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
        px[i] = (RGB){.r = boot_mul(c.r, v), .g = boot_mul(c.g, v), .b = boot_mul(c.b, v)};
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

// 9 Converge: two white, slightly ragged edges start at opposite corners and
// meet across the middle.
#define BOOT_CONVERGE_MS 1600
static void boot_anim_converge(uint16_t t) {
    boot_sweep(t, BOOT_CONVERGE_MS, boot_ends, 110, 30, true);
}

// 10 Flicker: each key sputters on like a warming neon tube, at its own random
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

// 11 Lock-in: the whole pad sweeps once through the spectrum as one color, and
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

// --- palette 2 -------------------------------------------------------------
//
// Deliberately unlike palette 1's sweeping fronts: discrete events, particles
// with momentum or gravity, per-column motion, light-source metaphors, things
// that scramble and settle. Same rules: pure functions of t (random layouts
// are rebuilt from boot_seed when it changes, never advanced per call), dark
// is exactly 0, and each one ends on the exact static frame.

static RGB boot_fb[BOOT_N]; // additive frame buffer for particle effects

static void boot_fb_clear(void) {
    for (uint8_t i = 0; i < BOOT_N; i++) boot_fb[i] = (RGB){0, 0, 0};
}

static void boot_fb_add(uint8_t i, RGB c, uint8_t w) {
    boot_fb[i].r = qadd8(boot_fb[i].r, boot_mul(c.r, w));
    boot_fb[i].g = qadd8(boot_fb[i].g, boot_mul(c.g, w));
    boot_fb[i].b = qadd8(boot_fb[i].b, boot_mul(c.b, w));
}

static void boot_fb_flush(void) {
    for (uint8_t i = 0; i < BOOT_N; i++) rgb_matrix_set_color(i, boot_fb[i].r, boot_fb[i].g, boot_fb[i].b);
}

static RGB boot_rgb_scale(RGB c, uint8_t v) {
    return (RGB){.r = boot_mul(c.r, v), .g = boot_mul(c.g, v), .b = boot_mul(c.b, v)};
}

static RGB boot_hsv(uint8_t h, uint8_t s, uint8_t v) {
    return hsv_to_rgb((HSV){h, s, v});
}

// A per-playback random number, stable for a given key.
static uint32_t boot_rnd(uint32_t key) {
    return boot_hash32(boot_seed ^ (key * 0x9E3779B9U + 0x7F4A7C15U));
}

// True when an animation's precomputed layout must be rebuilt: another
// animation used the cache last, or the seed changed (new boot / new round).
static bool boot_cache_stale(uint8_t id) {
    static uint32_t seed  = 0;
    static uint8_t  owner = 0xFF;
    if (owner == id && seed == boot_seed) return false;
    owner = id;
    seed  = boot_seed;
    return true;
}

// Distance between two points in key-pitch/100 units (sqrt16 needs halving:
// the squared diagonal of the pad overflows 16 bits).
static uint16_t boot_dist(int32_t dx, int32_t dy) {
    dx /= 2;
    dy /= 2;
    return (uint16_t)(sqrt16((uint16_t)MIN(dx * dx + dy * dy, 65535)) * 2);
}

// 12 Fireworks: rockets rise from the bottom and burst; each burst lights the
// keys around it (a white pop settling into their colors) and throws sparks
// that arc under gravity. A big finale lights the rest.
#define BOOT_FW_MS 2800
#define BOOT_FW_SHELLS 4
#define BOOT_FW_SPARKS 8
#define BOOT_FW_RISE 380
static uint16_t boot_fw_launch[BOOT_FW_SHELLS];
static uint8_t  boot_fw_col[BOOT_FW_SHELLS];
static uint16_t boot_fw_y[BOOT_FW_SHELLS];
static uint16_t boot_fw_radius[BOOT_FW_SHELLS];
static uint8_t  boot_fw_hue[BOOT_FW_SHELLS];
static uint16_t boot_fw_reveal[BOOT_N];

static void boot_anim_fireworks(uint16_t t) {
    static const uint16_t row_y[6] = {0, 72, 176, 273, 369, 474};
    if (boot_cache_stale(16)) {
        uint8_t prev = 0xFF;
        for (uint8_t s = 0; s < BOOT_FW_SHELLS; s++) {
            uint32_t h        = boot_rnd(0xF14Eu + s);
            bool     finale   = s == BOOT_FW_SHELLS - 1;
            uint8_t  col      = finale ? 1 + (h & 1) : (uint8_t)((h >> 4) % 4);
            if (!finale && col == prev) col = (col + 1 + (h >> 8) % 3) % 4;
            prev              = col;
            boot_fw_col[s]    = col;
            boot_fw_launch[s] = (uint16_t)(100 + s * 490 + (h >> 12) % 80);
            static const uint8_t rows[BOOT_FW_SHELLS][2] = {{1, 2}, {3, 4}, {1, 2}, {2, 3}};
            boot_fw_y[s]      = row_y[rows[s][(h >> 20) & 1]];
            boot_fw_radius[s] = finale ? 700 : 165;
            boot_fw_hue[s]    = (uint8_t)(h >> 24);
        }
        for (uint8_t i = 0; i < BOOT_N; i++) {
            boot_fw_reveal[i] = 0xFFFF;
            for (uint8_t s = 0; s < BOOT_FW_SHELLS; s++) {
                uint16_t d = boot_dist((int32_t)boot_px[i] - boot_fw_col[s] * 100, (int32_t)boot_py[i] - boot_fw_y[s]);
                if (d > boot_fw_radius[s]) continue;
                uint16_t at = boot_fw_launch[s] + BOOT_FW_RISE + d / 4;
                if (at < boot_fw_reveal[i]) boot_fw_reveal[i] = at;
            }
        }
    }
    boot_fb_clear();
    // Lit keys: a pale pop at burst, saturating into their own color.
    for (uint8_t i = 0; i < BOOT_N; i++) {
        if (t < boot_fw_reveal[i]) continue;
        uint8_t s = 120 + boot_mul(135, boot_ease_out(boot_prog(t - boot_fw_reveal[i], 0, 450)));
        boot_fb_add(i, boot_hsv(boot_hue[i], s, 255), 255);
    }
    for (uint8_t s = 0; s < BOOT_FW_SHELLS; s++) {
        int32_t dt = (int32_t)t - boot_fw_launch[s];
        if (dt < 0) continue;
        int32_t cx = boot_fw_col[s] * 100;
        if (dt < BOOT_FW_RISE) {
            // The rocket: a warm-white head with a short tail, slowing to apex.
            int32_t y = 534 - (int32_t)(534 - boot_fw_y[s]) * boot_ease_out((uint8_t)(dt * 255 / BOOT_FW_RISE)) / 255;
            RGB     rc = boot_hsv(28, 90, 255);
            for (uint8_t r = 0; r < 6; r++) {
                uint8_t i  = BOOT_KEY(r, boot_fw_col[s]);
                int32_t dy = (int32_t)boot_py[i] - y; // > 0: below the head
                if (dy > -50 && dy <= 0) boot_fb_add(i, rc, (uint8_t)((50 + dy) * 255 / 50));
                if (dy > 0 && dy < 110) boot_fb_add(i, rc, (uint8_t)((110 - dy) * 255 / 110));
            }
            continue;
        }
        // Sparks: fly out from the burst, arc down under gravity, burn out.
        int32_t bt  = dt - BOOT_FW_RISE;
        RGB     sc  = boot_hsv(boot_fw_hue[s], 200, 255);
        for (uint8_t j = 0; j < BOOT_FW_SPARKS; j++) {
            uint32_t h    = boot_rnd(0x5A4Cu + s * 16u + j);
            int32_t  life = 450 + (int32_t)(h % 250);
            if (bt >= life) continue;
            uint8_t a     = (uint8_t)(j * 32 + (h >> 8) % 24);
            int32_t speed = 450 + (int32_t)((h >> 16) % 350); // units / s
            int32_t x     = cx + ((int32_t)cos8(a) - 128) * speed / 128 * bt / 1000;
            int32_t y     = boot_fw_y[s] + ((int32_t)sin8(a) - 128) * speed / 128 * bt / 1000 + 450 * bt * bt / 1000000;
            uint8_t heat  = (uint8_t)(255 - bt * 255 / life);
            for (uint8_t i = 0; i < BOOT_N; i++) {
                int32_t dx = (int32_t)boot_px[i] - x, dy = (int32_t)boot_py[i] - y;
                int32_t d2 = dx * dx + dy * dy;
                if (d2 >= 4900) continue;
                boot_fb_add(i, sc, boot_mul((uint8_t)(255 - d2 * 255 / 4900), heat));
            }
        }
    }
    boot_fb_flush();
}

// 13 Lightning: dark, then bolts strike top to bottom along a jagged path,
// flickering the way lightning does and lighting the sky. Each bolt leaves the
// keys it struck lit in their colors; the last one lights everything.
#define BOOT_BOLT_MS 2400
#define BOOT_BOLTS 3
static uint16_t boot_bolt_t[BOOT_BOLTS];
static uint8_t  boot_bolt_path[BOOT_BOLTS][6]; // column per row
static uint8_t  boot_bolt_fork[BOOT_BOLTS];    // a one-key side branch (LED index)

static uint8_t boot_bolt_env(int32_t dt) {
    if (dt < 0) return 0;
    if (dt < 60) return 255;
    if (dt < 110) return 40;
    if (dt < 170) return 255;
    if (dt < 220) return 70;
    if (dt < 270) return 210;
    if (dt < 600) {
        uint8_t r = (uint8_t)(255 - (dt - 270) * 255 / 330);
        return boot_mul(210, boot_mul(r, r));
    }
    return 0;
}

static void boot_anim_lightning(uint16_t t) {
    if (boot_cache_stale(17)) {
        static const uint16_t base[BOOT_BOLTS] = {200, 850, 1500};
        for (uint8_t k = 0; k < BOOT_BOLTS; k++) {
            uint32_t h     = boot_rnd(0xB017u + k);
            boot_bolt_t[k] = base[k] + (uint16_t)(h % 140);
            int8_t col     = (int8_t)((h >> 8) % 4);
            for (uint8_t r = 0; r < 6; r++) {
                if (r) col += (int8_t)((h >> (10 + 2 * r)) % 3) - 1; // -1, 0, +1
                if (col < 0) col = 1;
                if (col > 3) col = 2;
                boot_bolt_path[k][r] = (uint8_t)col;
            }
            uint8_t fr        = 1 + (h >> 26) % 4;
            int8_t  fc        = (int8_t)boot_bolt_path[k][fr] + ((h >> 30) & 1 ? 1 : -1);
            if (fc < 0 || fc > 3) fc = (int8_t)boot_bolt_path[k][fr] - (fc < 0 ? -1 : 1);
            boot_bolt_fork[k] = BOOT_KEY(fr + 1 > 5 ? 5 : fr + 1, fc);
        }
    }
    const RGB bolt = boot_hsv(165, 50, 255);
    const RGB sky  = boot_hsv(160, 90, 255);
    boot_fb_clear();
    // Keys earlier strikes revealed (and, after the last strike, all of them).
    int32_t last = (int32_t)t - boot_bolt_t[BOOT_BOLTS - 1];
    uint8_t all  = last < 110 ? 0 : boot_fade(boot_prog(last, 110, 590));
    for (uint8_t i = 0; i < BOOT_N; i++) {
        uint8_t v = all;
        for (uint8_t k = 0; k < BOOT_BOLTS && v < 255; k++) {
            if ((int32_t)t - boot_bolt_t[k] >= 110 && boot_bolt_path[k][boot_row[i]] == boot_col[i]) v = 255;
        }
        if (v) boot_fb_add(i, boot_own[i], v);
    }
    for (uint8_t k = 0; k < BOOT_BOLTS; k++) {
        int32_t dt  = (int32_t)t - boot_bolt_t[k];
        uint8_t env = boot_bolt_env(dt);
        if (!env) continue;
        for (uint8_t i = 0; i < BOOT_N; i++) boot_fb_add(i, sky, boot_mul(env, 46));
        for (uint8_t r = 0; r < 6; r++) {
            if (dt < r * 12) break; // the leader races down the path
            boot_fb_add(BOOT_KEY(r, boot_bolt_path[k][r]), bolt, env);
        }
        if (dt >= 40) boot_fb_add(boot_bolt_fork[k], bolt, boot_mul(env, 150));
    }
    boot_fb_flush();
}

// 14 Stack: the colors drop in from above like falling blocks, bottom row
// first, each landing in its slot with a little flash, until the pad is full.
#define BOOT_STACK_MS 2650
static uint16_t boot_stack_t[BOOT_N];

static void boot_anim_stack(uint16_t t) {
    // Fall time to row r from one row above the pad at 40 rows/s^2.
    static const uint16_t land_ms[6] = {224, 316, 387, 447, 500, 548};
    if (boot_cache_stale(19)) {
        for (uint8_t g = 0; g < 6; g++) { // g = 0 is the bottom row
            uint8_t order[4];
            boot_shuffle(order, 4, boot_rnd(0x57ACu + g));
            for (uint8_t j = 0; j < 4; j++) {
                uint8_t k                                = g * 4 + j;
                boot_stack_t[BOOT_KEY(5 - g, order[j])] = (uint16_t)(80 + k * 80 + g * 50 + boot_rnd(0x57A0u + k) % 25);
            }
        }
    }
    boot_fb_clear();
    for (uint8_t i = 0; i < BOOT_N; i++) {
        int32_t dt = (int32_t)t - boot_stack_t[i];
        if (dt < 0) continue;
        uint8_t r = boot_row[i];
        if (dt >= land_ms[r]) {
            uint8_t s = 150 + boot_mul(105, boot_prog(dt - land_ms[r], 0, 180)); // landing flash
            boot_fb_add(i, boot_hsv(boot_hue[i], s, 255), 255);
            continue;
        }
        int32_t y = -256 + dt * dt * 512 / 100000; // 1/256 rows
        int32_t r0 = y >> 8;                       // the two rows the block straddles
        for (int32_t rr = r0; rr <= r0 + 1; rr++) {
            if (rr < 0 || rr > 5) continue;
            int32_t d = y - rr * 256;
            if (d < 0) d = -d;
            if (d >= 256) continue;
            boot_fb_add(BOOT_KEY(rr, boot_col[i]), boot_own[i], (uint8_t)((256 - d) * 255 / 256));
        }
    }
    boot_fb_flush();
}

// 15 Spotlight: a soft spotlight wanders around the dark pad, showing the
// rainbow only where it shines (a little paler at its hot center), then
// widens until the whole pad is lit.
#define BOOT_SPOT_MS 2600
static void boot_anim_spotlight(uint16_t t) {
    uint8_t phx  = (uint8_t)(boot_rnd(0x5B07u) + (uint32_t)t * 2304 / 10000); // 0.9 Hz
    uint8_t phy  = (uint8_t)(boot_rnd(0x5B08u) + (uint32_t)t * 1408 / 10000); // 0.55 Hz
    int32_t sx   = 150 + 135 * ((int32_t)sin8(phx) - 128) / 128;
    int32_t sy   = 237 + 215 * ((int32_t)sin8(phy) - 128) / 128;
    uint8_t grow = boot_prog(t, 1500, 1000);
    int32_t r0   = 105 + 560 * (int32_t)boot_mul(grow, grow) / 255;
    uint8_t pale = boot_mul(110, 255 - grow);
    uint8_t v0   = boot_fade(boot_prog(t, 0, 250));
    for (uint8_t i = 0; i < BOOT_N; i++) {
        int32_t d = boot_dist((int32_t)boot_px[i] - sx, (int32_t)boot_py[i] - sy);
        uint8_t v = d <= r0 ? 255 : d >= r0 + 75 ? 0 : (uint8_t)((r0 + 75 - d) * 255 / 75);
        uint8_t s = d < r0 ? 255 - boot_mul(pale, (uint8_t)(255 - d * 255 / r0)) : 255;
        boot_set(i, boot_hue[i], s, boot_mul(v, v0));
    }
}

// 16 Glitch: the picture boots like a corrupted video signal -- rows tear
// sideways, blocks of wrong color flash, channels drop out, frames black out
// -- and the corruption thins out until the image holds clean.
#define BOOT_GLITCH_MS 2300
#define BOOT_GLITCH_SLOT 45
static void boot_anim_glitch(uint16_t t) {
    uint32_t slot  = t / BOOT_GLITCH_SLOT;
    uint32_t h     = boot_rnd(0x6117C800u + slot);
    uint32_t h2    = boot_hash32(h);
    uint8_t  chaos = 255 - boot_prog(t, 400, 1600);
    bool     image = t >= 300 || (h & 0x300) == 0; // the picture stutters in at first
    RGB      g[6][4];
    for (uint8_t r = 0; r < 6; r++)
        for (uint8_t c = 0; c < 4; c++) g[r][c] = image ? boot_own[BOOT_KEY(r, c)] : (RGB){0, 0, 0};

    if ((uint8_t)(h >> 24) < boot_mul(chaos, 230)) {
        switch ((h >> 4) % 6) {
            case 0: // rows tear sideways
                for (uint8_t r = 0; r < 6; r++) {
                    if (!((h2 >> r) & 1)) continue;
                    uint8_t sh = 1 + (h2 >> (8 + 2 * r)) % 3;
                    RGB     row[4];
                    for (uint8_t c = 0; c < 4; c++) row[c] = g[r][(c + sh) % 4];
                    for (uint8_t c = 0; c < 4; c++) g[r][c] = row[c];
                }
                break;
            case 1: { // a block of wrong color (sometimes white)
                uint8_t r0 = h2 % 6, c0 = (h2 >> 4) % 4, rh = 1 + (h2 >> 8) % 3, cw = 1 + (h2 >> 12) % 3;
                RGB     col = boot_hsv((uint8_t)(h2 >> 16), (h2 >> 28) ? 255 : 0, 255);
                for (uint8_t r = r0; r < r0 + rh && r < 6; r++)
                    for (uint8_t c = c0; c < c0 + cw && c < 4; c++) g[r][c] = col;
                break;
            }
            case 2: // a color channel drops out
                for (uint8_t r = 0; r < 6; r++)
                    for (uint8_t c = 0; c < 4; c++) {
                        if (h2 % 3 == 0) g[r][c].r = 0;
                        if (h2 % 3 == 1) g[r][c].g = 0;
                        if (h2 % 3 == 2) g[r][c].b = 0;
                    }
                break;
            case 3: // blackout from some row down
                for (uint8_t r = h2 % 6; r < 6; r++)
                    for (uint8_t c = 0; c < 4; c++) g[r][c] = (RGB){0, 0, 0};
                break;
            case 4: { // vertical tear: the lower part slips down a row
                uint8_t rt = 1 + h2 % 5;
                for (uint8_t r = 5; r > rt; r--)
                    for (uint8_t c = 0; c < 4; c++) g[r][c] = g[r - 1][c];
                break;
            }
            default: { // a band with inverted hues
                uint8_t r0 = h2 % 5;
                for (uint8_t r = r0; r < r0 + 2; r++)
                    for (uint8_t c = 0; c < 4; c++) g[r][c] = boot_hsv(boot_hue[BOOT_KEY(r, c)] + 128, 255, 255);
                break;
            }
        }
    }
    for (uint8_t r = 0; r < 6; r++)
        for (uint8_t c = 0; c < 4; c++) rgb_matrix_set_color(BOOT_KEY(r, c), g[r][c].r, g[r][c].g, g[r][c].b);
}

// --- text ------------------------------------------------------------------
//
// Letters are 5 rows tall on pad rows 1-5, the evenly spaced ones; the
// half-height top row (row 0) stays dark -- except for the tall forms of W
// and M, which use all six rows. Squeezed into the same rows as everything
// else, both read as an H: the tall W's outer strokes reach up into row 0 with
// the joins low down, and the tall M is its mirror image, starting in row 0
// with the joins high and long legs.
//
// Letter case picks the form: uppercase W/M are tall, lowercase w/m short.
// Every other letter has one form, so its case doesn't matter. W and M also
// come in five columns (tickers, where width is free) and four (styles that
// show one letter at a time). Glyph rows are bitmaps, rows[0] = pad row 0,
// bit (w-1-x) = column x.

#define BOOT_GLYPH_NARROW 0x01 // the four-column form of a five-column letter
#define BOOT_GLYPH_TALL 0x02   // uses row 0

typedef struct {
    char    ch; // uppercase
    uint8_t flags;
    uint8_t w;
    uint8_t rows[6];
} boot_glyph_t;

#define BOOT_TALL BOOT_GLYPH_TALL
#define BOOT_NARROW BOOT_GLYPH_NARROW
static const boot_glyph_t boot_font[] = {
    {'A', 0, 4, {0, 0x6, 0x9, 0xF, 0x9, 0x9}},
    {'B', 0, 4, {0, 0xE, 0x9, 0xE, 0x9, 0xE}},
    {'C', 0, 4, {0, 0x7, 0x8, 0x8, 0x8, 0x7}},
    {'D', 0, 4, {0, 0xE, 0x9, 0x9, 0x9, 0xE}},
    {'E', 0, 4, {0, 0xF, 0x8, 0xE, 0x8, 0xF}},
    {'F', 0, 4, {0, 0xF, 0x8, 0xE, 0x8, 0x8}},
    {'G', 0, 4, {0, 0x7, 0x8, 0xB, 0x9, 0x7}},
    {'H', 0, 4, {0, 0x9, 0x9, 0xF, 0x9, 0x9}},
    {'I', 0, 3, {0, 0x7, 0x2, 0x2, 0x2, 0x7}},
    {'J', 0, 4, {0, 0x3, 0x1, 0x1, 0x9, 0x6}},
    {'K', 0, 4, {0, 0x9, 0xA, 0xC, 0xA, 0x9}},
    {'L', 0, 4, {0, 0x8, 0x8, 0x8, 0x8, 0xF}},
    // M: #...#  ##.##  #.#.#  #...#  #...#  (tall: from row 0, one more leg row)
    {'M', 0, 5, {0, 0x11, 0x1B, 0x15, 0x11, 0x11}},
    {'M', BOOT_TALL, 5, {0x11, 0x1B, 0x15, 0x11, 0x11, 0x11}},
    {'M', BOOT_NARROW, 4, {0, 0x9, 0xF, 0xF, 0x9, 0x9}},
    {'M', BOOT_NARROW | BOOT_TALL, 4, {0x9, 0xF, 0xF, 0x9, 0x9, 0x9}},
    {'N', 0, 4, {0, 0x9, 0xD, 0xB, 0x9, 0x9}},
    {'O', 0, 4, {0, 0x6, 0x9, 0x9, 0x9, 0x6}},
    {'P', 0, 4, {0, 0xE, 0x9, 0xE, 0x8, 0x8}},
    {'Q', 0, 4, {0, 0x6, 0x9, 0x9, 0xA, 0x5}},
    {'R', 0, 4, {0, 0xE, 0x9, 0xE, 0xA, 0x9}},
    {'S', 0, 4, {0, 0x7, 0x8, 0x6, 0x1, 0xE}},
    {'T', 0, 3, {0, 0x7, 0x2, 0x2, 0x2, 0x2}},
    {'U', 0, 4, {0, 0x9, 0x9, 0x9, 0x9, 0x6}},
    {'V', 0, 3, {0, 0x5, 0x5, 0x5, 0x5, 0x2}},
    // W: #...#  #...#  #.#.#  ##.##  #...#  (tall: outer strokes up into row 0)
    {'W', 0, 5, {0, 0x11, 0x11, 0x15, 0x1B, 0x11}},
    {'W', BOOT_TALL, 5, {0x11, 0x11, 0x11, 0x15, 0x1B, 0x11}},
    {'W', BOOT_NARROW, 4, {0, 0x9, 0x9, 0xF, 0xF, 0x9}},
    {'W', BOOT_NARROW | BOOT_TALL, 4, {0x9, 0x9, 0x9, 0xF, 0xF, 0x9}},
    {'X', 0, 4, {0, 0x9, 0x9, 0x6, 0x9, 0x9}},
    {'Y', 0, 3, {0, 0x5, 0x5, 0x2, 0x2, 0x2}},
    {'Z', 0, 4, {0, 0xF, 0x1, 0x6, 0x8, 0xF}},
    {'0', 0, 3, {0, 0x7, 0x5, 0x5, 0x5, 0x7}},
    {'1', 0, 3, {0, 0x2, 0x6, 0x2, 0x2, 0x7}},
    {'2', 0, 4, {0, 0xE, 0x1, 0x6, 0x8, 0xF}},
    {'3', 0, 4, {0, 0xE, 0x1, 0x6, 0x1, 0xE}},
    {'4', 0, 4, {0, 0x9, 0x9, 0xF, 0x1, 0x1}},
    {'5', 0, 4, {0, 0xF, 0x8, 0xE, 0x1, 0xE}},
    {'6', 0, 4, {0, 0x6, 0x8, 0xE, 0x9, 0x6}},
    {'7', 0, 4, {0, 0xF, 0x1, 0x2, 0x4, 0x4}},
    {'8', 0, 4, {0, 0x6, 0x9, 0x6, 0x9, 0x6}},
    {'9', 0, 4, {0, 0x6, 0x9, 0x7, 0x1, 0x6}},
    {'-', 0, 3, {0, 0x0, 0x0, 0x7, 0x0, 0x0}},
    {'!', 0, 1, {0, 0x1, 0x1, 0x1, 0x0, 0x1}},
    {'.', 0, 1, {0, 0x0, 0x0, 0x0, 0x0, 0x1}},
    // In a ticker a space is only the gaps either side of it: 4 dark columns
    // between words, as many as the pad is wide. Any more and the dark pad
    // looks like the end of the text.
    {' ', 0, 0, {0, 0, 0, 0, 0, 0}},
};

// The glyph for c. narrow: the letter must fit four columns. Unknown
// characters draw as a space.
static const boot_glyph_t *boot_glyph(char c, bool narrow) {
    bool    tall = c >= 'A' && c <= 'Z';
    char    up   = (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
    uint8_t want = (narrow ? BOOT_NARROW : 0) | (tall ? BOOT_TALL : 0);
    const boot_glyph_t *any = NULL;
    for (uint8_t i = 0; i < sizeof(boot_font) / sizeof(boot_font[0]); i++) {
        const boot_glyph_t *g = &boot_font[i];
        if (g->ch != up) continue;
        if (g->flags == want) return g;
        if (!any && !(g->flags & BOOT_NARROW) && !(g->flags & BOOT_TALL)) any = g;
    }
    return any ? any : &boot_font[sizeof(boot_font) / sizeof(boot_font[0]) - 1];
}

// Is key (r, c) part of glyph g, centered in the 4 columns?
static bool boot_glyph_lit(const boot_glyph_t *g, uint8_t r, uint8_t c) {
    uint8_t x0 = (uint8_t)((4 - g->w + 1) / 2);
    if (c < x0 || c >= x0 + g->w) return false;
    return (g->rows[r] >> (g->w - 1 - (c - x0))) & 1;
}

// Text color modes.
#define BOOT_TXT_RAINBOW 0 // each lit key in its own rainbow color (a stencil)
#define BOOT_TXT_LETTERS 1 // each letter in its own color, red to violet
#define BOOT_TXT_WHITE 2

// Hue of the k-th non-space letter out of n, spread over the rainbow.
static uint8_t boot_letter_hue(uint8_t k, uint8_t n) {
    return boot_hue_at(n > 1 ? (int16_t)(k * 255 / (n - 1)) : 0);
}

static uint8_t boot_text_letters(const char *txt) {
    uint8_t n = 0;
    for (const char *p = txt; *p; p++) n += *p != ' ';
    return n;
}

static uint8_t boot_text_len(const char *txt) {
    uint8_t n = 0;
    while (txt[n]) n++;
    return n;
}

// The color of a lit text key in a color mode, at saturation s (0 = white).
static RGB boot_text_rgb(uint8_t mode, uint8_t i, uint8_t letter_hue, uint8_t s, uint8_t v) {
    if (mode == BOOT_TXT_WHITE) return boot_hsv(0, 0, v);
    return boot_cool(mode == BOOT_TXT_LETTERS ? letter_hue : boot_hue[i], s, v);
}

// --- ticker ---

// Dark columns after each letter. A key between two columns shows a blend of
// both, so a gap of one is never dark on any key while the text moves; with
// two, one key column is always fully dark between letters.
#define BOOT_TEXT_GAP 2
#define BOOT_TEXT_MAXCOLS (23 * (5 + BOOT_TEXT_GAP) + 4) // 23 of the widest letters, and the block
#define BOOT_TEXT_BLOCK 0x80 // a column of the closing rainbow block (all rows)
static uint8_t     boot_text_cols[BOOT_TEXT_MAXCOLS]; // bits 0-5 = pad rows 0-5
static uint8_t     boot_text_hue[BOOT_TEXT_MAXCOLS];  // per-letter color of each column
static uint8_t     boot_text_ncols;
static const char *boot_text_built = NULL;
static bool        boot_text_block; // whether the built text has the block

// Lay the text out as columns: each glyph, BOOT_TEXT_GAP blank columns, and
// (block) at the end a 4-column block of the rainbow itself that scrolls in
// and docks.
static void boot_text_build(const char *txt, bool block) {
    if (boot_text_built == txt && boot_text_block == block) return;
    uint8_t letters = boot_text_letters(txt), k = 0, n = 0;
    for (const char *p = txt; *p && n <= BOOT_TEXT_MAXCOLS - (5 + BOOT_TEXT_GAP) - 4; p++) {
        const boot_glyph_t *g   = boot_glyph(*p, false);
        uint8_t             hue = boot_letter_hue(k, letters);
        k += *p != ' ';
        for (uint8_t x = 0; x < g->w; x++) {
            uint8_t m = 0;
            for (uint8_t r = 0; r < 6; r++) {
                if ((g->rows[r] >> (g->w - 1 - x)) & 1) m |= 1 << r;
            }
            boot_text_hue[n]    = hue;
            boot_text_cols[n++] = m;
        }
        for (uint8_t q = 0; q < BOOT_TEXT_GAP; q++) {
            boot_text_hue[n]    = hue;
            boot_text_cols[n++] = 0;
        }
    }
    if (block) {
        for (uint8_t q = 0; q < 4; q++) boot_text_cols[n++] = BOOT_TEXT_BLOCK;
    }
    boot_text_ncols = n;
    boot_text_built = txt;
    boot_text_block = block;
}

static uint8_t boot_text_px(int32_t col, uint8_t r) {
    if (col < 0 || col >= boot_text_ncols) return 0;
    uint8_t m = boot_text_cols[col];
    if (m & BOOT_TEXT_BLOCK) return 255;
    return ((m >> r) & 1) ? 255 : 0;
}

// Full-speed and slowing-down times of a ticker, ms (it decelerates over the
// last two columns as the rainbow block docks).
static void boot_text_scroll_times(const char *txt, uint16_t cps, int32_t *t1, int32_t *td) {
    boot_text_build(txt, true);
    int32_t D = (int32_t)boot_text_ncols * 256, L = 2 * 256, v = (int32_t)MAX(cps, 1) * 256;
    *t1       = (D - L) * 1000 / v;
    *td       = 2 * L * 1000 / v;
}

static uint16_t boot_text_scroll_ms(const char *txt, uint16_t cps) {
    int32_t t1, td;
    boot_text_scroll_times(txt, cps, &t1, &td);
    return (uint16_t)MIN(t1 + td + 150, 65000);
}

// Scroll txt right to left at cps columns/s, easing to a stop as the rainbow
// block docks. Sub-column positions blend neighboring columns.
static void boot_text_scroll(uint16_t t, const char *txt, uint16_t cps, uint8_t mode) {
    int32_t t1, td;
    boot_text_scroll_times(txt, cps, &t1, &td);
    const int32_t D = (int32_t)boot_text_ncols * 256, L = 2 * 256, v = (int32_t)MAX(cps, 1) * 256;
    int32_t       p;
    if (t < t1) {
        p = v * t / 1000;
    } else if (t < t1 + td) {
        int32_t dt = t - t1;
        p          = D - L + v * dt / 1000 - (int32_t)((int64_t)v * dt * dt / (2000LL * td));
    } else {
        p = D;
    }
    p -= 4 * 256;
    int32_t base = p >> 8; // arithmetic shift: floors
    uint8_t fr   = (uint8_t)(p & 0xFF);
    for (uint8_t r = 0; r < 6; r++) {
        for (uint8_t c = 0; c < 4; c++) {
            int32_t a = base + c, b = a + 1;
            uint8_t i = BOOT_KEY(r, c);
            // Letter columns take the text color; the closing block keeps the
            // key's own rainbow color.
            RGB ca = boot_own[i], cb = boot_own[i];
            if (a >= 0 && a < boot_text_ncols && !(boot_text_cols[a] & BOOT_TEXT_BLOCK)) ca = boot_text_rgb(mode, i, boot_text_hue[a], 255, 255);
            if (b >= 0 && b < boot_text_ncols && !(boot_text_cols[b] & BOOT_TEXT_BLOCK)) cb = boot_text_rgb(mode, i, boot_text_hue[b], 255, 255);
            RGB o = boot_rgb_lerp(boot_rgb_scale(ca, boot_text_px(a, r)), boot_rgb_scale(cb, boot_text_px(b, r)), fr);
            rgb_matrix_set_color(i, o.r, o.g, o.b);
        }
    }
}

// --- one letter at a time ---

// The order the rest of the pad sparkles in around the last letter (rank
// 0..255 per key): each next key the one whose surroundings are darkest (the
// share of its neighbors still dark, so corners count fully), give or take a
// random nudge. Dark patches break up from the middle and the last keys to
// light are single gaps, never a patch. A plain shuffle often left one for
// last -- most often around the top-left, where the top row (only a W uses
// it) had been dark since. Worked out once per playback.
#define BOOT_FILL_NEAR (150L * 150) // around: next to it, diagonals included (boot_px/py units)
static const uint8_t *boot_fill_ranks(const boot_glyph_t *g) {
    static uint8_t             rank[BOOT_N];
    static uint32_t            for_seed;
    static const boot_glyph_t *for_glyph = NULL;
    if (for_glyph == g && for_seed == boot_seed) return rank;
    bool    dark[BOOT_N];
    uint8_t m = 0;
    for (uint8_t i = 0; i < BOOT_N; i++) {
        dark[i] = !(g && boot_glyph_lit(g, boot_row[i], boot_col[i]));
        m += dark[i];
    }
    for (uint8_t k = 0; k < m; k++) {
        int32_t best = -1;
        uint8_t pick = 0;
        for (uint8_t i = 0; i < BOOT_N; i++) {
            if (!dark[i]) continue;
            int32_t around = 0, still = 0;
            for (uint8_t j = 0; j < BOOT_N; j++) {
                int32_t dx = (int32_t)boot_px[i] - boot_px[j], dy = (int32_t)boot_py[i] - boot_py[j];
                if (j == i || dx * dx + dy * dy > BOOT_FILL_NEAR) continue;
                around++;
                still += dark[j];
            }
            // all neighbors dark outweighs the largest nudge three times over
            int32_t score = still * 18000 / MAX(around, 1) + (int32_t)(boot_rnd(0xF1u + i) % 6000);
            if (score > best) {
                best = score;
                pick = i;
            }
        }
        dark[pick] = false;
        rank[pick] = (uint8_t)(m > 1 ? (uint16_t)k * 255 / (m - 1) : 0);
    }
    for_glyph = g;
    for_seed  = boot_seed;
    return rank;
}

// Letters pop up one at a time, slot_ms each: a white flash cooling into the
// text color. The last stays, and the rest of the pad sparkles in around it.
static uint16_t boot_text_pop_ms(const char *txt, uint16_t slot_ms) {
    uint8_t n = boot_text_len(txt);
    return (uint16_t)MIN(100 + (uint32_t)(n ? n - 1 : 0) * slot_ms + 500 + 300 + 580 + 60, 65000);
}

static void boot_text_pop(uint16_t t, const char *txt, uint16_t slot_ms, uint8_t mode) {
    uint8_t n = boot_text_len(txt);
    if (!n || !slot_ms) return;
    int32_t k = t < 100 ? -1 : MIN(((int32_t)t - 100) / slot_ms, n - 1);
    int32_t dt   = (int32_t)t - 100 - k * slot_ms;
    int32_t fill = (int32_t)t - (100 + (int32_t)(n - 1) * slot_ms + 500); // the sparkle finish
    uint8_t hue  = 0;
    if (k >= 0) {
        uint8_t before = 0;
        for (int32_t q = 0; q < k; q++) before += txt[q] != ' ';
        hue = boot_letter_hue(before, boot_text_letters(txt));
    }
    const boot_glyph_t *g    = k >= 0 ? boot_glyph(txt[k], true) : NULL;
    const uint8_t      *rank = fill >= 0 ? boot_fill_ranks(g) : NULL;
    for (uint8_t i = 0; i < BOOT_N; i++) {
        if (g && boot_glyph_lit(g, boot_row[i], boot_col[i])) {
            uint8_t v = boot_prog(dt, 0, 70);
            if (k < n - 1) v = boot_mul(v, 255 - boot_prog(dt, slot_ms - 80, 70)); // the last letter stays
            RGB c = boot_text_rgb(mode, i, hue, boot_prog(dt, 70, 150), v);
            if (k == n - 1 && mode != BOOT_TXT_RAINBOW) {
                // The last letter ends in the key's own color, like the rest.
                c = boot_rgb_lerp(c, boot_rgb_scale(boot_own[i], v), boot_prog(fill, 0, 600));
            }
            rgb_matrix_set_color(i, c.r, c.g, c.b);
        } else if (fill >= 0) {
            boot_spark(i, (int16_t)(fill - (int32_t)rank[i] * 300 / 255));
        } else {
            boot_set(i, 0, 0, 0);
        }
    }
}

// Letters flash on for about three quarters of each period_ms, like a sign
// spelling itself out, then the whole rainbow sputters on like neon.
static uint16_t boot_text_flash_ms(const char *txt, uint16_t period_ms) {
    return (uint16_t)MIN(100 + (uint32_t)boot_text_len(txt) * period_ms + 180 + 300, 65000);
}

static void boot_text_flash(uint16_t t, const char *txt, uint16_t period_ms, uint8_t mode) {
    uint8_t n   = boot_text_len(txt);
    int32_t end = 100 + (int32_t)n * period_ms; // after the last letter
    if ((int32_t)t >= end || !period_ms) {
        int32_t dt = (int32_t)t - end;
        bool    on = dt < 50 || (dt >= 90 && dt < 120) || dt >= 180; // on, off, on, off, steady
        for (uint8_t i = 0; i < BOOT_N; i++) boot_set(i, boot_hue[i], 255, on ? 255 : 0);
        return;
    }
    int32_t k  = t < 100 ? 0 : ((int32_t)t - 100) / period_ms;
    int32_t dt = (int32_t)t - 100 - k * period_ms;
    bool    lit_phase = t >= 100 && dt < (int32_t)period_ms * 8 / 11;
    uint8_t before    = 0;
    for (int32_t q = 0; q < k; q++) before += txt[q] != ' ';
    uint8_t             hue = boot_letter_hue(before, boot_text_letters(txt));
    const boot_glyph_t *g   = boot_glyph(txt[k], true);
    for (uint8_t i = 0; i < BOOT_N; i++) {
        RGB c = {0, 0, 0};
        if (lit_phase && boot_glyph_lit(g, boot_row[i], boot_col[i])) c = boot_text_rgb(mode, i, hue, 255, 255);
        rgb_matrix_set_color(i, c.r, c.g, c.b);
    }
}

// --- the text animations ---

// 17 FW16 pop
#define BOOT_TEXT_POP_MS 2800
static void boot_anim_text_pop(uint16_t t) {
    boot_text_pop(t, "FW16", 420, BOOT_TXT_RAINBOW);
}

// 18 FW16 ticker
#define BOOT_TEXT_FW16_MS 3500
static void boot_anim_text_fw16(uint16_t t) {
    boot_text_scroll(t, "FW16", 9, BOOT_TXT_RAINBOW);
}

// 19 FRAMEWORK ticker, at an easy reading pace
#define BOOT_TEXT_FWK_MS 9050
static void boot_anim_text_framework(uint16_t t) {
    boot_text_scroll(t, "FRAMEWORK", 7, BOOT_TXT_RAINBOW);
}

// 20 FRAMEWORK flash, white
#define BOOT_TEXT_FLASH_MS 2650
static void boot_anim_text_flash(uint16_t t) {
    boot_text_flash(t, "FRAMEWORK", 220, BOOT_TXT_WHITE);
}

// 21 FRAMEWORK ticker, each letter its own color
static void boot_anim_text_framework_letters(uint16_t t) {
    boot_text_scroll(t, "FRAMEWORK", 7, BOOT_TXT_LETTERS);
}

// 22 FRAMEWORK16 ticker, each letter its own color (no space: the step from
// letters to digits is clear enough, and a gap there looked like the end)
#define BOOT_TEXT_FWK16_MS 10600
static void boot_anim_text_framework16(uint16_t t) {
    boot_text_scroll(t, "FRAMEWORK16", 7, BOOT_TXT_LETTERS);
}

// --- custom text (BOOT REPLAY 0xFB) ---
//
// Any text sent from the host, in any style, for trying things out; it plays
// through the normal playback, unnumbered, as variant BOOT_CTEXT.

#define BOOT_CTEXT 0xF0
#define BOOT_CTEXT_FLASH 0
#define BOOT_CTEXT_TICKER 1
#define BOOT_CTEXT_POP 2
static char     boot_ctext[24];
static uint8_t  boot_ctext_style, boot_ctext_color;
static uint16_t boot_ctext_speed; // flash/pop: ms per letter; ticker: columns/s

static uint16_t boot_ctext_ms(void) {
    switch (boot_ctext_style) {
        case BOOT_CTEXT_TICKER:
            return boot_text_scroll_ms(boot_ctext, boot_ctext_speed);
        case BOOT_CTEXT_POP:
            return boot_text_pop_ms(boot_ctext, boot_ctext_speed);
        default:
            return boot_text_flash_ms(boot_ctext, boot_ctext_speed);
    }
}

static void boot_ctext_render(uint16_t t) {
    switch (boot_ctext_style) {
        case BOOT_CTEXT_TICKER:
            boot_text_scroll(t, boot_ctext, boot_ctext_speed, boot_ctext_color);
            break;
        case BOOT_CTEXT_POP:
            boot_text_pop(t, boot_ctext, boot_ctext_speed, boot_ctext_color);
            break;
        default:
            boot_text_flash(t, boot_ctext, boot_ctext_speed, boot_ctext_color);
            break;
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
    {boot_anim_twinkle_scatter, BOOT_TWINKLE_MS},   // 7
    {boot_anim_fire, BOOT_FIRE_MS},                 // 8
    {boot_anim_converge, BOOT_CONVERGE_MS},         // 9
    {boot_anim_flicker, BOOT_FLICKER_MS},           // 10
    {boot_anim_lockin, BOOT_LOCKIN_MS},             // 11
    {boot_anim_fireworks, BOOT_FW_MS},              // 12
    {boot_anim_lightning, BOOT_BOLT_MS},            // 13
    {boot_anim_stack, BOOT_STACK_MS},               // 14
    {boot_anim_spotlight, BOOT_SPOT_MS},            // 15
    {boot_anim_glitch, BOOT_GLITCH_MS},             // 16
    // text
    {boot_anim_text_pop, BOOT_TEXT_POP_MS},         // 17
    {boot_anim_text_fw16, BOOT_TEXT_FW16_MS},       // 18
    {boot_anim_text_framework, BOOT_TEXT_FWK_MS},   // 19
    {boot_anim_text_flash, BOOT_TEXT_FLASH_MS},     // 20
    {boot_anim_text_framework_letters, BOOT_TEXT_FWK_MS}, // 21
    {boot_anim_text_framework16, BOOT_TEXT_FWK16_MS},     // 22
};
#define BOOT_ANIM_COUNT ((uint8_t)(sizeof(boot_anims) / sizeof(boot_anims[0])))

// The power-up pool: which animations (0-based table indices) a boot may
// pick. Every animation can be replayed by number; only these play at boot.
static const uint8_t boot_pool[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21};
#define BOOT_POOL_COUNT ((uint8_t)(sizeof(boot_pool) / sizeof(boot_pool[0])))

// A variant's length and render, the custom text (BOOT_CTEXT) included.
static uint16_t boot_variant_ms(uint8_t v) {
    return v == BOOT_CTEXT ? boot_ctext_ms() : boot_anims[v].duration_ms;
}

static void boot_variant_render(uint8_t v, uint16_t t) {
    if (v == BOOT_CTEXT) {
        boot_ctext_render(t);
    } else {
        boot_anims[v].render(t);
    }
}

// --- picking ---------------------------------------------------------------

// Which variant the n-th power-up plays: position n % POOL of the shuffle
// for cycle n / POOL. A cycle whose first pick repeats the previous cycle's
// last swaps its first two, so no variant ever plays twice in a row.
static uint8_t boot_bag_pick(uint32_t n) {
    uint8_t  cur[BOOT_POOL_COUNT];
    uint32_t cycle = n / BOOT_POOL_COUNT;
    boot_shuffle(cur, BOOT_POOL_COUNT, boot_hash32(cycle));
    if (cycle > 0) {
        uint8_t prev[BOOT_POOL_COUNT];
        boot_shuffle(prev, BOOT_POOL_COUNT, boot_hash32(cycle - 1));
        if (cur[0] == prev[BOOT_POOL_COUNT - 1]) {
            cur[0] = cur[1];
            cur[1] = prev[BOOT_POOL_COUNT - 1];
        }
    }
    return boot_pool[cur[n % BOOT_POOL_COUNT]];
}

static void boot_counter_load(void) {
    if (!boot_counter_loaded) {
        boot_counter_loaded = true;
        boot_counter        = eeconfig_read_user();
    }
}

// Draw the next power-up pick and advance the counter (saved after playback).
static uint8_t boot_next_pick(void) {
    boot_counter_load();
    uint8_t v = boot_bag_pick(boot_counter);
    boot_base_seed = boot_hash32(boot_counter ^ 0xB0075EEDU);
    boot_counter++;
    boot_counter_dirty = true;
    return v;
}

// --- playback --------------------------------------------------------------

static void boot_start(uint8_t first, uint8_t span, uint8_t rounds, bool framed) {
    if (span == 0) span = 1;
    if (rounds == 0) rounds = 1;
    if (rounds > 255 / span) rounds = 255 / span; // boot_count is 8-bit
    boot_first    = first;
    boot_span     = span;
    boot_count    = span * rounds;
    boot_framed   = framed;
    boot_dots     = framed && span > 1; // one animation needs no naming
    boot_speed    = 100;
    boot_t0       = timer_read32();
    boot_playing  = true;
    boot_seed_slot = 0xFF; // reseed on the first frame
}

// BOOT REPLAY command: args[0] = first variant (1-based; 0 = all of them,
// 0xFE = a power-up pick, anything else past the end = stop), args[1] = last
// variant (0 = just the first), args[2] = rounds (0 = 1), args[3..4] = speed
// in percent (u16 LE, 0 = 100).
static void boot_replay(const uint8_t *args) {
    if (args[0] == 0xFD) return; // status query only
    if (args[0] == 0xFC) {       // preview warning styles args[1]..args[2]
        warn_prev_first   = args[1] ? MIN(args[1], SYSMON_WARN_STYLES) : 1;
        warn_prev_last    = args[2] ? MIN(args[2], SYSMON_WARN_STYLES) : (args[1] ? warn_prev_first : SYSMON_WARN_STYLES);
        warn_prev_started = false;
        return;
    }
    boot_armed = false; // a replay before the first frame replaces the boot play
    if (args[0] == 0xFB) {
        // Custom text: [1] style (0 flash, 1 ticker, 2 pop), [2] color (0
        // rainbow, 1 per letter, 2 white), [3..4] speed (u16 LE: ms per letter
        // for flash/pop, columns/s for the ticker; 0 = default), [5..] text,
        // NUL-terminated or up to 23 characters.
        static const uint16_t defaults[3] = {220, 7, 420};
        boot_ctext_style = args[1] <= BOOT_CTEXT_POP ? args[1] : BOOT_CTEXT_FLASH;
        boot_ctext_color = args[2] <= BOOT_TXT_WHITE ? args[2] : BOOT_TXT_RAINBOW;
        boot_ctext_speed = (uint16_t)(args[3] | (args[4] << 8));
        if (!boot_ctext_speed) boot_ctext_speed = defaults[boot_ctext_style];
        uint8_t q = 0;
        for (; q < sizeof(boot_ctext) - 1 && args[5 + q]; q++) boot_ctext[q] = (char)args[5 + q];
        boot_ctext[q]   = 0;
        boot_text_built = NULL; // same buffer, new contents
        boot_base_seed  = boot_hash32(timer_read32());
        boot_start(BOOT_CTEXT, 1, 1, false);
        return;
    }
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
        boot_playing    = false;
        warn_prev_first = 0;
        return;
    }
    boot_start(first - 1, last - first + 1, args[2], true);
    uint16_t speed = (uint16_t)(args[3] | (args[4] << 8));
    boot_speed     = speed ? MIN(MAX(speed, 10), 1000) : 100;
}

static void boot_put32(uint8_t *out, uint32_t v) {
    for (uint8_t b = 0; b < 4; b++) out[b] = (uint8_t)(v >> (8 * b));
}

// Status for the BOOT REPLAY echo (14 bytes; layout in the protocol notes).
static void boot_status(uint8_t *out) {
    boot_counter_load(); // (no pick may have loaded it: the animation skipped)
    boot_put32(out, boot_counter);
    out[4] = boot_powerup_pick;
    boot_put32(out + 5, eeconfig_read_user());
    out[9]  = sysmon_dark() ? DISP_DARK : sysmon_disp; // dark: nothing shows
    out[10] = boot_playing;
    out[11] = msg_waiting || msg_playing;
    out[12] = SYSMON_CAPS;
    out[13] = !sysmon_lights_off();
}

// Variant n's name tag. Up to 9: n white dots in reading order from the
// top-left key. From 10: the tens as cyan dots on the top row, then the units
// as white dots in reading order from the second row.
static void boot_render_dots(uint8_t n) {
    for (uint8_t i = 0; i < BOOT_N; i++) boot_set(i, 0, 0, 0);
    if (n < 10) {
        for (uint8_t k = 0; k < n; k++) boot_set(BOOT_KEY(k / 4, k % 4), 0, 0, 255);
        return;
    }
    for (uint8_t k = 0; k < n / 10 && k < 4; k++) boot_set(BOOT_KEY(0, k), 128, 255, 255);
    for (uint8_t k = 0; k < n % 10; k++) boot_set(BOOT_KEY(1 + k / 4, k % 4), 0, 0, 255);
}

// How long variant v takes to play at the replay's speed, ms.
static uint32_t boot_play_ms(uint8_t v) {
    return (uint32_t)boot_variant_ms(v) * 100 / boot_speed;
}

// Length of one playlist slot showing variant v.
static uint32_t boot_slot_ms(uint8_t v) {
    uint32_t len = boot_play_ms(v);
    if (boot_framed) len += (boot_dots ? BOOT_DOTS_MS : 0) + BOOT_GAP_MS + BOOT_HOLD_MS;
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
            // the slot, not the frame, so it is drawn once per slot.
            boot_seed_slot = k;
            boot_reseed(boot_hash32(boot_base_seed + k));
        }
        if (boot_framed) {
            if (boot_dots) {
                if (t < BOOT_DOTS_MS) {
                    boot_render_dots(v + 1);
                    return true;
                }
                t -= BOOT_DOTS_MS;
            }
            if (t < BOOT_GAP_MS) {
                for (uint8_t i = 0; i < BOOT_N; i++) boot_set(i, 0, 0, 0);
                return true;
            }
            t -= BOOT_GAP_MS;
        }
        uint32_t len = boot_play_ms(v);
        if (t < len) {
            // the animation's own clock, at the replay's speed
            boot_variant_render(v, (uint16_t)MIN(t * boot_speed / 100, (uint32_t)boot_variant_ms(v) - 1));
            return true;
        }
        t -= len;
        if (boot_framed) {
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

static uint32_t sysmon_timeout_ms(void) {
    return (uint32_t)sysmon_timeout_units * 100;
}

// ms since the last FRAME B (0 before the first)
static uint32_t sysmon_since(void) {
    return sysmon_last_update ? timer_elapsed32(sysmon_last_update) : 0;
}

static bool sysmon_host_live(void) {
    return sysmon_last_update != 0 && !sysmon_goodbye && sysmon_since() <= sysmon_timeout_ms();
}

// Any key press drops the boot rainbow for the regular effect: the rainbow
// waits for the host, and a pad used without one should answer its keys.
//
// With NumLock off, keypad 5 (which does nothing then) is the replay key: it
// asks the host, through the next FRAME B echo, to say again which limits hold
// the CPU and dGPU back. It is caught by keycode rather than put in the
// keymap: VIA keeps the keymap in EEPROM, where a new default would never
// reach a pad that already has one.
//
// This is the pre-process hook, not process_record_user: the Framework
// keyboard code calls process_record_user and ignores what it returns, so
// returning false there still sent KP_5 to the host (Clear on Windows). False
// here drops the event before any keycode handling. The release is dropped
// only if its press was, so NumLock changing while the key is held can't
// leave KP_5 stuck down or release a key that was never pressed.
//
// Only while the sysmon host is streaming, i.e. there to answer, and the pad
// is lit (dark, there is nothing to show the answer on). Anywhere else the key
// stays an ordinary keypad 5: a host that never reports NumLock (macOS, a
// pre-boot environment) reads as NumLock off all the time, and swallowing the
// key there would take it away for good.
//
// Holding the top-left key (Esc; RGB toggle on the Fn layer) for
// SYSMON_SAVE_HOLD_MS saves the LED trace to the flash (see "saved trace"):
// for a glitch seen while the laptop boots, before any tool on it can read
// the trace out. Caught by position, so it works whatever the keymap and
// NumLock say, and the press never reaches the host; a shorter press is
// played back through QMK on release, as the key it is then. A hold doesn't
// count as using the pad (the boot rainbow stays); a tap does.
#define SYSMON_SAVE_KEY_ROW 2 // matrix position of the top-left key (H1)
#define SYSMON_SAVE_KEY_COL 1
#define SYSMON_SAVE_HOLD_MS 1000
static uint32_t save_key_down;   // when it went down (0: up)
static bool     save_key_fired;  // this hold saved a trace
static bool     save_key_tap;    // released early: play its tap back
static bool     save_key_replay; // ...being played back now

bool pre_process_record_user(uint16_t keycode, keyrecord_t *record) {
    static matrix_row_t swallowed[MATRIX_ROWS]; // keys whose press was swallowed
    keypos_t            k = record->event.key;
    if (IS_KEYEVENT(record->event) && k.row == SYSMON_SAVE_KEY_ROW && k.col == SYSMON_SAVE_KEY_COL && !save_key_replay) {
        if (record->event.pressed) {
            save_key_down  = sysmon_stamp();
            save_key_fired = false;
        } else if (save_key_down) {
            save_key_tap  = !save_key_fired;
            save_key_down = 0;
        }
        return false;
    }
    if (record->event.pressed) {
        boot_dismissed = true;
        boot_playing   = false;
    }
    if (!IS_KEYEVENT(record->event)) return true; // not a matrix key (an encoder, say)
    matrix_row_t bit = (matrix_row_t)1 << k.col;
    if (!record->event.pressed && (swallowed[k.row] & bit)) {
        // its release, by position: VIA can remap the key while it is held,
        // and more than one key can be keypad 5
        swallowed[k.row] &= (matrix_row_t)~bit;
        return false;
    }
    if (keycode != KC_P5 || !record->event.pressed) return true;
    if (host_keyboard_led_state().num_lock || !sysmon_host_live() || sysmon_dark()) return true;
    swallowed[k.row] |= bit;
    sysmon_ask = true;
    return false;
}

// The boot counter is saved once the animation is over (played out, or cut
// short by a key) and USB is configured: a flash write stalls the MCU, which
// during enumeration can make it fail. A key press alone is no sign of that --
// keys are scanned from power-up -- so the USB state is checked, not a time.
// Saved from here rather than from the indicator hook, which QMK stops calling
// while the RGB matrix is off: a pick that is never saved plays again next time.
//
// REBOOT: restart a moment after the command, so its echo still goes out. The
// counter is saved first even mid-animation (USB is up: the command came over
// it), or a reboot during the animation would replay the same pick.
void housekeeping_task_user(void) {
    // Runs with the matrix off too, unlike the indicator hook: note that the
    // pad went dark, for the display to start from dark when it is back.
    if (sysmon_dark()) sysmon_went_dark = true;
    // The power-up animation belongs to the power-up. With the lighting off
    // at power-up (RGB_TOG's off persists), the first frame comes whenever it
    // is back -- a key, or VIA -- maybe hours later: don't play it then.
    // (Checked here, at once: a first frame merely delayed by a USB suspend,
    // the pad plugged into a sleeping laptop, still plays it on wake. Not in
    // keyboard_post_init_user, which macropad.c already defines.)
    static bool boot_checked = false;
    if (!boot_checked) {
        boot_checked = true;
        if (sysmon_lights_off()) boot_armed = false;
    }
    // USB configured and settled: SET_CONFIGURATION comes before the host's
    // HID setup (descriptors, SET_IDLE, the LED report), so wait a moment more.
    // (A suspend restarts it: suspend_power_down_user clears usb_up_since, as
    // housekeeping doesn't run in the suspend loop to see USB go away.)
    if (!usb_connected_state()) {
        usb_up_since = 0;
    } else if (!usb_up_since) {
        usb_up_since = sysmon_stamp();
    }
    static bool usb_ever_up = false;
    if (usb_up_since) usb_ever_up = true;
    // The save key: held long enough, it saves; let go sooner, it was a tap,
    // played back here, outside QMK's handling of the release.
    if (save_key_down && !save_key_fired && timer_elapsed32(save_key_down) >= SYSMON_SAVE_HOLD_MS) {
        save_key_fired = true;
        sysmon_trace_save_start(SYSMON_SAVED_BY_KEY);
    }
    if (save_key_tap) {
        save_key_tap    = false;
        save_key_replay = true;
        action_exec(MAKE_KEYEVENT(SYSMON_SAVE_KEY_ROW, SYSMON_SAVE_KEY_COL, true));
        action_exec(MAKE_KEYEVENT(SYSMON_SAVE_KEY_ROW, SYSMON_SAVE_KEY_COL, false));
        save_key_replay = false;
    }
    sysmon_trace_save_step(usb_up_since && timer_elapsed32(usb_up_since) >= BOOT_SAVE_SETTLE_MS);
    // (With nothing drawn nothing plays, so a replay started meanwhile
    // counts as over: its pick is saved too.)
    if (boot_counter_dirty && (!boot_playing || sysmon_dark())) {
        bool usb_settled = usb_up_since && timer_elapsed32(usb_up_since) >= BOOT_SAVE_SETTLE_MS;
        // (No host configuring USB at all -- power only: nothing to keep clear
        // of. Only if it never came up: down again later is a re-enumeration.)
        bool no_usb = !usb_ever_up && timer_read32() >= BOOT_SAVE_NO_USB_MS;
        if (usb_settled || no_usb) {
            boot_counter_dirty = false;
            eeconfig_update_user(boot_counter);
        }
    }
    if (sysmon_reboot_at && timer_elapsed32(sysmon_reboot_at) >= 50) {
        if (boot_counter_dirty) eeconfig_update_user(boot_counter);
        soft_reset_keyboard();
    }
}

// Restarting (REBOOT, or a reset into the bootloader): go dark first. The LED
// chip keeps its registers through the MCU's reset, and the restart switches
// it back on at the stock current before the first frame is sent, so the
// last frame (the rainbow's full PWM, meant for a current of 3) flashed up at
// that current. The current goes to 0 at once, then the PWM is cleared, so
// the restart finds nothing to show.
void shutdown_user(void) {
    IS31FL_unlock_register(DRIVER_ADDR_1, ISSI_PAGE_FUNCTION);
    IS31FL_write_single_register(DRIVER_ADDR_1, ISSI_REG_GLOBALCURRENT, 0);
    sysmon_gcc = sysmon_gcc_next = 0;
    rgb_matrix_set_color_all(0, 0, 0);
    rgb_matrix_driver.flush();
}

// A USB suspend (sleep) silences the host for an expected reason: don't greet
// the wake-up with a disconnect warning. The next frame clears this.
void suspend_power_down_user(void) {
    sysmon_quiet     = true;
    sysmon_went_dark = true; // nothing is drawn while suspended
    usb_up_since     = 0;    // the bus re-settles after the wake
}

// --- disconnect warning ----------------------------------------------------

// Shown when the host stops sending without saying goodbye (a crash, a hung
// process): black-and-yellow caution stripes, diagonal bands two keys thick.
// Candidates (preview them with BOOT REPLAY 0xFC):
//   1 static, bands rising to the right "/"   2 static, falling "\"
//   3 static, slowly breathing                4 blinking slowly as a whole
//   5 stepping one band-width at a time, like the chaser lights on a barrier
#define SYSMON_WARN_MS 10000
#define SYSMON_WARN_STYLE 1 // the one shown for real
#define SYSMON_WARN_HUE 40  // yellow
static uint32_t sysmon_warn_el;                         // how far into the warning the last frame was
static uint8_t  sysmon_warn_shown = SYSMON_WARN_STYLE; // style of that frame

// Is key (r, c) on a yellow band? Bands follow the key grid's diagonals
// (r + c = const runs "/"), two diagonals yellow, two black; shift moves them.
static bool sysmon_warn_stripe(uint8_t r, uint8_t c, bool falling, uint8_t shift) {
    uint8_t d = r + (falling ? 3 - c : c) + shift;
    return ((d / 2) & 1) == 0;
}

// Warning color of key i, el ms into the warning. Every style fades in and,
// over the last 800 ms, out.
static RGB sysmon_warn_rgb(uint8_t style, uint8_t i, uint32_t el) {
    uint8_t r = boot_row[i], c = boot_col[i];
    uint8_t env = boot_mul(boot_prog(el, 0, 300), 255 - boot_prog(el, SYSMON_WARN_MS - 800, 800));
    uint8_t v   = 255;
    bool    on  = false;
    switch (style) {
        default:
        case 1:
            on = sysmon_warn_stripe(r, c, false, 0);
            break;
        case 2:
            on = sysmon_warn_stripe(r, c, true, 0);
            break;
        case 3: // breathing 55%..100% over 2.4 s
            on = sysmon_warn_stripe(r, c, false, 0);
            v  = 140 + boot_mul(sin8((uint8_t)((el % 2400) * 256 / 2400)), 115);
            break;
        case 4: { // on 800 ms of every 1300, with soft 150 ms edges
            uint32_t ph = el % 1300;
            on          = sysmon_warn_stripe(r, c, false, 0);
            v           = ph >= 800 ? 0 : (uint8_t)MIN(MIN(ph, 800 - ph) * 255 / 150, 255);
            break;
        }
        case 5: // the bands step down-right one diagonal every 350 ms
            on = sysmon_warn_stripe(r, c, false, (uint8_t)(4 - (el / 350) % 4));
            break;
    }
    if (!on) return (RGB){0, 0, 0};
    return boot_hsv(SYSMON_WARN_HUE, 255, boot_mul(env, v));
}

// BOOT REPLAY 0xFC: show warning styles first..last, each named by amber dots
// first, at the warning's real length. Starts when it first renders, so it can
// be queued behind a boot replay.
static bool     warn_prev_started = false;
static uint32_t warn_prev_t0;
#define WARN_PREV_SLOT (BOOT_DOTS_MS + BOOT_GAP_MS + SYSMON_WARN_MS)

static bool warn_preview_render(void) {
    if (!warn_prev_first) return false;
    if (!warn_prev_started) {
        warn_prev_started = true;
        warn_prev_t0      = timer_read32();
    }
    uint32_t t     = timer_elapsed32(warn_prev_t0);
    uint32_t k     = t / WARN_PREV_SLOT;
    uint32_t el    = t % WARN_PREV_SLOT;
    if (warn_prev_first + k > warn_prev_last) {
        warn_prev_first = 0;
        return false;
    }
    uint8_t style = (uint8_t)(warn_prev_first + k);
    for (uint8_t i = 0; i < BOOT_N; i++) {
        RGB col = {0, 0, 0};
        if (el < BOOT_DOTS_MS) {
            uint8_t n = 0;
            for (uint8_t q = 0; q < style; q++)
                if (BOOT_KEY(q / 4, q % 4) == i) n = 1;
            if (n) col = boot_hsv(30, 255, 255);
        } else if (el >= BOOT_DOTS_MS + BOOT_GAP_MS) {
            sysmon_warn_el    = el - BOOT_DOTS_MS - BOOT_GAP_MS;
            sysmon_warn_shown = style;
            col               = sysmon_warn_rgb(style, i, sysmon_warn_el);
        }
        rgb_matrix_set_color(i, col.r, col.g, col.b);
    }
    return true;
}

// --- blending into the bars ------------------------------------------------

// Coming from the boot rainbow or the warning, the bars fade in over
// SYSMON_XFADE_MS instead of cutting (the cut read as a dark gap). From the
// regular effect it still cuts.
//
// Each key makes its own way, straight from what it showed when the fade
// began to what the bars want it to show now: the bars are read afresh every
// frame, so a bar that moves meanwhile is followed, not jumped to at the end.
// The way is a straight line in a perceptual color space (Oklab, whose
// cube-root cone responses a lerp can run in directly), so brightness and
// hue change evenly to the eye: a key going dark fades evenly instead of
// lingering and then dropping, and one going from red to green passes
// through orange, not through a dim olive.
//
// Each frame goes out at just the global current its brightest key needs,
// the PWM making up the rest, so no key is ever clipped (clipped keys sat
// still while the current stepped under them: a jerk per step) and the
// current only moves along with the light. Bars brighter than the rainbow:
// the current rises with the brightest keys and arrives at the bars' own at
// the end. Bars no brighter: it stays at the rainbow's for the fade and
// steps down to the bars' after it.
#define SYSMON_XFADE_MS 700
static uint8_t  sysmon_xf_from = DISP_EFFECT;
static uint32_t sysmon_xf_t0;
static bool     sysmon_xf_ready;                       // this fade's start frame is taken
static uint8_t  sysmon_xf_gf;                          // the current it was shown at
static float    sysmon_xf_s[RGB_MATRIX_LED_COUNT][3];  // the start frame, perceptual
static float    sysmon_xf_t[RGB_MATRIX_LED_COUNT][3];  // the bars, perceptual,
static RGB      sysmon_xf_tc[RGB_MATRIX_LED_COUNT];    // as of these colors
static uint8_t  sysmon_xf_tg;                          // at this current (0: none yet)

static bool sysmon_xfading(void) {
    return sysmon_xf_from != DISP_EFFECT && timer_elapsed32(sysmon_xf_t0) < SYSMON_XFADE_MS;
}

// c, drawn for global current gcc, as PWM for current g (clipped at full).
static RGB sysmon_gcc_scale(RGB c, uint8_t gcc, uint8_t g) {
    if (gcc == g) return c;
    g = MAX(g, 1);
    return (RGB){.r = (uint8_t)MIN((uint16_t)c.r * gcc / g, 255), .g = (uint8_t)MIN((uint16_t)c.g * gcc / g, 255), .b = (uint8_t)MIN((uint16_t)c.b * gcc / g, 255)};
}

static float sysmon_smooth(float u) {
    u = u < 0 ? 0 : (u > 1 ? 1 : u);
    return u * u * (3 - 2 * u);
}

// Cube root: a first guess from the float's bits, then Newton. (newlib's
// cbrtf works in soft doubles; this one runs up to 72 times a frame.)
static float sysmon_cbrt(float x) {
    if (x <= 0) return 0;
    union {
        float    f;
        uint32_t i;
    } u     = {.f = x};
    u.i     = u.i / 3 + 709958130u;
    float y = u.f;
    for (uint8_t k = 0; k < 3; k++) y = (2 * y + x / (y * y)) * (1.0f / 3);
    return y;
}

// c's light (PWM x current gcc) as a point in the perceptual space...
static void sysmon_xf_to(RGB c, uint8_t gcc, float p[3]) {
    float r = (float)c.r * gcc, g = (float)c.g * gcc, b = (float)c.b * gcc;
    p[0]    = sysmon_cbrt(0.4122214708f * r + 0.5363325363f * g + 0.0514459929f * b);
    p[1]    = sysmon_cbrt(0.2119034982f * r + 0.6806995451f * g + 0.1073969566f * b);
    p[2]    = sysmon_cbrt(0.0883024619f * r + 0.2817188376f * g + 0.6299787005f * b);
}

// ...and a point back to light.
static void sysmon_xf_light(const float p[3], float o[3]) {
    float l = p[0] * p[0] * p[0], m = p[1] * p[1] * p[1], s = p[2] * p[2] * p[2];
    o[0]    = MAX(4.0767416621f * l - 3.3077115913f * m + 0.2309699292f * s, 0.0f);
    o[1]    = MAX(-1.2684380046f * l + 2.6097574011f * m - 0.3413193965f * s, 0.0f);
    o[2]    = MAX(-0.0041960863f * l - 0.7034186147f * m + 1.7076147010f * s, 0.0f);
}

// The fade's frame: every key e of the way along its line, written out, and
// the current it all goes out at (returned).
//
// The start frame is lit at the current on the chip when the fade begins:
// what the last frame of the rainbow, the warning or the message actually went
// out at. Not the current the host's brightness byte asks for -- a host
// coming back at a new screen brightness would start the fade at that one,
// and the warning would jump brighter or dimmer on the fade's first frame.
static uint8_t sysmon_xf_frame(uint8_t gb) {
    if (!sysmon_xf_ready) {
        // From dark the start frame is black, lit at any current alike: take
        // the bars' own. The chip's can be stale -- the pad went dark at
        // another screen brightness -- and holding it through the fade would
        // run the bars at a much higher current on a few PWM steps.
        sysmon_xf_gf = sysmon_xf_from == DISP_DARK ? gb : sysmon_gcc;
        for (uint8_t i = 0; i < RGB_MATRIX_LED_COUNT; i++) {
            RGB a = {.r = 0, .g = 0, .b = 0}; // a message ends on dark
            if (sysmon_xf_from == DISP_BOOT) a = boot_own[i];
            if (sysmon_xf_from == DISP_WARN) a = sysmon_warn_rgb(sysmon_warn_shown, i, sysmon_warn_el);
            sysmon_xf_to(a, sysmon_xf_gf, sysmon_xf_s[i]);
        }
        sysmon_xf_ready = true;
    }
    uint8_t gf = sysmon_xf_gf;
    float   e  = sysmon_smooth((float)timer_elapsed32(sysmon_xf_t0) / SYSMON_XFADE_MS);
    float light[RGB_MATRIX_LED_COUNT][3], top = 0;
    for (uint8_t i = 0; i < RGB_MATRIX_LED_COUNT; i++) {
        RGB b = sysmon_fb[i];
        if (gb != sysmon_xf_tg || b.r != sysmon_xf_tc[i].r || b.g != sysmon_xf_tc[i].g || b.b != sysmon_xf_tc[i].b) {
            sysmon_xf_to(b, gb, sysmon_xf_t[i]);
            sysmon_xf_tc[i] = b;
        }
        float p[3];
        for (uint8_t c = 0; c < 3; c++) p[c] = sysmon_xf_s[i][c] + (sysmon_xf_t[i][c] - sysmon_xf_s[i][c]) * e;
        sysmon_xf_light(p, light[i]);
        top = MAX(top, MAX(MAX(light[i][0], light[i][1]), light[i][2]));
    }
    sysmon_xf_tg = gb;
    int g        = (int)ceilf(top / 255 - 0.001f); // the brightest key at full PWM
    if (gb > gf) {
        // at least what a full-PWM key going the same way would need, so it
        // arrives at the bars' own however dim they are
        float lo = (1 - e) * sysmon_cbrt(gf) + e * sysmon_cbrt(gb);
        g        = MAX(g, (int)ceilf(lo * lo * lo - 0.001f));
    } else {
        g = MAX(g, gf);
    }
    g = MIN(MAX(g, 1), 255);
    for (uint8_t i = 0; i < RGB_MATRIX_LED_COUNT; i++) {
        uint8_t o[3];
        for (uint8_t c = 0; c < 3; c++) o[c] = (uint8_t)MIN(light[i][c] / g + 0.5f, 255.0f);
        rgb_matrix_set_color(i, o[0], o[1], o[2]);
    }
    return (uint8_t)g;
}

// Write the bars' frame, blended with the previous display while a fade
// runs, and request the global current it goes out at. Outside a fade the
// bars are scaled down where the current is still above their own, never
// up: below it they just show dimmer until the current has caught up.
static void sysmon_flush(uint8_t gcc_bars) {
    if (!sysmon_xfading()) {
        uint8_t g = sysmon_xf_from == DISP_EFFECT ? gcc_bars : sysmon_gcc_toward(gcc_bars); // a cut jumps
        sysmon_write_gcc(g);
        for (uint8_t i = 0; i < RGB_MATRIX_LED_COUNT; i++) {
            RGB c = sysmon_gcc_scale(sysmon_fb[i], MIN(gcc_bars, g), g);
            rgb_matrix_set_color(i, c.r, c.g, c.b);
        }
        return;
    }
    sysmon_write_gcc(sysmon_xf_frame(gcc_bars));
}

// --- messages ----------------------------------------------------------------

// A MESSAGE (layout in the protocol notes): the bars dim out over
// SYSMON_MSG_DIM_MS, the text scrolls through on dark at a steady pace in the
// boot text's font, and once its last column has gone the bars fade back in
// the way they fade in after the boot rainbow, from dark. The bars keep
// running underneath, so they come back as they are by then.
#define SYSMON_MSG_DIM_MS 300
#define SYSMON_MSG_CPS 8 // columns per second: about 9.5 s for "CPU STAPM 28W"

static void sysmon_message(const uint8_t *p) {
    if (!p[5]) return; // no text: a capability probe
    // A number reported earlier is about an earlier message: a restarted host
    // numbers from 1 again, and the pad keeps its reports while it has power.
    // (Cleared first: the drop report below, of the one this replaces, must
    // stand even if that one carried the same number -- two hosts at once.)
    if (p[4]) {
        if (msg_played_seq == p[4]) msg_played_seq = 0;
        if (msg_dropped_seq == p[4]) msg_dropped_seq = 0;
    }
    // A new one replaces one waiting or playing: that one is dropped.
    sysmon_msg_report_drop();
    msg_seq = p[4]; // data[7]
    uint8_t q = 0;
    for (; q < sizeof(msg_text) - 1 && p[5 + q]; q++) msg_text[q] = (char)p[5 + q];
    msg_text[q]     = 0;
    msg_hue         = p[1];
    msg_sat         = p[2];
    msg_cps         = p[3] ? MIN(MAX(p[3], 2), 40) : SYSMON_MSG_CPS;
    boot_text_built = NULL; // same buffer, new contents
    if (msg_playing) {
        msg_t0 = timer_read32() - SYSMON_MSG_DIM_MS; // already dark: straight to the text
    } else {
        msg_waiting = true;
    }
}

// Draw the message into sysmon_fb, over the bars rendered there. False once
// it has finished. A pure function of the time since it started.
static bool sysmon_msg_render(void) {
    uint32_t t = timer_elapsed32(msg_t0);
    if (t < SYSMON_MSG_DIM_MS) {
        // even steps in lightness (cube-root space), down to exactly 0
        float e = 1 - sysmon_smooth((float)t / SYSMON_MSG_DIM_MS), k = e * e * e;
        for (uint8_t i = 0; i < RGB_MATRIX_LED_COUNT; i++) {
            RGB c        = sysmon_fb[i];
            sysmon_fb[i] = (RGB){.r = (uint8_t)(c.r * k), .g = (uint8_t)(c.g * k), .b = (uint8_t)(c.b * k)};
        }
        return true;
    }
    t -= SYSMON_MSG_DIM_MS;
    boot_text_build(msg_text, false);
    // Column 0 enters at the right edge; done when the last one has left.
    uint32_t len = ((uint32_t)boot_text_ncols + 4) * 1000 / msg_cps;
    if (t >= len) return false;
    int32_t p    = (int32_t)(t * msg_cps * 256 / 1000) - 4 * 256;
    int32_t base = p >> 8; // arithmetic shift: floors
    uint8_t fr   = (uint8_t)(p & 0xFF);
    RGB     ink  = hsv_to_rgb((HSV){msg_hue, msg_sat, 255});
    for (uint8_t r = 0; r < 6; r++) {
        for (uint8_t c = 0; c < 4; c++) {
            // between two columns, a blend of both; lifted like a partial bar
            // segment so a key the text is gliding across stays visible at a
            // very low current
            uint8_t v = boot_lerp(boot_text_px(base + c, r), boot_text_px(base + c + 1, r), fr);
            if (v) v = (uint8_t)sysmon_visible_pwm(v);
            sysmon_fb[BOOT_KEY(r, c)] = boot_rgb_scale(ink, v);
        }
    }
    return true;
}

// --- LED trace ---------------------------------------------------------------

// A flight recorder, always running: every frame as it goes to the LED chip --
// the global current, every key's PWM, what was showing -- in a ring holding
// the last SYSMON_TRACE_FRAMES frames (about 13 s). TRACE freezes it so the
// host can read it out. It records the values sent, not the light: how the
// LEDs turn those into light (very low PWM at a high current, say) is
// outside what it can show.
#define SYSMON_TRACE_FRAMES 768
typedef struct {
    uint16_t t;           // ms, the timer's low 16 bits
    uint8_t  gcc;         // the global current the frame went out at
    uint8_t  info;        // bits 0-1 display (DISP_*); bits 2-3 GCC change
                          // (1 rising: PWM sent first, 2 falling: GCC first);
                          // bit 4 cross-fading; bit 5 a message is showing
    uint8_t  px[6][4][3]; // PWM, pad rows top to bottom, columns left to right, R G B
} sysmon_trace_frame_t;
_Static_assert(sizeof(sysmon_trace_frame_t) == 76, "trace frame layout");

extern uint8_t              g_pwm_buffer[DRIVER_COUNT][ISSI_MAX_LEDS]; // the driver's, as sent
extern bool                 g_pwm_buffer_update_required[DRIVER_COUNT]; // ...and whether it changed
static sysmon_trace_frame_t sysmon_trace[SYSMON_TRACE_FRAMES];
static uint16_t             sysmon_trace_head;  // the next frame to write
static uint16_t             sysmon_trace_count; // frames held
static bool                 sysmon_trace_frozen;

// A key's PWM as the driver holds it (what goes out with the next flush).
static RGB sysmon_led_get(uint8_t i) {
    is31_led led;
    memcpy_P(&led, &g_is31_leds[i], sizeof(led));
    return (RGB){.r = g_pwm_buffer[led.driver][led.r], .g = g_pwm_buffer[led.driver][led.g], .b = g_pwm_buffer[led.driver][led.b]};
}

// Record the frame just drawn (once per frame, after the GCC commit).
static void sysmon_trace_frame(void) {
    uint8_t change      = sysmon_trace_change;
    sysmon_trace_change = 0;
    if (sysmon_trace_frozen) return;
    sysmon_trace_frame_t *f = &sysmon_trace[sysmon_trace_head];
    f->t                    = (uint16_t)timer_read32();
    f->gcc                  = sysmon_gcc;
    f->info                 = (uint8_t)((sysmon_disp & 3) | (change << 2) | (sysmon_xfading() ? 0x10 : 0) | (msg_playing ? 0x20 : 0));
    for (uint8_t r = 0; r < 6; r++) {
        for (uint8_t c = 0; c < 4; c++) {
            RGB px         = sysmon_led_get(sysmon_row_leds[r][c]);
            f->px[r][c][0] = px.r;
            f->px[r][c][1] = px.g;
            f->px[r][c][2] = px.b;
        }
    }
    sysmon_trace_head = (uint16_t)((sysmon_trace_head + 1) % SYSMON_TRACE_FRAMES);
    if (sysmon_trace_count < SYSMON_TRACE_FRAMES) sysmon_trace_count++;
}

// --- saved trace ---

// Holding the save key (see pre_process_record_user) copies the ring, oldest
// frame first, to one of SYSMON_SAVE_SLOTS slots in the flash, just below the
// EEPROM's wear-leveling area and far above the firmware. A copy survives
// power-off and reflashing (a UF2 only rewrites the blocks it carries). Each
// save goes to the slot after the newest, so the last four are kept. TRACE 4
// reads them out.
//
// Erasing and programming the flash stops the MCU (interrupts off: its code
// runs from that flash), so the work is spread over housekeeping passes: one
// 4 KiB sector erase (~45 ms) or four 256-byte pages (~3 ms) a pass, about a
// second in all, and only once USB has settled, as for the boot counter (or
// after SYSMON_SAVE_WAIT_MS without it). The ring stays frozen meanwhile and
// the header goes last, so a save cut short leaves no valid slot. Done, the
// whole pad flashes white once.
#define SYSMON_SAVE_SLOTS 4
#define SYSMON_SAVE_SLOT_SIZE (64UL * 1024)
#define SYSMON_SAVE_BASE ((uint32_t)(WEAR_LEVELING_RP2040_FLASH_BASE) - SYSMON_SAVE_SLOTS * SYSMON_SAVE_SLOT_SIZE) // flash offset
#define SYSMON_SAVE_MAGIC 0x31435254UL // "TRC1"
#define SYSMON_SAVE_WAIT_MS 5000
_Static_assert(FLASH_PAGE_SIZE + SYSMON_TRACE_FRAMES * sizeof(sysmon_trace_frame_t) <= SYSMON_SAVE_SLOT_SIZE, "a saved trace fits its slot");
_Static_assert(SYSMON_SAVE_BASE % FLASH_SECTOR_SIZE == 0, "slots start on a sector");

typedef struct {
    uint32_t magic;      // SYSMON_SAVE_MAGIC (erased flash reads 0xFFFFFFFF)
    uint32_t seq;        // saves so far, this one included: the highest is the newest
    uint32_t saved_at;   // ms since power-up when it was saved
    uint32_t boot;       // the power-up counter: which start of the pad it was
    uint16_t frames;     // frames after the header, oldest first
    uint8_t  frame_size; // sizeof(sysmon_trace_frame_t)
    uint8_t  why;        // SYSMON_SAVED_BY_*
} sysmon_save_hdr_t;

// The image's end in flash (code and constants, then .data's initial values).
extern uint8_t __textdata_base__[], __data_base__[], __data_end__[];

static uint8_t           save_stage; // 0 idle, 1 erasing, 2 writing the frames, 3 the header
static uint8_t           save_slot;
static uint16_t          save_step;              // sector or page
static uint16_t          save_head, save_count;  // the ring as it was frozen
static bool              save_was_frozen;        // (by the host: it stays frozen)
static uint32_t          save_t0;
static sysmon_save_hdr_t save_hdr;
static uint32_t          save_done_at; // when the last save finished, for the flash (0: none)

static const sysmon_save_hdr_t *sysmon_save_hdr(uint8_t slot) {
    return (const sysmon_save_hdr_t *)(XIP_BASE + SYSMON_SAVE_BASE + (uint32_t)slot * SYSMON_SAVE_SLOT_SIZE);
}

static void sysmon_trace_save_start(uint8_t why) {
    if (save_stage) return; // one at a time
    // never over the firmware (it would have to grow nine-fold)
    if ((uint32_t)__textdata_base__ + (uint32_t)(__data_end__ - __data_base__) > XIP_BASE + SYSMON_SAVE_BASE) return;
    uint32_t seq    = 0;
    int8_t   newest = -1;
    for (uint8_t s = 0; s < SYSMON_SAVE_SLOTS; s++) {
        const sysmon_save_hdr_t *h = sysmon_save_hdr(s);
        if (h->magic == SYSMON_SAVE_MAGIC && h->seq >= seq) {
            seq    = h->seq;
            newest = (int8_t)s;
        }
    }
    save_slot           = (uint8_t)((newest + 1) % SYSMON_SAVE_SLOTS);
    save_was_frozen     = sysmon_trace_frozen;
    sysmon_trace_frozen = true;
    save_head           = sysmon_trace_head;
    save_count          = sysmon_trace_count;
    save_hdr            = (sysmon_save_hdr_t){
        .magic = SYSMON_SAVE_MAGIC, .seq = seq + 1, .saved_at = timer_read32(), .boot = boot_counter, .frames = save_count, .frame_size = sizeof(sysmon_trace_frame_t), .why = why};
    save_step  = 0;
    save_stage = 1;
    save_t0    = timer_read32();
}

static void sysmon_flash_program(uint32_t offset, const uint8_t *page) {
    uint32_t irq = save_and_disable_interrupts();
    flash_range_program(offset, page, FLASH_PAGE_SIZE);
    restore_interrupts(irq);
}

// One step of a save, from housekeeping.
static void sysmon_trace_save_step(bool usb_settled) {
    if (!save_stage) return;
    if (!usb_settled && timer_elapsed32(save_t0) < SYSMON_SAVE_WAIT_MS) return;
    uint32_t       base  = SYSMON_SAVE_BASE + (uint32_t)save_slot * SYSMON_SAVE_SLOT_SIZE;
    uint32_t       bytes = (uint32_t)save_count * sizeof(sysmon_trace_frame_t);
    static uint8_t page[FLASH_PAGE_SIZE]; // the source must not be the flash itself
    if (save_stage == 1) {
        uint32_t irq = save_and_disable_interrupts();
        flash_range_erase(base + (uint32_t)save_step * FLASH_SECTOR_SIZE, FLASH_SECTOR_SIZE);
        restore_interrupts(irq);
        if (++save_step * (uint32_t)FLASH_SECTOR_SIZE >= FLASH_PAGE_SIZE + bytes) {
            save_stage = 2;
            save_step  = 0;
        }
        return;
    }
    if (save_stage == 2) {
        for (uint8_t k = 0; k < 4 && (uint32_t)save_step * FLASH_PAGE_SIZE < bytes; k++, save_step++) {
            for (uint16_t b = 0; b < FLASH_PAGE_SIZE; b++) {
                uint32_t pos = (uint32_t)save_step * FLASH_PAGE_SIZE + b;
                uint8_t  v   = 0xFF;
                if (pos < bytes) {
                    uint16_t n = (uint16_t)(pos / sizeof(sysmon_trace_frame_t));
                    uint16_t i = (uint16_t)((save_head + SYSMON_TRACE_FRAMES - save_count + n) % SYSMON_TRACE_FRAMES);
                    v          = ((const uint8_t *)&sysmon_trace[i])[pos % sizeof(sysmon_trace_frame_t)];
                }
                page[b] = v;
            }
            sysmon_flash_program(base + FLASH_PAGE_SIZE + (uint32_t)save_step * FLASH_PAGE_SIZE, page);
        }
        if ((uint32_t)save_step * FLASH_PAGE_SIZE >= bytes) save_stage = 3;
        return;
    }
    memset(page, 0xFF, sizeof(page)); // the header, last
    memcpy(page, &save_hdr, sizeof(save_hdr));
    sysmon_flash_program(base, page);
    save_stage          = 0;
    sysmon_trace_frozen = save_was_frozen;
    save_done_at        = sysmon_stamp();
}

// The answer to a save, drawn over whatever shows: once it is done, every key
// flashes white for SYSMON_SAVE_FLASH_MS.
#define SYSMON_SAVE_FLASH_MS 150
static void sysmon_save_flash(void) {
    if (!save_done_at) return;
    if (timer_elapsed32(save_done_at) >= SYSMON_SAVE_FLASH_MS) {
        save_done_at = 0;
        return;
    }
    rgb_matrix_set_color_all(255, 255, 255);
}

// TRACE: p = data[3..]; layout in the protocol notes.
static void sysmon_trace_cmd(uint8_t *p) {
    if (p[0] == 2) {
        uint32_t off = (uint32_t)p[1] | ((uint32_t)p[2] << 8) | ((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 24);
        uint32_t end = (uint32_t)sysmon_trace_count * sizeof(sysmon_trace_frame_t);
        for (uint8_t k = 0; k < 24; k++, off++) {
            uint8_t v = 0;
            if (sysmon_trace_frozen && off < end) {
                uint16_t n = (uint16_t)(off / sizeof(sysmon_trace_frame_t)); // oldest first
                uint16_t i = (uint16_t)((sysmon_trace_head + SYSMON_TRACE_FRAMES - sysmon_trace_count + n) % SYSMON_TRACE_FRAMES);
                v          = ((const uint8_t *)&sysmon_trace[i])[off % sizeof(sysmon_trace_frame_t)];
            }
            p[5 + k] = v;
        }
        return;
    }
    if (p[0] == 4) {
        uint32_t off = (uint32_t)p[1] | ((uint32_t)p[2] << 8) | ((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 24);
        for (uint8_t k = 0; k < 24; k++, off++) {
            p[5 + k] = off < SYSMON_SAVE_SLOTS * SYSMON_SAVE_SLOT_SIZE ? *(const uint8_t *)(XIP_BASE + SYSMON_SAVE_BASE + off) : 0xFF;
        }
        return;
    }
    if (p[0] == 5) sysmon_trace_save_start(SYSMON_SAVED_BY_HOST);
    if (save_stage) {
        // the ring is being saved: change only what it goes back to after
        if (p[0] == 1) save_was_frozen = true;
        if (p[0] == 3) save_was_frozen = false;
    } else {
        if (p[0] == 1) sysmon_trace_frozen = true;
        if (p[0] == 3) {
            sysmon_trace_count  = 0;
            sysmon_trace_frozen = false;
        }
    }
    p[5]  = (uint8_t)sysmon_trace_count;
    p[6]  = (uint8_t)(sysmon_trace_count >> 8);
    p[7]  = sizeof(sysmon_trace_frame_t);
    p[8]  = sysmon_trace_frozen;
    p[9]  = (uint8_t)SYSMON_TRACE_FRAMES;
    p[10] = (uint8_t)(SYSMON_TRACE_FRAMES >> 8);
    p[11] = SYSMON_SAVE_SLOTS;
    p[12] = save_stage != 0;
    p[13] = (uint8_t)(SYSMON_SAVE_SLOT_SIZE / 1024);
}

// Display priority, highest first:
//   1. a playing boot animation (it is short; finish it even if the host
//      connects meanwhile, and a replay preview must show over live data)
//   2. fresh host data: the sysmon bars (cross-fading in from 1, 3 or 4), or
//      a message over them
//   3. the host has never connected: the static boot rainbow
//   4. the host went silent without a goodbye: the disconnect warning, for
//      SYSMON_WARN_MS after the no-data timeout
//   5. otherwise (goodbye, warning over, or rainbow dismissed): the regular
//      RGB effect
// Every mode draws all the keys; this runs once per frame.
//
// All of it runs from the RGB matrix's indicator hook, which QMK skips while
// the matrix is switched off (RGB_TOG: NumLock off, then the top-left key).
// That is the pad's lights-off switch and is left to work as one: the bars go
// dark with everything else until it is pressed again. The FRAME B echo
// reports it (SYSMON_STATUS_DARK), so the host can say why the pad is dark.
static bool sysmon_indicators(void) {
    static bool geometry_ready = false;
    if (!geometry_ready) {
        geometry_ready = true;
        boot_geometry_init();
    }
    // The power-up animation plays on the first frame -- unless a key or the
    // host came first, or the matrix was off at power-up (housekeeping_task_
    // user disarms it then). No pick is used up when it doesn't play.
    if (boot_armed) {
        boot_armed = false;
        if (!boot_dismissed && sysmon_last_update == 0) {
            boot_powerup_pick = boot_next_pick() + 1;
            boot_start(boot_powerup_pick - 1, 1, 1, false);
        }
    }

    // Frames that stopped (the matrix switched off, a suspend) leave
    // sysmon_disp at what showed before. The pad was dark since, so whatever
    // comes next starts from dark: the bars fade in from it, rather than from
    // a stale rainbow or warning frame. (After a USB suspend the host itself
    // was asleep: the regular effect shows until it streams again, and the
    // bars cut in from that, as before.)
    if (sysmon_went_dark) {
        sysmon_went_dark = false;
        if (sysmon_disp != DISP_EFFECT) sysmon_disp = DISP_DARK;
    }

    if (boot_render()) {
        sysmon_set_gcc(BOOT_BRIGHTNESS);
        sysmon_disp = DISP_BOOT;
        // A message is for the bars: it waits out the animation (the power-up
        // one can finish after the host connects, a replay preview can play
        // over live data) and plays once they are back.
        sysmon_msg_hold();
        return false;
    }
    if (warn_preview_render()) {
        sysmon_set_gcc(sysmon_last_update ? sysmon_slewed_val(false) : BOOT_BRIGHTNESS);
        sysmon_disp = DISP_WARN;
        sysmon_msg_hold(); // it waits this out too, as the boot animation's
        return false;
    }
    uint32_t timeout_ms = sysmon_timeout_ms();
    uint32_t since      = sysmon_since();
    if (!sysmon_host_live()) {
        // A message is for the bars only: the host is gone, so drop it (the
        // host is told when it is back, and names that limit again).
        sysmon_msg_drop();
        sysmon_ask = false;
        if (sysmon_last_update == 0 && !boot_dismissed) {
            sysmon_set_gcc(BOOT_BRIGHTNESS);
            boot_render_static();
            sysmon_disp = DISP_BOOT;
            return false;
        }
        if (sysmon_last_update != 0 && !sysmon_goodbye && !sysmon_quiet && since - timeout_ms < SYSMON_WARN_MS) {
            sysmon_set_gcc(sysmon_slewed_val(false));
            sysmon_warn_el    = since - timeout_ms;
            sysmon_warn_shown = SYSMON_WARN_STYLE;
            for (uint8_t i = 0; i < RGB_MATRIX_LED_COUNT; i++) {
                RGB c = sysmon_warn_rgb(SYSMON_WARN_STYLE, i, sysmon_warn_el);
                rgb_matrix_set_color(i, c.r, c.g, c.b);
            }
            sysmon_disp = DISP_WARN;
            return false;
        }
        // No fresh data from the host; restore the stock global current and
        // let the normal RGB effect run at its own brightness
        sysmon_set_gcc(255);
        sysmon_disp = DISP_EFFECT;
        return true;
    }

    if (sysmon_disp != DISP_SYSMON) {
        sysmon_xf_from    = sysmon_disp; // blend in from whatever was showing
        sysmon_xf_t0      = timer_read32();
        sysmon_xf_ready   = false;
        sysmon_entered    = sysmon_xf_t0;
        sysmon_disp       = DISP_SYSMON;
    }
    bool snap         = timer_elapsed32(sysmon_entered) < SYSMON_SNAP_MS;
    sysmon_glide_snap = snap && sysmon_xf_from != DISP_EFFECT; // a cut from the effect glides
    uint8_t gcc_bars  = sysmon_gcc_for(sysmon_slewed_val(snap));
    sysmon_gcc_bars  = gcc_bars;

    uint32_t glide_ms = (uint32_t)sysmon_glide_units * 4;

    static sysmon_glide_t glides[6];
    static sysmon_glide_t ghost_glides[6];
    for (uint8_t row = 0; row < 6; row++) {
        sysmon_render_row(row, &glides[row], &ghost_glides[row], glide_ms, timeout_ms);
    }
    sysmon_glide_snap = false;

    // A message waits for the fade into the bars to finish, then draws over
    // them; at its end the bars fade back in from the dark it ends on.
    if (msg_waiting && !sysmon_xfading()) {
        msg_waiting = false;
        msg_playing = true;
        msg_t0      = timer_read32();
    }
    if (msg_playing && !sysmon_msg_render()) {
        msg_playing     = false;
        if (msg_seq) msg_played_seq = msg_seq;
        msg_seq = 0;
        sysmon_xf_from  = DISP_DARK;
        sysmon_xf_t0    = timer_read32();
        sysmon_xf_ready = false;
    }
    sysmon_flush(gcc_bars); // and picks the frame's current

    return false;
}

// QMK renders a frame in chunks (five here) and calls this after each one.
// The sysmon display covers every key, so it is drawn once, on the frame's
// first call, kept, and put back over what each later chunk of the regular
// effect drew. (Drawing it on every call did all the work five times over.
// Rendering in one pass instead would have changed the stock effects:
// Raindrops adds a drop per render call.)
//
// Once the frame is complete its global current goes out with it
// (sysmon_gcc_commit) and the trace records it, once per frame.
//
// A sysmon frame is complete as soon as it is drawn, so its current goes out
// on the first call -- never waiting for a last call that an effect rendering
// in one call, like Digital Rain, never makes. When that commit sends the PWM
// itself (the current moves: fades, slews), the restore over the last chunk
// leaves the buffer as sent, so QMK's regular flush is told there is nothing
// new.
//
// A regular-effect frame is complete on its last call, or for a one-call
// effect on its first: recognised by two single-call frames in a row (QMK
// restarting a frame after its first chunk, on an effect change, leaves one).
// Not every one-call frame repaints every key (Pixel Flow's waiting frames,
// Jellybean Raindrops), so the first effect frame after the sysmon display
// starts from black: the stock current then never lights up a leftover bars
// or rainbow frame.
static RGB  sysmon_frame[RGB_MATRIX_LED_COUNT]; // this frame, as sysmon drew it
static bool sysmon_frame_effect;                // ...or the regular effect shows

bool rgb_matrix_indicators_advanced_user(uint8_t led_min, uint8_t led_max) {
    static uint8_t calls      = 0;    // calls in the frame so far
    static uint8_t singles    = 0;    // frames in a row that had just one call
    static bool    committed  = false; // this frame's current is out
    static bool    was_effect = false; // the last frame showed the regular effect
    bool           early      = false; // commit on the first call
    if (led_min == 0) {
        // Two in a row: one alone can be a frame QMK restarted after its
        // first chunk (an effect change), not a one-call effect.
        singles             = calls == 1 ? (singles < 2 ? singles + 1 : 2) : 0;
        calls               = 0;
        committed           = false;
        sysmon_gcc_flushed  = false;
        sysmon_frame_effect = sysmon_indicators();
        if (!sysmon_frame_effect) {
            sysmon_save_flash(); // kept with the frame
            for (uint8_t i = 0; i < RGB_MATRIX_LED_COUNT; i++) sysmon_frame[i] = sysmon_led_get(i);
        } else if (!was_effect) {
            // handing over to the effect: from black, not the last bars frame
            rgb_matrix_set_color_all(0, 0, 0);
        }
        was_effect = sysmon_frame_effect;
        early      = sysmon_frame_effect ? singles >= 2 : true;
    } else if (!sysmon_frame_effect && led_max >= RGB_MATRIX_LED_COUNT) {
        // Once, on the last chunk (only the buffer after it goes out): every
        // key, not just this chunk's -- some effects (Raindrops, Pixel Rain)
        // light a random key anywhere on each call.
        for (uint8_t i = 0; i < RGB_MATRIX_LED_COUNT; i++) rgb_matrix_set_color(i, sysmon_frame[i].r, sysmon_frame[i].g, sysmon_frame[i].b);
        // The buffer is now the frame the commit already sent, if it sent it:
        // QMK's regular flush needn't send it again.
        if (sysmon_gcc_flushed) {
            for (uint8_t d = 0; d < DRIVER_COUNT; d++) g_pwm_buffer_update_required[d] = false;
        }
    }
    if (sysmon_frame_effect) sysmon_save_flash(); // over each chunk the effect drew
    if (calls < UINT8_MAX) calls++;
    if (!committed && (early || led_max >= RGB_MATRIX_LED_COUNT)) {
        committed = true;
        sysmon_gcc_commit();
        sysmon_trace_frame();
    }
    return sysmon_frame_effect;
}

#endif // RGB_MATRIX_ENABLE
