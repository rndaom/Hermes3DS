#!/bin/sh
# Build hermes3ds.3dsx. On Windows this goes through devkitPro's MSYS2 make (works from Git Bash too);
# elsewhere it runs make directly (needs DEVKITPRO/DEVKITARM set, as devkitPro's installer does).
#   app/build.sh            build
#   app/build.sh cia        the installable hermes3ds.cia
#   app/build.sh clean      clean
cd "$(dirname "$0")" || exit 1
MSYS_BASH=/c/devkitPro/msys2/usr/bin/bash.exe
if [ -x "$MSYS_BASH" ]; then
    DIR="$(pwd -W 2>/dev/null || pwd)"
    exec "$MSYS_BASH" -lc "cd '$DIR' && make $*"
fi
exec make "$@"
