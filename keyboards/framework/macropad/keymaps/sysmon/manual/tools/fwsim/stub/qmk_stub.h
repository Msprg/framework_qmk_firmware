// Copyright 2026 Msprg
// SPDX-License-Identifier: GPL-2.0-or-later
//
// QMK_KEYBOARD_H for fwsim: just enough of QMK for the sysmon keymap.c to
// compile on a PC. Maths and tables come from QMK's real sources (color.h,
// lib8tion, the IS31FL3743A register map, the macropad's config.h and LED
// table); only I/O is stubbed, in fwsim.c.
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "progmem.h"
#include "util.h" // MIN, MAX
#include "color.h"
#include "keyboards/framework/macropad/config.h"
#include "keyboards/framework/macropad/keymaps/sysmon/config.h"
#include "is31flcommon.h"

// --- keys -----------------------------------------------------------------
typedef uint8_t matrix_row_t; // MATRIX_COLS == 8

typedef struct {
    uint8_t col;
    uint8_t row;
} keypos_t;

typedef enum keyevent_type_t { TICK_EVENT = 0, KEY_EVENT = 1 } keyevent_type_t;

typedef struct {
    keypos_t        key;
    uint16_t        time;
    keyevent_type_t type;
    bool            pressed;
} keyevent_t;

typedef struct {
    keyevent_t event;
} keyrecord_t;

static inline bool IS_KEYEVENT(const keyevent_t event) {
    return event.type == KEY_EVENT;
}
#define MAKE_KEYEVENT(row_num, col_num, press) \
    ((keyevent_t){.key = (keypos_t){.row = (row_num), .col = (col_num)}, .pressed = (press), .time = 1, .type = KEY_EVENT})

void action_exec(keyevent_t event);

// The keymap array isn't used here: LAYOUT drops its keycodes unevaluated.
#define LAYOUT(...) {{0}}
enum _layers { _NUMLOCK, _FN, _FACTORY };
#define KC_P5 0x005D // HID Keypad 5, the replay key's keycode

typedef union {
    uint8_t raw;
    struct {
        bool num_lock : 1;
        bool caps_lock : 1;
        bool scroll_lock : 1;
        bool compose : 1;
        bool kana : 1;
        uint8_t reserved : 3;
    };
} led_t;
led_t host_keyboard_led_state(void);
void  layer_on(uint8_t layer);
void  layer_off(uint8_t layer);

// --- RGB matrix -----------------------------------------------------------
#define NO_LED 255
typedef struct {
    uint8_t x;
    uint8_t y;
} led_point_t;
typedef struct {
    uint8_t     matrix_co[MATRIX_ROWS][MATRIX_COLS];
    led_point_t point[RGB_MATRIX_LED_COUNT];
    uint8_t     flags[RGB_MATRIX_LED_COUNT];
} led_config_t;
extern led_config_t g_led_config;

typedef struct {
    void (*flush)(void);
} rgb_matrix_driver_t;
extern const rgb_matrix_driver_t rgb_matrix_driver;

void rgb_matrix_set_color(int index, uint8_t red, uint8_t green, uint8_t blue);
void rgb_matrix_set_color_all(uint8_t red, uint8_t green, uint8_t blue);
bool rgb_matrix_is_enabled(void);
bool rgb_matrix_get_suspend_state(void);

// --- the rest -----------------------------------------------------------------
uint32_t timer_read32(void);
uint32_t timer_elapsed32(uint32_t last);
uint32_t eeconfig_read_user(void);
void     eeconfig_update_user(uint32_t val);
void     soft_reset_keyboard(void);
