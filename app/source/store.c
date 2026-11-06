#include "store.h"

#include <3ds.h>
#include <jansson.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "codec.h"
#include "log.h"
#include "noise.h"

#define KEY_PATH STORE_DIR "/device_key"
#define SERVER_PATH STORE_DIR "/server.json"

void store_init(void)
{
    mkdir("sdmc:/3ds", 0777);
    mkdir(STORE_DIR, 0777);
}

// Write to a temp file, then rename over the target, so a crash or pulled SD never leaves half a file.
static bool write_atomic(const char *path, const void *data, size_t len)
{
    char tmp[128];
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    FILE *f = fopen(tmp, "wb");
    if (!f) return false;
    bool ok = fwrite(data, 1, len, f) == len;
    ok = (fclose(f) == 0) && ok;
    if (ok) {
        remove(path);
        ok = rename(tmp, path) == 0;
    }
    if (!ok) remove(tmp);
    return ok;
}

bool store_device_key(uint8_t priv[32])
{
    FILE *f = fopen(KEY_PATH, "rb");
    if (f) {
        size_t n = fread(priv, 1, 32, f);
        fclose(f);
        if (n == 32) return true;
        log_printf("device_key is corrupt; making a new one (the PC will need pairing again)");
    }
    if (R_FAILED(PS_GenerateRandomBytes(priv, 32))) return false;
    if (!write_atomic(KEY_PATH, priv, 32)) {
        noise_wipe(priv, 32);
        return false;
    }
    log_printf("created device key");
    return true;
}

bool store_load_server(ServerInfo *s)
{
    json_error_t err;
    json_t *j = json_load_file(SERVER_PATH, 0, &err);
    if (!j) return false;
    memset(s, 0, sizeof *s);
    const char *host = json_string_value(json_object_get(j, "host"));
    const char *key = json_string_value(json_object_get(j, "key"));
    const char *name = json_string_value(json_object_get(j, "name"));
    json_int_t port = json_integer_value(json_object_get(j, "port"));
    bool ok = host && key && port > 0 && port < 65536 && b64u_decode(key, s->key, sizeof s->key) == 32;
    if (ok) {
        snprintf(s->host, sizeof s->host, "%s", host);
        snprintf(s->name, sizeof s->name, "%s", name ? name : "");
        s->port = (uint16_t)port;
        // Optional away-from-home address: {"host": "...ts.net", "port": 443}
        json_t *away = json_object_get(j, "away");
        const char *ah = json_string_value(json_object_get(away, "host"));
        json_int_t ap = json_integer_value(json_object_get(away, "port"));
        if (ah && ah[0] && strlen(ah) < sizeof s->away_host && ap > 0 && ap < 65536) {
            snprintf(s->away_host, sizeof s->away_host, "%s", ah);
            s->away_port = (uint16_t)ap;
        }
    }
    json_decref(j);
    return ok;
}

bool store_save_server(const ServerInfo *s)
{
    char key[48], fp[9];
    b64u_encode(s->key, 32, key);
    key_fingerprint(s->key, fp);
    json_t *j = json_pack("{s:s, s:i, s:s, s:s, s:s}", "host", s->host, "port", (int)s->port, "key", key, "name",
                          s->name, "fp", fp);
    if (s->away_host[0] && s->away_port)
        json_object_set_new(j, "away", json_pack("{s:s, s:i}", "host", s->away_host, "port", (int)s->away_port));
    char *text = json_dumps(j, JSON_INDENT(1));
    json_decref(j);
    bool ok = text && write_atomic(SERVER_PATH, text, strlen(text));
    free(text);
    return ok;
}

void store_forget_server(void) { remove(SERVER_PATH); }

#define SETTINGS_PATH STORE_DIR "/settings.json"

void store_load_settings(Settings *s)
{
    s->sounds = true;
    s->speak_replies = true;
    s->agent[0] = 0;
    json_t *j = json_load_file(SETTINGS_PATH, 0, NULL);
    if (!j) return;
    json_t *v;
    if (json_is_boolean(v = json_object_get(j, "sounds"))) s->sounds = json_is_true(v);
    if (json_is_boolean(v = json_object_get(j, "speak_replies"))) s->speak_replies = json_is_true(v);
    if (json_is_string(v = json_object_get(j, "agent"))) snprintf(s->agent, sizeof s->agent, "%s", json_string_value(v));
    json_decref(j);
}

bool store_save_settings(const Settings *s)
{
    json_t *j = json_pack("{s:b, s:b, s:s}", "sounds", s->sounds, "speak_replies", s->speak_replies,
                          "agent", s->agent);
    char *text = json_dumps(j, JSON_INDENT(1));
    json_decref(j);
    bool ok = text && write_atomic(SETTINGS_PATH, text, strlen(text));
    free(text);
    return ok;
}
