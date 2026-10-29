// Small append-only log on the SD card (sdmc:/3ds/hermes/log.txt) for debugging on real hardware.
#pragma once

void log_open(void);
void log_close(void);
void log_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
