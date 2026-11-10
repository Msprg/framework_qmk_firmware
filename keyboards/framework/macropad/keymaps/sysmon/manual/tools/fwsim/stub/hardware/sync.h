// Copyright 2026 Msprg
// SPDX-License-Identifier: GPL-2.0-or-later
// fwsim stand-in for the Pico SDK's hardware/sync.h.
#pragma once
#include <stdint.h>
static inline uint32_t save_and_disable_interrupts(void) {
    return 0;
}
static inline void restore_interrupts(uint32_t status) {
    (void)status;
}
