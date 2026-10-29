# Third-party notices

Hermes 3DS itself is MIT licensed (see `LICENSE`). The 3DS app (`hermes3ds.3dsx` / `hermes3ds.cia`) bundles or
statically links the following. Full licence texts are in `licenses/` (and `app/lib/monocypher/LICENCE.md`).

| Component | Version | Licence | Where |
|---|---|---|---|
| [Monocypher](https://monocypher.org/) | 4.0.2 | BSD-2-Clause or CC0-1.0 (dual) | vendored in `app/lib/monocypher/`, licence in `LICENCE.md` there |
| [Twemoji](https://github.com/jdecked/twemoji) graphics | 17.0.3 | CC-BY 4.0 | `app/romfs/emoji.bin` (see below) |
| [libctru](https://github.com/devkitPro/libctru) | 2.7.0 | zlib | linked |
| [citro2d](https://github.com/devkitPro/citro2d), [citro3d](https://github.com/devkitPro/citro3d) | 1.7.0, 1.7.1 | zlib | linked |
| [Mbed TLS](https://github.com/Mbed-TLS/mbedtls) | 2.28.8 | Apache-2.0 (or GPL-2.0-or-later) | linked, `licenses/mbedtls.txt` |
| [libjpeg-turbo](https://libjpeg-turbo.org/) | 3.1.3 | IJG, BSD-3-Clause, zlib | linked, `licenses/libjpeg-turbo.md` |
| [libpng](http://www.libpng.org/pub/png/libpng.html) | 1.6.53 | libpng (PNG Reference Library License v2) | linked, `licenses/libpng.txt` |
| [zlib](https://zlib.net/) | 1.3.1 | zlib | linked, `licenses/zlib.txt` |
| [Jansson](https://github.com/akheron/jansson) | 2.13 | MIT | linked, `licenses/jansson.txt` |

Mbed TLS is used under the Apache License 2.0.

**libjpeg-turbo:** this software is based in part on the work of the Independent JPEG Group.

**zlib** is (C) Jean-loup Gailly and Mark Adler.

## Twemoji

Emoji graphics are from Twemoji 17.0.3, copyright 2019 Twitter, Inc. and other contributors, and 2024 onward
jdecked and other contributors, licensed under [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Changes: the PNGs are scaled down to 32 px and repacked for the Nintendo 3DS by `tools/build_emoji.py`. The app
shows this credit on its connect screen and ships it as `romfs:/emoji-credits.txt`.

## The gateway plugin

The `plugin/` folder runs inside [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT) and uses
libraries Hermes already provides (cryptography, Pillow, httpx, anydoc); it bundles none. Audio conversion uses
`ffmpeg` from the system PATH when it is installed.

## Trademarks

Hermes 3DS is an unofficial community project. It is not affiliated with, endorsed by or sponsored by Nous
Research or Nintendo. "Hermes" and "Hermes Agent" belong to Nous Research; "Nintendo 3DS" is a trademark of
Nintendo. The app contains no Nintendo code or assets: it draws with the console's own system font.
