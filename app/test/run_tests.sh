#!/bin/sh
# Host-side tests for the portable C code (Noise IK + codecs, blob cache pruning, camera YUV, DSP firmware search).
# Needs python and clang (or set CC=gcc).
set -e
cd "$(dirname "$0")"
CC="${CC:-clang}"
TMP="${TMPDIR:-${TEMP:-/tmp}}"
OUT="$TMP/h3ds_test_noise.exe"
python gen_vectors.py
"$CC" -std=c99 -O2 -Wall -Wextra -Werror -I../lib/monocypher -D_CRT_SECURE_NO_WARNINGS \
    test_noise.c ../source/noise.c ../source/codec.c ../lib/monocypher/monocypher.c -o "$OUT"
"$OUT"

# The MSVC runtime has no <dirent.h>: compat/ supplies one. It has the math functions built in, where
# everywhere else they need libm.
COMPAT=""
LIBM="-lm"
case "$("$CC" -dumpmachine 2>/dev/null)" in *msvc*) COMPAT="-Icompat" LIBM="" ;; esac
OUT="$TMP/h3ds_test_cache.exe"
DIR="$TMP/h3ds_test_cache.$$"
"$CC" -std=c99 -O2 -Wall -Wextra -Werror $COMPAT -D_CRT_SECURE_NO_WARNINGS -D_CRT_NONSTDC_NO_WARNINGS \
    test_cache.c ../source/cache.c -o "$OUT"
rm -rf "$DIR" && mkdir -p "$DIR"
"$OUT" "$DIR"
rm -rf "$DIR"

# The queue of spoken replies (plain C, no player).
OUT="$TMP/h3ds_test_autoplay.exe"
"$CC" -std=c99 -O2 -Wall -Wextra -Werror -D_CRT_SECURE_NO_WARNINGS test_autoplay.c ../source/autoplay.c -o "$OUT"
"$OUT"

# The camera's YUV (preview, JPEG planes, thumbnail, warm-up); with libjpeg-turbo, a real JPEG round trip too.
OUT="$TMP/h3ds_test_yuv.exe"
TJ=""
if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists libturbojpeg; then
    TJ="-DHAVE_TURBOJPEG $(pkg-config --cflags --libs libturbojpeg)"
fi
# shellcheck disable=SC2086
"$CC" -std=c99 -O2 -Wall -Wextra -Werror -D_CRT_SECURE_NO_WARNINGS test_yuv.c ../source/yuv.c $TJ -o "$OUT"
"$OUT"

# The UI sounds (lyre plucks).
OUT="$TMP/h3ds_test_tones.exe"
"$CC" -std=c99 -O2 -Wall -Wextra -Werror -D_CRT_SECURE_NO_WARNINGS test_tones.c ../source/tones.c $LIBM -o "$OUT"
"$OUT"

# The DSP firmware inside the HOME Menu's .code (unpacking and the checks; nothing of Nintendo's needed).
OUT="$TMP/h3ds_test_dspfirm.exe"
"$CC" -std=c99 -O2 -Wall -Wextra -Werror -D_CRT_SECURE_NO_WARNINGS test_dspfirm.c ../source/dspfirm.c -o "$OUT"
"$OUT"

# IMA-ADPCM decoding (audio attachments) against ffmpeg's decoder, when ffmpeg is installed.
if command -v ffmpeg >/dev/null 2>&1; then
    OUT="$TMP/h3ds_test_adpcm.exe"
    DIR="$TMP/h3ds_test_adpcm.$$"
    rm -rf "$DIR" && mkdir -p "$DIR"
    # A tone under loud noise: every code and step size gets used.
    ffmpeg -nostdin -hide_banner -loglevel error -f lavfi -i "sine=f=440:d=2:r=44100" \
        -f lavfi -i "anoisesrc=d=2:a=0.6:r=44100" -filter_complex amix=inputs=2 \
        -ac 1 -ar 22050 -c:a adpcm_ima_wav -block_size 1024 "$DIR/ima.wav"
    ffmpeg -nostdin -hide_banner -loglevel error -i "$DIR/ima.wav" -f s16le -c:a pcm_s16le "$DIR/ref.s16"
    "$CC" -std=c99 -O2 -Wall -Wextra -Werror -D_CRT_SECURE_NO_WARNINGS -D_USE_MATH_DEFINES test_adpcm.c ../source/adpcm.c $LIBM -o "$OUT"
    "$OUT" "$DIR/ima.wav" "$DIR/ref.s16"
    # The encoder (voice messages): ffmpeg must decode what it writes exactly as adpcm.c does.
    "$OUT" --encode "$DIR/enc.wav" "$DIR/ours.s16"
    ffmpeg -nostdin -hide_banner -loglevel error -i "$DIR/enc.wav" -f s16le -c:a pcm_s16le "$DIR/ff.s16"
    # (ffmpeg also outputs the last block's padding sample, past the fact count: compare up to there.)
    cmp -n "$(wc -c < "$DIR/ours.s16")" "$DIR/ours.s16" "$DIR/ff.s16" &&
        echo "adpcm encoder: ffmpeg decodes it identically"
    rm -rf "$DIR"
else
    echo "adpcm: skipped (no ffmpeg)"
fi
