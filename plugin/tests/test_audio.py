"""Audio attachments (protocol.md §4.7 `audio`): ffmpeg → mono IMA-ADPCM blocks."""

import math
import os
import struct
import subprocess
import wave

import pytest

from h3proto import audio
from h3proto.blobs import BlobStore
from h3proto.media import build_attachment, build_attachment_bytes

needs_ffmpeg = pytest.mark.skipif(audio.ffmpeg_path() is None, reason="ffmpeg not on PATH")

STEPS = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88,
         97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
         724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660,
         4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
         18500, 20350, 22385, 24623, 27086, 29794, 32767]
INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]


def decode(data: bytes, block: int) -> list[int]:
    """Reference IMA-ADPCM decoder (Microsoft WAV layout, mono), as the 3DS does it in app/source/adpcm.c."""
    out = []
    for b in range(0, len(data), block):
        blk = data[b:b + block]
        pred, idx = struct.unpack_from("<hB", blk)
        out.append(pred)
        for byte in blk[4:]:
            for nib in (byte & 15, byte >> 4):
                step = STEPS[idx]
                diff = step >> 3
                if nib & 1: diff += step >> 2
                if nib & 2: diff += step >> 1
                if nib & 4: diff += step
                pred = max(-32768, min(32767, pred - diff if nib & 8 else pred + diff))
                idx = max(0, min(88, idx + INDEX[nib & 7]))
                out.append(pred)
    return out


def _sine_wav(path, seconds=1.5, rate=44100, channels=2, freq=440.0):
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", v) * channels
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def _ima_wav(fmt_block=1024, samples=None, body=b"", tag=0x11, channels=1, bits=4):
    fmt = struct.pack("<HHIIHHHH", tag, channels, 22050, 11100, fmt_block, bits, 2, (fmt_block - 4) * 2 + 1)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if samples is not None:
        chunks += b"fact" + struct.pack("<II", 4, samples)
    chunks += b"LIST" + struct.pack("<I", 3) + b"abc\0"  # odd size: padded to even
    chunks += b"data" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path / "blobs")


def _read(store, ref):
    return b"".join(d for _, d in store.open_chunks(ref["id"]))


# --- WAV parsing ----------------------------------------------------------------------

def test_parse_wav_uses_fact_and_skips_padding_chunks():
    body = bytes(1024 * 2 + 100)
    got = audio.parse_wav(_ima_wav(samples=3000, body=body))
    assert got["rate"] == 22050 and got["block"] == 1024 and got["samples"] == 3000 and got["data"] == body


def test_parse_wav_counts_samples_without_fact_and_drops_torn_header():
    full = audio.parse_wav(_ima_wav(body=bytes(1024 + 100)))
    assert full["samples"] == 2041 + (96 * 2 + 1)
    torn = audio.parse_wav(_ima_wav(body=bytes(1024 + 3)))
    assert torn["samples"] == 2041 and len(torn["data"]) == 1024
    assert audio.parse_wav(_ima_wav(samples=10 ** 6, body=bytes(1024)))["samples"] == 2041  # fact too big


@pytest.mark.parametrize("kw", [{"tag": 1}, {"channels": 2}, {"bits": 16}, {"fmt_block": 4}])
def test_parse_wav_rejects_other_formats(kw):
    assert audio.parse_wav(_ima_wav(body=bytes(2048), **kw)) is None
    assert audio.parse_wav(b"RIFF....WAVE") is None and audio.parse_wav(b"OggS") is None


def test_audio_names():
    assert audio.is_audio("voice.ogg", "application/octet-stream") and audio.is_audio("x", "audio/mpeg")
    assert not audio.is_audio("clip.mp4", "video/mp4") and not audio.is_audio("a.txt", "text/plain")


# --- transcoding ------------------------------------------------------------------------

@needs_ffmpeg
def test_stereo_44k_wav_becomes_mono_22k_adpcm(store, tmp_path):
    src = _sine_wav(tmp_path / "tone.wav")
    att = build_attachment(store, src)
    assert att["kind"] == "audio" and att["name"] == "tone.wav" and att["mime"] in ("audio/wav", "audio/x-wav", "audio/vnd.wave")
    assert list(att) == ["kind", "name", "size", "mime", "audio", "rate", "block", "ms", "file"]
    assert att["rate"] == 22050 and att["block"] == 1024 and abs(att["ms"] - 1500) <= 1
    assert att["audio"]["mime"] == "audio/x-ima-adpcm" and att["audio"]["size"] % 1024 == 0
    assert _read(store, att["file"]) == src.read_bytes()
    pcm = decode(_read(store, att["audio"]), 1024)
    assert len(pcm) >= 22050 * 3 // 2
    # The tone survives: compare against the ideal 440 Hz sine (any phase) over the middle half second.
    seg = pcm[8000:8000 + 11025]
    c = sum(v * math.cos(2 * math.pi * 440 * i / 22050) for i, v in enumerate(seg))
    s = sum(v * math.sin(2 * math.pi * 440 * i / 22050) for i, v in enumerate(seg))
    amplitude = 2 * math.hypot(c, s) / len(seg)
    assert 10000 < amplitude < 14000


@needs_ffmpeg
def test_compressed_audio_from_bytes(store, tmp_path):
    src = _sine_wav(tmp_path / "t.wav", seconds=0.5, channels=1)
    ogg = tmp_path / "voice.ogg"
    subprocess.run([audio.ffmpeg_path(), "-nostdin", "-loglevel", "error", "-i", str(src), "-c:a", "libvorbis",
                    str(ogg)], check=True)
    att = build_attachment_bytes(store, ogg.read_bytes(), "voice.ogg")
    assert att["kind"] == "audio" and abs(att["ms"] - 500) < 60 and "cut" not in att


@needs_ffmpeg
def test_long_audio_is_cut(store, tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "MAX_SECONDS", 1)
    att = build_attachment(store, _sine_wav(tmp_path / "long.wav", seconds=2.5, channels=1))
    assert att["kind"] == "audio" and att["cut"] is True and abs(att["ms"] - 1000) <= 1
    short = build_attachment(store, _sine_wav(tmp_path / "short.wav", seconds=0.6, channels=1))
    assert "cut" not in short


@needs_ffmpeg
def test_unreadable_audio_is_a_plain_file(store, tmp_path):
    src = tmp_path / "broken.mp3"
    src.write_bytes(os.urandom(4000))
    att = build_attachment(store, src)
    assert att["kind"] == "file" and "audio" not in att and att["file"]["size"] == 4000


def test_without_ffmpeg_audio_is_a_plain_file(store, tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "ffmpeg_path", lambda: None)
    att = build_attachment(store, _sine_wav(tmp_path / "tone.wav", seconds=0.1))
    assert att["kind"] == "file" and "audio" not in att


@needs_ffmpeg
def test_ffmpeg_timeout_is_a_plain_file(store, tmp_path, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 1)
    monkeypatch.setattr(audio.subprocess, "run", slow)
    att = build_attachment(store, _sine_wav(tmp_path / "tone.wav", seconds=0.1))
    assert att["kind"] == "file"


@needs_ffmpeg
def test_voice_without_an_audio_name_is_tried_when_asked(store, tmp_path):
    src = _sine_wav(tmp_path / "tone.wav", seconds=0.3, channels=1)
    blob = src.rename(tmp_path / "tts_output.bin")
    assert build_attachment(store, blob)["kind"] == "file"
    att = build_attachment(store, blob, try_image=False, try_audio=True)
    assert att["kind"] == "audio" and att["name"] == "tts_output.bin" and abs(att["ms"] - 300) <= 1
    assert build_attachment(store, _sine_wav(tmp_path / "x.wav", seconds=0.1), try_audio=False)["kind"] == "file"
