"""Streaming speech for the 3DS: Hermes speaks a reply sentence by sentence while the model is still writing it.

Hermes' gateway has a streaming-TTS path (`gateway.streaming_tts_consumer`): it cuts the model's text into
sentences, asks a *streaming* TTS provider for the raw PCM of each, and hands the PCM to the adapter. Edge TTS,
Hermes' default, has no such API, so a spoken reply only starts once the whole reply is written and then
synthesized (~1 s, sometimes 5 s over the network). This module registers a small local provider on top of Piper,
`piper-stream`, so every sentence but the last is already spoken when the model finishes.

Turn it on with `tts.streaming.provider: piper-stream`. `tts.provider` (the whole-file path: the TTS tool, the
fallback when streaming fails before any audio) is not touched, and `tts.piper.voice` picks the voice.

The adapter then posts the speech as it is synthesized, in groups of a few seconds, one voice message each, to an app
that plays voice messages one after another (hello cap `voiceq`); `Pacer` decides when each group goes out. An app
without that cap gets the whole reply as one message when the model is done.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
import time
import wave
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger("hermes3ds")

NAME = "piper-stream"
DEFAULT_VOICE = "en_US-lessac-medium"  # Hermes' default Piper voice
DEFAULT_RATE = 22050
VOICE_QUEUE_CAP = "voiceq"  # hello cap: the app plays consecutive voice messages one after another (protocol.md §4.3)
SPEAK_CAP = "speak"  # hello cap: the app plays `speak` events, so the parts stay out of the chat (protocol.md §4.3)
_KNOBS = ("length_scale", "noise_scale", "noise_w_scale", "volume", "normalize_audio", "speaker_id")


def _tts_config() -> dict[str, Any]:
    from hermes_cli.config import load_config
    cfg = load_config().get("tts")
    return cfg if isinstance(cfg, dict) else {}


def _voices_dir() -> Path:
    """Where Hermes keeps downloaded Piper voices (`<HERMES_HOME>/cache/piper-voices`)."""
    from hermes_constants import get_hermes_dir
    return Path(get_hermes_dir("cache/piper-voices", "piper_voices_cache"))


def _piper_section(cfg: dict[str, Any]) -> dict[str, Any]:
    section = cfg.get("piper")
    return section if isinstance(section, dict) else {}


def is_pinned(cfg: Optional[dict[str, Any]] = None) -> bool:
    """Whether `tts.streaming.provider` names this provider."""
    try:
        cfg = _tts_config() if cfg is None else cfg
        pinned = (cfg.get("streaming") or {}).get("provider")
        return str(pinned or "").strip().lower() == NAME
    except Exception:
        return False


def voice_files(cfg: dict[str, Any]) -> Optional[tuple[Path, Path]]:
    """The configured voice's `.onnx` and `.onnx.json` when both are on disk. Never downloads: this runs while
    a reply's speech is being set up, on the gateway's event loop."""
    section = _piper_section(cfg)
    name = str(section.get("voice") or DEFAULT_VOICE)
    model = Path(name).expanduser()
    if model.suffix.lower() != ".onnx":
        model = Path(section.get("voices_dir") or _voices_dir()).expanduser() / f"{name}.onnx"
    conf = model.with_name(model.name + ".json")
    return (model, conf) if model.is_file() and conf.is_file() else None


def voice_rate(conf: Path) -> int:
    try:
        return int(json.loads(conf.read_text(encoding="utf-8"))["audio"]["sample_rate"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_RATE


def _synthesis_config(section: dict[str, Any]) -> Any:
    """Piper's SynthesisConfig when a knob is set in `tts.piper` (the same keys as Hermes' own Piper provider)."""
    if not any(k in section for k in _KNOBS):
        return None
    from piper import SynthesisConfig
    speaker = section.get("speaker_id", 0)
    return SynthesisConfig(
        length_scale=float(section.get("length_scale", 1.0)), noise_scale=float(section.get("noise_scale", 0.667)),
        noise_w_scale=float(section.get("noise_w_scale", 0.8)), volume=float(section.get("volume", 1.0)),
        normalize_audio=bool(section.get("normalize_audio", True)), speaker_id=speaker if type(speaker) is int else 0)


_voices: dict[str, Any] = {}  # loaded Piper voices by model file and device; one serves every speaker
_voices_lock = threading.Lock()


def load_voice(cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The configured Piper voice, loaded once, and its `tts.piper` section. Blocking (~3 s the first time).
    Never downloads: a voice that isn't on disk raises (`hermes tools` or Hermes' own Piper provider fetch it)."""
    section = _piper_section(cfg)
    files = voice_files(cfg)
    if files is None:
        raise FileNotFoundError(f"Piper voice {section.get('voice') or DEFAULT_VOICE!r} is not downloaded")
    use_cuda = bool(section.get("use_cuda", False))
    key = f"{files[0]}::cuda={use_cuda}"
    with _voices_lock:
        voice = _voices.get(key)
        if voice is None:
            from piper import PiperVoice
            voice = _voices[key] = PiperVoice.load(str(files[0]), config_path=str(files[1]), use_cuda=use_cuda)
    return voice, section


class Pacer:
    """When the speech collected so far goes out as the next voice message.

    The first group goes as soon as there is any audio. After that the app is assumed to play what it was sent in
    real time, as soon as it is sent, so the audio it still has queued is known: a group goes out when that is
    about to run dry (`DRY`), or when there is a decent group (`MIN_GROUP`) and the queue is getting short
    (`LEAD`), or when the group is as long as it may be (`MAX_GROUP`). While the model writes faster than the
    voice speaks, the groups grow (3 s, 3 s, 6 s, 12 s...) so a reply is a handful of bubbles, not dozens.
    All times are seconds."""

    LEAD = 2.5       # post a decent group when the app has less than this queued
    DRY = 1.0        # post anything when the app has less than this queued (encoding and transfer take about that)
    MIN_GROUP = 2.0
    MAX_GROUP = 15.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.first = True           # nothing posted yet
        self.ends = 0.0             # when the app finishes playing everything posted so far

    def queued(self) -> float:
        """Speech the app still has to play, by the real-time assumption."""
        return max(0.0, self.ends - self.clock())

    def due(self, pending: float) -> bool:
        """Whether `pending` seconds of speech should go out now."""
        if pending <= 0:
            return False
        if self.first or pending >= self.MAX_GROUP:
            return True
        left = self.queued()
        return left <= self.DRY or (pending >= self.MIN_GROUP and left <= self.LEAD)

    def wait(self) -> float:
        """Seconds until pending speech must go out whatever its length (the queue about to run dry)."""
        return max(0.0, self.queued() - self.DRY)

    def posted(self, seconds: float) -> None:
        self.first = False
        self.ends = max(self.ends, self.clock()) + seconds


def warm_voice() -> bool:
    """Load the voice so the first spoken reply doesn't pay the ~3 s model load. Blocking; best-effort."""
    try:
        load_voice(_tts_config())
        return True
    except Exception as exc:
        logger.info("3DS: Piper voice warm-up skipped: %s", exc)
        return False


def make_provider() -> Any:
    """The `piper-stream` TTSProvider. Built on demand: its base class only exists inside Hermes."""
    from agent.tts_provider import TTSProvider

    class PiperStreamProvider(TTSProvider):
        streams_pcm = True
        name = NAME

        @property
        def display_name(self) -> str:
            return "Piper (streaming)"

        @property
        def stream_sample_rate(self) -> Optional[int]:  # type: ignore[override]
            files = voice_files(_tts_config())
            return voice_rate(files[1]) if files else None

        def is_available(self) -> bool:
            """Piper installed and the voice already downloaded (a streamed reply never waits for a download)."""
            try:
                return importlib.util.find_spec("piper") is not None and voice_files(_tts_config()) is not None
            except Exception:
                return False

        def stream(self, text: str, *, voice: Optional[str] = None, model: Optional[str] = None,
                   format: str = "pcm", **extra: Any) -> Iterator[bytes]:
            piper_voice, section = load_voice(_tts_config())
            syn = _synthesis_config(section)
            for chunk in (piper_voice.synthesize(text, syn_config=syn) if syn is not None
                          else piper_voice.synthesize(text)):
                yield chunk.audio_int16_bytes

        def synthesize(self, text: str, output_path: str, *, voice: Optional[str] = None,
                       model: Optional[str] = None, speed: Optional[float] = None, format: str = "mp3",
                       **extra: Any) -> str:
            """The whole reply as a WAV file (Piper's own format; the path gets a `.wav` extension)."""
            piper_voice, section = load_voice(_tts_config())
            syn = _synthesis_config(section)
            path = str(Path(output_path).with_suffix(".wav"))
            with wave.open(path, "wb") as wav:
                if syn is not None:
                    piper_voice.synthesize_wav(text, wav, syn_config=syn)
                else:
                    piper_voice.synthesize_wav(text, wav)
            return path

        def warm(self) -> None:
            warm_voice()

    return PiperStreamProvider()


def register_provider(ctx: Any) -> bool:
    """Register `piper-stream` with Hermes. False on a Hermes without TTS provider plugins."""
    register = getattr(ctx, "register_tts_provider", None)
    if register is None:
        return False
    try:
        register(make_provider())
    except Exception as exc:  # no TTSProvider ABC, a name clash: speech then stays on the whole-file path
        logger.info("3DS: streaming speech provider not registered: %s", exc)
        return False
    return True
