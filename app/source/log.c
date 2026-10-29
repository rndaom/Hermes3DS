#include "log.h"

#include <3ds.h>
#include <stdarg.h>
#include <stdio.h>
#include <sys/stat.h>
#include <time.h>

#include "store.h"

#define LOG_MAX (64 * 1024)  // start fresh when the file grows past this

static FILE *fp;
static LightLock lock;

void log_open(void)
{
    LightLock_Init(&lock);
    struct stat st;
    const char *mode = (stat(STORE_DIR "/log.txt", &st) == 0 && st.st_size > LOG_MAX) ? "w" : "a";
    fp = fopen(STORE_DIR "/log.txt", mode);
    log_printf("--- start");
}

void log_close(void)
{
    if (fp) fclose(fp);
    fp = NULL;
}

void log_printf(const char *fmt, ...)
{
    if (!fp) return;
    LightLock_Lock(&lock);
    time_t now = time(NULL);
    struct tm *tm = localtime(&now);
    fprintf(fp, "%02d:%02d:%02d ", tm->tm_hour, tm->tm_min, tm->tm_sec);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(fp, fmt, ap);
    va_end(ap);
    fputc('\n', fp);
    fflush(fp);
    LightLock_Unlock(&lock);
}
