#include "led.h"

#include <3ds.h>
#include <string.h>

#include "log.h"

static bool ready, lit;

void led_init(void)
{
    Result rc = mcuHwcInit();
    ready = R_SUCCEEDED(rc);
    if (!ready) log_printf("led: mcu::HWC unavailable (%08lx)", (unsigned long)rc);
}

static void set(bool on)
{
    InfoLedPattern p;
    memset(&p, 0, sizeof p);
    if (on) {
        // Yellow, on for half a second then off, repeating: 32 steps of 1/16 s.
        p.delay = 1;
        p.smoothing = 0x40;
        p.loopDelay = 0;
        for (int i = 0; i < 32; i++)
            if ((i / 8) % 2 == 0) {
                p.redPattern[i] = 0xFF;
                p.greenPattern[i] = 0x90;
            }
    }
    if (R_FAILED(MCUHWC_SetInfoLedPattern(&p))) log_printf("led: set pattern failed");
}

void led_waiting(bool on)
{
    if (!ready || on == lit) return;
    lit = on;
    set(on);
}

void led_exit(void)
{
    if (!ready) return;
    if (lit) set(false);
    mcuHwcExit();
    ready = lit = false;
}
