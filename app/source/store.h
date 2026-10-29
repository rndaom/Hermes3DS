// Persistent state on the SD card (protocol.md §2): device key and the paired server.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "net.h"

#define STORE_DIR "sdmc:/3ds/hermes"

void store_init(void);
// Loads sdmc:/3ds/hermes/device_key, creating it (32 random bytes) on first run. false on SD failure.
bool store_device_key(uint8_t priv[32]);
bool store_load_server(ServerInfo *s);
bool store_save_server(const ServerInfo *s);
void store_forget_server(void);

// App settings (sdmc:/3ds/hermes/settings.json); defaults when the file is missing.
typedef struct {
    bool sounds;         // UI sounds (reply arrived, needs you, sent, recording)
    bool speak_replies;  // play Hermes' spoken replies as they arrive
} Settings;

void store_load_settings(Settings *s);
bool store_save_settings(const Settings *s);
