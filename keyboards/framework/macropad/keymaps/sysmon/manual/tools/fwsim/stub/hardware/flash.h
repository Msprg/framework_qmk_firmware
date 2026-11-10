// Copyright 2026 Msprg
// SPDX-License-Identifier: GPL-2.0-or-later
// fwsim stand-in for the Pico SDK's hardware/flash.h: the saved-trace slots
// live in a RAM buffer (fwsim_flash), as if it were the XIP-mapped flash.
#pragma once
#include <stddef.h>
#include <stdint.h>

#define FLASH_PAGE_SIZE 256u
#define FLASH_SECTOR_SIZE 4096u
// four 64 KiB slots below it: they start at offset 0 of fwsim_flash
#define WEAR_LEVELING_RP2040_FLASH_BASE (4u * 64u * 1024u)
extern uint8_t fwsim_flash[WEAR_LEVELING_RP2040_FLASH_BASE];
#define XIP_BASE ((uintptr_t)fwsim_flash)

void flash_range_erase(uint32_t flash_offs, size_t count);
void flash_range_program(uint32_t flash_offs, const uint8_t *data, size_t count);
