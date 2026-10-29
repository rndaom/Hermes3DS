// The notification LED (mcu::HWC): blinks yellow while Hermes waits for an answer (design notes).
#pragma once

#include <stdbool.h>

void led_init(void);
void led_exit(void);       // turns it off
void led_waiting(bool on); // cheap to call every frame; only state changes reach the MCU
