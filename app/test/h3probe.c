// h3probe: the 3DS app's C connection code (conn.c + noise.c), run on the PC against the live gateway.
// Uses the fake client's pairing (.fake3ds/device_key + server.json), so no new pairing is needed.
//
//   h3probe [--send "text"] [--seconds N] [--discover]
//
// Prints the welcome, the handshake time, a ping/pong round trip and every event for N seconds.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/codec.h"
#include "../source/conn.h"

#ifdef _WIN32
#include <bcrypt.h>
static void random_bytes(uint8_t *p, size_t n) { BCryptGenRandom(NULL, p, (ULONG)n, BCRYPT_USE_SYSTEM_PREFERRED_RNG); }
#endif

static Conn conn;

static int read_file(const char *path, char *buf, size_t cap)
{
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    size_t n = fread(buf, 1, cap - 1, f);
    fclose(f);
    buf[n] = 0;
    return (int)n;
}

// Tiny extractor for "name": "value" in the fake client's server.json (no JSON lib on the host build).
static int json_str(const char *json, const char *name, char *out, size_t cap)
{
    char pat[64];
    snprintf(pat, sizeof pat, "\"%s\"", name);
    const char *p = strstr(json, pat);
    if (!p) return -1;
    p = strchr(p + strlen(pat), ':');
    if (!p) return -1;
    while (*++p == ' ') {}
    if (*p == '"') {
        const char *e = strchr(++p, '"');
        if (!e || (size_t)(e - p) >= cap) return -1;
        memcpy(out, p, (size_t)(e - p));
        out[e - p] = 0;
    } else {
        size_t i = 0;
        while ((*p >= '0' && *p <= '9') && i + 1 < cap) out[i++] = *p++;
        out[i] = 0;
    }
    return 0;
}

static void print_body(const char *dir, const uint8_t *b, size_t n)
{
    if (b[0] == BODY_JSON) printf("%s %.*s\n", dir, (int)(n - 1), (const char *)b + 1);
    else printf("%s <blob chunk %zu bytes>\n", dir, n - 1);
}

int main(int argc, char **argv)
{
    const char *send_text = NULL;
    int seconds = 5, do_discover = 0, callback = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--send") && i + 1 < argc) send_text = argv[++i];
        else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) seconds = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--discover")) do_discover = 1;
        else if (!strcmp(argv[i], "--callback")) callback = 1;  // ask the PC to dial us (§2.3)
    }
#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif
    char sj[1024], host[64], port_s[16], key_hex[80];
    uint8_t s_priv[32], server_pub[32], e_priv[32];
    if (read_file("../../.fake3ds/server.json", sj, sizeof sj) < 0 ||
        read_file("../../.fake3ds/device_key", (char *)s_priv, 34) != 32) {
        fprintf(stderr, "run from app/test with a paired .fake3ds/ (tools/fake3ds.py pair)\n");
        return 2;
    }
    if (json_str(sj, "host", host, sizeof host) || json_str(sj, "port", port_s, sizeof port_s) ||
        json_str(sj, "key", key_hex, sizeof key_hex) || strlen(key_hex) != 64) {
        fprintf(stderr, "bad server.json\n");
        return 2;
    }
    for (int i = 0; i < 32; i++) sscanf(key_hex + 2 * i, "%2hhx", &server_pub[i]);
    char fp[9];
    key_fingerprint(server_pub, fp);
    printf("server %s:%s fingerprint %s\n", host, port_s, fp);

    if (do_discover) {
        DiscoveryReply r[4];
        int n = discover("255.255.255.255", 7334, 3, 500, r, 4);
        printf("discovery (broadcast): %d\n", n);
        for (int i = 0; i < n; i++) printf("  %s %s\n", r[i].addr, r[i].json);
        n = discover(host, 7334, 3, 500, r, 4);
        printf("discovery (unicast %s): %d\n", host, n);
        for (int i = 0; i < n; i++) printf("  %s %s\n", r[i].addr, r[i].json);
    }

    conn_init(&conn);
    uint64_t t0 = plat_ms();
    int rc = callback ? conn_callback(&conn, host, DISCOVERY_PORT, 4000)
                      : conn_open(&conn, host, (uint16_t)atoi(port_s), 5000);
    if (rc) {
        fprintf(stderr, "connect failed: %d\n", rc);
        return 1;
    }
    uint64_t t1 = plat_ms();
    static const char hello[] = "\x01{\"t\":\"hello\",\"v\":1,\"app\":\"h3probe\",\"device\":\"Fake 3DS\","
                                "\"model\":\"old3ds\",\"screen\":{\"top\":[400,240],\"bottom\":[320,240]},"
                                "\"caps\":[]}";
    random_bytes(e_priv, 32);
    const uint8_t *reply;
    size_t reply_len;
    rc = conn_handshake(&conn, s_priv, server_pub, e_priv, (const uint8_t *)hello, sizeof hello - 1, &reply,
                        &reply_len, 10000);
    uint64_t t2 = plat_ms();
    if (rc) {
        fprintf(stderr, "handshake failed: %d\n", rc);
        return 1;
    }
    print_body("<<", reply, reply_len);
    printf("connect %llu ms, handshake %llu ms\n", (unsigned long long)(t1 - t0), (unsigned long long)(t2 - t1));

    uint64_t ping_at = plat_ms();
    conn_send_json(&conn, "{\"t\":\"ping\",\"n\":1}", 5000);
    if (send_text) {
        char msg[1024];
        snprintf(msg, sizeof msg, "{\"t\":\"send\",\"id\":1,\"text\":\"%s\"}", send_text);  // caller passes JSON-safe text
        printf(">> %s\n", msg);
        conn_send_json(&conn, msg, 5000);
    }
    uint64_t end = plat_ms() + (uint64_t)seconds * 1000;
    while (plat_ms() < end) {
        const uint8_t *body;
        size_t len;
        rc = conn_recv_body(&conn, (int)(end - plat_ms()), &body, &len);
        if (rc == CONN_TIMEOUT) break;
        if (rc) {
            printf("connection ended: %d\n", rc);
            return 1;
        }
        print_body("<<", body, len);
        if (len > 1 && strstr((const char *)body + 1, "\"t\":\"pong\"") && ping_at) {
            printf("ping round trip %llu ms\n", (unsigned long long)(plat_ms() - ping_at));
            ping_at = 0;
        }
        if (len > 1 && memcmp(body + 1, "{\"t\":\"ping\"", 11) == 0) conn_send_json(&conn, "{\"t\":\"pong\"}", 5000);
    }
    conn_close(&conn);
    printf("ok\n");
    return 0;
}
