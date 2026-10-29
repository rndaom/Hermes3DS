"""Audio for the 3DS (docs/protocol.md §4.7 `audio`): any format ffmpeg reads → mono IMA-ADPCM blocks.

The Old 3DS plays these cheaply: ndsp takes PCM16, and an IMA-ADPCM block decodes to it with a few integer
operations per sample on the ARM11. At 22050 Hz mono that is ~11 KB/s (a quarter of PCM16), so a voice
reply downloads in moments and a long clip stays small in the SD cache.

ffmpeg does the decoding and encoding (it must be on PATH; Hermes' own voice features use it too). Without
it, audio files stay plain `file` attachments.
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from array import array
from functools import lru_cache
from typing import Any

from . import preview

RATE = 22050
BLOCK = 1024              # bytes per IMA-ADPCM block (4-byte header + 2040 bytes = 2041 samples)
MAX_SECONDS = 15 * 60     # longer audio is cut here (~10 MB of blocks)
TIMEOUT = 180             # seconds for one ffmpeg run
MIME = "audio/x-ima-adpcm"
WAVE_IMA_ADPCM = 0x11

AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".opus", ".wav", ".flac", ".m4a", ".aac", ".wma", ".aif", ".aiff",
              ".amr", ".mka", ".caf"}


def is_audio(name: str, mime: str) -> bool:
    return mime.startswith("audio/") or os.path.splitext(name)[1].lower() in AUDIO_EXTS


@lru_cache(maxsize=1)
def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def samples_per_block(block: int) -> int:
    return (block - 4) * 2 + 1


def parse_wav(data: bytes) -> dict[str, Any] | None:
    """The fmt/fact/data chunks of an IMA-ADPCM mono WAV, or None when it is anything else."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    fmt = fact = body = None
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        chunk = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = chunk
        elif cid == b"fact" and len(chunk) >= 4:
            fact = struct.unpack_from("<I", chunk)[0]
        elif cid == b"data":
            body = chunk
        pos += 8 + size + (size & 1)
    if fmt is None or body is None or len(fmt) < 16:
        return None
    tag, channels, rate, _, block, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag != WAVE_IMA_ADPCM or channels != 1 or bits != 4 or not 8 <= block <= 8192:
        return None
    spb = samples_per_block(block)
    whole, tail = divmod(len(body), block)
    samples = whole * spb + (samples_per_block(tail) if tail >= 4 else 0)
    if tail and tail < 4:  # a torn last header: drop it
        body = body[:whole * block]
    if fact is not None and 0 < fact <= samples:
        samples = fact
    return {"rate": rate, "block": block, "samples": samples, "data": body}


def transcode(src: preview.Source, name: str = "") -> dict[str, Any] | None:
    """Decode audio (a path or bytes) with ffmpeg into IMA-ADPCM blocks.

    Returns `{data, rate, block, ms, cut}` or None (no ffmpeg, no audio stream, unreadable, timed out).
    Blocking: run it in a thread.
    """
    exe = ffmpeg_path()
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="h3ds-audio-") as tmp:
        if isinstance(src, (bytes, bytearray)):
            # A file, not a pipe: MP4/M4A need a seekable input (the index can sit at the end).
            inp = os.path.join(tmp, "in" + os.path.splitext(name)[1].lower()[:8])
            with open(inp, "wb") as f:
                f.write(src)
        else:
            inp = str(src)
        out = os.path.join(tmp, "out.wav")
        cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", inp, "-map", "0:a:0", "-vn",
               "-sn", "-dn", "-map_metadata", "-1", "-ac", "1", "-ar", str(RATE), "-t", str(MAX_SECONDS),
               "-c:a", "adpcm_ima_wav", "-block_size", str(BLOCK), "-f", "wav", out]
        try:
            run = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 timeout=TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return None
        if run.returncode != 0 or not os.path.exists(out):
            return None
        with open(out, "rb") as f:
            wav = parse_wav(f.read())
    if wav is None or wav["samples"] == 0:
        return None
    ms = wav["samples"] * 1000 // wav["rate"]
    return {"data": wav["data"], "rate": wav["rate"], "block": wav["block"], "ms": ms,
            # ffmpeg stops at MAX_SECONDS; reaching it (within a block) means the source went on.
            "cut": wav["samples"] >= MAX_SECONDS * wav["rate"] - samples_per_block(wav["block"])}


# --- the other way: a 3DS voice message (protocol.md §4.4 `voice`) -----------------------------------

_STEPS = (7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88,
          97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
          724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660,
          4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
          18500, 20350, 22385, 24623, 27086, 29794, 32767)
_INDEX = (-1, -1, -1, -1, 2, 4, 6, 8)


def _diff_table() -> tuple[tuple[int, ...], ...]:
    """Per step index, the signed difference each 4-bit code adds (the decoder's inner arithmetic, precomputed)."""
    rows = []
    for step in _STEPS:
        row = []
        for nib in range(16):
            d = step >> 3
            if nib & 1:
                d += step >> 2
            if nib & 2:
                d += step >> 1
            if nib & 4:
                d += step
            row.append(-d if nib & 8 else d)
        rows.append(tuple(row))
    return tuple(rows)


_DIFF = _diff_table()
_NEXT = tuple(tuple(max(0, min(88, i + _INDEX[n & 7])) for n in range(16)) for i in range(89))


def decode_adpcm(data: bytes, block: int, samples: int | None = None) -> bytes:
    """IMA-ADPCM blocks (the §4.7 layout: int16 first sample, u8 step index, u8 reserved, codes low nibble first)
    → PCM16 LE mono. `samples` cuts the padding of the last block. A torn last header is dropped."""
    if not 8 <= block <= 8192:
        raise ValueError("bad block size")
    out = array("h")
    diff, nxt = _DIFF, _NEXT
    for b in range(0, len(data), block):
        blk = data[b:b + block]
        if len(blk) < 4:
            break
        pred, idx = struct.unpack_from("<hB", blk)
        if idx > 88:
            raise ValueError("bad step index")
        out.append(pred)
        for byte in blk[4:]:
            for nib in (byte & 15, byte >> 4):
                pred += diff[idx][nib]
                pred = -32768 if pred < -32768 else 32767 if pred > 32767 else pred
                idx = nxt[idx][nib]
                out.append(pred)
    if samples is not None and 0 <= samples < len(out):
        del out[samples:]
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def pcm16_wav(pcm: bytes, rate: int) -> bytes:
    """A mono 16-bit PCM WAV file around `pcm`."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def voice_to_wav(data: bytes, rate: int, block: int, ms: int | None = None) -> bytes:
    """A 3DS voice message (IMA-ADPCM blocks) → WAV for Hermes' speech-to-text. Blocking: run it in a thread."""
    if not 4000 <= rate <= 48000:
        raise ValueError("bad sample rate")
    samples = ms * rate // 1000 if ms and ms > 0 else None
    return pcm16_wav(decode_adpcm(data, block, samples), rate)
