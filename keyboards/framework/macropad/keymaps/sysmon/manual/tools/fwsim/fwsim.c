// Copyright 2026 Msprg
// SPDX-License-Identifier: GPL-2.0-or-later
//
// fwsim: the sysmon firmware's display code, run on a PC.
//
// keymap.c is compiled unchanged (#included below), together with QMK's real
// colour maths (color.c), lib8tion, the IS31FL3743A register map and the
// macropad's LED table. Only I/O is stubbed: a millisecond clock this program
// advances, the LED driver's PWM buffer, a RAM "flash", NumLock and the
// lighting switch. It is driven through the firmware's own entry points --
// via_custom_value_command_kb (what the host and bootanim_preview.py send),
// pre_process_record_user (keys), housekeeping_task_user, and
// rgb_matrix_indicators_advanced_user called in QMK's chunk pattern every
// 16 ms -- and reads each frame back from the firmware's own LED flight
// recorder (sysmon_trace), the same data `bootanim_preview.py trace` reads.
//
// Commands on stdin, one per line:
//   via HEX64          a 32-byte VIA report (data[0] = 0x07 ...), as the host sends it
//   wait MS            run the clock MS milliseconds
//   key ROW COL P KC   matrix key ROW,COL pressed (P 1) or released (0), keycode KC
//   numlock 0|1        the host's NumLock LED
//   lights 0|1         the RGB matrix on or off (RGB_TOG)
//   suspend 0|1        USB suspend (the laptop asleep)
//   eeprom N           the EEPROM user word (the boot counter) before the first frame
// On stdout, one line per frame:
//   F MS GCC INFO HEX144   time, global current, trace info byte, then PWM R G B
//                          for the 24 keys, pad rows top to bottom, keys left to right
// and E HEX64 for each VIA echo.
//
// Build: see manual/img/animations.py, which extracts keymap.c from a commit
// and compiles this with the host's gcc.

#include <stdio.h>
#include <stdlib.h>

#include "qmk_stub.h"
#include "keymap.c"
#include "keyboards/framework/macropad/macropad.c"
#include "quantum/color.c"

// --- what QMK and the Pico SDK would provide -----------------------------------

uint16_t rand16seed = 1337; // lib8tion.c
uint8_t  g_pwm_buffer[DRIVER_COUNT][ISSI_MAX_LEDS];
bool     g_pwm_buffer_update_required[DRIVER_COUNT];
uint8_t  fwsim_flash[WEAR_LEVELING_RP2040_FLASH_BASE];
uint8_t  __textdata_base__[1], __data_base__[1], __data_end__[1];

static uint32_t now_ms     = 1000; // never 0: the firmware reads 0 as "never"
static uint32_t eeprom     = 0;
static bool     lights     = true;
static bool     suspended  = false;
static led_t    host_leds  = {.raw = 0};

uint32_t timer_read32(void) {
    return now_ms;
}
uint32_t timer_elapsed32(uint32_t last) {
    return now_ms - last;
}
uint32_t eeconfig_read_user(void) {
    return eeprom;
}
void eeconfig_update_user(uint32_t val) {
    eeprom = val;
}
void soft_reset_keyboard(void) {}
bool usb_connected_state(void) {
    return true;
}
led_t host_keyboard_led_state(void) {
    return host_leds;
}
void layer_on(uint8_t layer) {
    (void)layer;
}
void layer_off(uint8_t layer) {
    (void)layer;
}
void action_exec(keyevent_t event) {
    (void)event;
}

void IS31FL_write_single_register(uint8_t addr, uint8_t reg, uint8_t data) {
    (void)addr;
    (void)reg;
    (void)data;
}
void IS31FL_unlock_register(uint8_t addr, uint8_t page) {
    (void)addr;
    (void)page;
}

// As QMK's IS31FL_RGB_set_color: into the driver's PWM buffer, by the LED table.
void rgb_matrix_set_color(int index, uint8_t red, uint8_t green, uint8_t blue) {
    if (index < 0 || index >= RGB_MATRIX_LED_COUNT) return;
    is31_led led;
    memcpy_P(&led, &g_is31_leds[index], sizeof(led));
    g_pwm_buffer[led.driver][led.r]          = red;
    g_pwm_buffer[led.driver][led.g]          = green;
    g_pwm_buffer[led.driver][led.b]          = blue;
    g_pwm_buffer_update_required[led.driver] = true;
}
void rgb_matrix_set_color_all(uint8_t red, uint8_t green, uint8_t blue) {
    for (int i = 0; i < RGB_MATRIX_LED_COUNT; i++) rgb_matrix_set_color(i, red, green, blue);
}
bool rgb_matrix_is_enabled(void) {
    return lights;
}
bool rgb_matrix_get_suspend_state(void) {
    return suspended;
}
static void fwsim_flush(void) {
    for (int d = 0; d < DRIVER_COUNT; d++) g_pwm_buffer_update_required[d] = false;
}
const rgb_matrix_driver_t rgb_matrix_driver = {.flush = fwsim_flush};

void flash_range_erase(uint32_t flash_offs, size_t count) {
    if (flash_offs + count <= sizeof(fwsim_flash)) memset(fwsim_flash + flash_offs, 0xFF, count);
}
void flash_range_program(uint32_t flash_offs, const uint8_t *data, size_t count) {
    if (flash_offs + count <= sizeof(fwsim_flash)) memcpy(fwsim_flash + flash_offs, data, count);
}

// --- the main loop ----------------------------------------------------------------

#define FRAME_MS 16 // RGB_MATRIX_LED_FLUSH_LIMIT
#define CHUNK 5     // RGB_MATRIX_LED_PROCESS_LIMIT: (24 + 4) / 5

static void put_frame(uint32_t t, uint8_t gcc, uint8_t info, const uint8_t px[6][4][3]) {
    printf("F %lu %u %u ", (unsigned long)t, gcc, info);
    for (int r = 0; r < 6; r++)
        for (int c = 0; c < 4; c++) printf("%02x%02x%02x", px[r][c][0], px[r][c][1], px[r][c][2]);
    putchar('\n');
}

static void frame(void) {
    static const uint8_t dark[6][4][3];
    if (!lights || suspended) {
        // QMK skips the matrix task: nothing is drawn, nothing recorded
        put_frame(now_ms, 0, DISP_DARK, dark);
        return;
    }
    uint16_t head = sysmon_trace_head;
    for (int lo = 0; lo < RGB_MATRIX_LED_COUNT; lo += CHUNK) {
        int hi = lo + CHUNK > RGB_MATRIX_LED_COUNT ? RGB_MATRIX_LED_COUNT : lo + CHUNK;
        rgb_matrix_indicators_advanced_user((uint8_t)lo, (uint8_t)hi);
    }
    rgb_matrix_driver.flush();
    if (sysmon_trace_head != head) {
        const sysmon_trace_frame_t *f = &sysmon_trace[(sysmon_trace_head + SYSMON_TRACE_FRAMES - 1) % SYSMON_TRACE_FRAMES];
        put_frame(now_ms, f->gcc, f->info, f->px);
    }
}

static void run(uint32_t ms) {
    while (ms--) {
        now_ms++;
        housekeeping_task_user();
        if (now_ms % FRAME_MS == 0) frame();
    }
}

static int hexval(int ch) {
    if (ch >= '0' && ch <= '9') return ch - '0';
    if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
    if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
    return -1;
}

int main(void) {
    memset(fwsim_flash, 0xFF, sizeof(fwsim_flash)); // erased
    char line[512];
    while (fgets(line, sizeof(line), stdin)) {
        char     cmd[16] = {0};
        unsigned a = 0, b = 0, c = 0, d = 0;
        char     hex[256] = {0};
        if (sscanf(line, "%15s", cmd) != 1) continue;
        if (!strcmp(cmd, "via") && sscanf(line, "%*s %255s", hex) == 1) {
            uint8_t buf[32] = {0};
            for (int i = 0; i < 32 && hex[2 * i] && hex[2 * i + 1]; i++) buf[i] = (uint8_t)(hexval(hex[2 * i]) << 4 | hexval(hex[2 * i + 1]));
            via_custom_value_command_kb(buf, sizeof(buf));
            printf("E ");
            for (int i = 0; i < 32; i++) printf("%02x", buf[i]);
            putchar('\n');
        } else if (!strcmp(cmd, "wait") && sscanf(line, "%*s %u", &a) == 1) {
            run(a);
        } else if (!strcmp(cmd, "key") && sscanf(line, "%*s %u %u %u %u", &a, &b, &c, &d) == 4) {
            keyrecord_t rec = {.event = MAKE_KEYEVENT((uint8_t)a, (uint8_t)b, c != 0)};
            pre_process_record_user((uint16_t)d, &rec);
        } else if (!strcmp(cmd, "numlock") && sscanf(line, "%*s %u", &a) == 1) {
            host_leds.num_lock = a != 0;
        } else if (!strcmp(cmd, "lights") && sscanf(line, "%*s %u", &a) == 1) {
            lights = a != 0;
        } else if (!strcmp(cmd, "suspend") && sscanf(line, "%*s %u", &a) == 1) {
            if (a && !suspended) suspend_power_down_user();
            suspended = a != 0;
        } else if (!strcmp(cmd, "eeprom") && sscanf(line, "%*s %u", &a) == 1) {
            eeprom = a;
        } else {
            fprintf(stderr, "fwsim: unknown command: %s", line);
            return 2;
        }
    }
    return 0;
}
