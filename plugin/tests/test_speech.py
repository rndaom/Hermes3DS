"""Streaming spoken replies: the Piper provider (speech.py) and the adapter's streaming-TTS hooks."""

import abc
import asyncio
import json
import math
import struct
import sys
import types

import pytest

from h3proto import audio
from h3proto.blobs import BlobStore
from h3proto.media import build_speech_attachment
from speech import SPEAK_CAP, VOICE_QUEUE_CAP, Pacer
from test_fetch import adapter_mod  # noqa: F401 - fixture
from test_upload import ad  # noqa: F401 - fixture

needs_ffmpeg = pytest.mark.skipif(audio.ffmpeg_path() is None, reason="ffmpeg not on PATH")
RATE = 22050


def sine(seconds: float, rate: int = RATE) -> bytes:
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(9000 * math.sin(i / 7)) for i in range(n)))


# --- the attachment -----------------------------------------------------------------------------

@needs_ffmpeg
def test_speech_attachment_is_adpcm_voice_audio_without_an_original(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    att = build_speech_attachment(store, sine(1.5), RATE)
    assert att["kind"] == "audio" and att["voice"] is True and "file" not in att
    assert att["rate"] == RATE and att["block"] == audio.BLOCK and abs(att["ms"] - 1500) <= 2
    ref = att["audio"]
    assert ref["mime"] == audio.MIME and ref["size"] >= 1500 * RATE // 1000 // 4  # ~4 bits a sample
    assert store.meta(ref["id"])["role"] == "audio"


def test_speech_attachment_is_none_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "ffmpeg_path", lambda: None)
    assert build_speech_attachment(BlobStore(tmp_path / "blobs"), sine(0.2), RATE) is None


# --- the adapter hooks --------------------------------------------------------------------------

FMT = types.SimpleNamespace(sample_rate=RATE, channels=1, sample_width=2)


def test_streaming_speech_is_supported_for_a_paired_chat_with_ffmpeg(ad, adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(adapter_mod, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    assert ad.supports_streaming_tts("dev:fp1", FMT)
    assert not ad.supports_streaming_tts("dev:other", FMT)
    assert not ad.supports_streaming_tts("dev:fp1", types.SimpleNamespace(sample_rate=RATE, channels=2, sample_width=2))
    assert not ad.supports_streaming_tts("dev:fp1", types.SimpleNamespace(sample_rate=RATE, channels=1, sample_width=1))
    assert not ad.supports_streaming_tts("dev:fp1", types.SimpleNamespace(sample_rate=100, channels=1, sample_width=2))
    monkeypatch.setattr(adapter_mod, "ffmpeg_path", lambda: None)
    assert not ad.supports_streaming_tts("dev:fp1", FMT)


@needs_ffmpeg
def test_chunks_become_one_voice_message_at_the_end(ad):  # noqa: F811
    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        for _ in range(3):
            await ad.write_streaming_tts(h, sine(0.5))
        assert ad._server.posted == []  # nothing goes out before the reply is done
        await ad.finish_streaming_tts(h)
        return h

    h = asyncio.run(go())
    (msg,) = ad._server.posted
    (att,) = msg["attach"]
    assert att["kind"] == "audio" and att["voice"] is True and abs(att["ms"] - 1500) <= 2
    assert msg["final"] is True and msg["text"] == ""
    assert not h.pcm


@needs_ffmpeg
@pytest.mark.parametrize("how", ["aborted", "interrupted", "silent"])
def test_nothing_is_sent_for_an_aborted_interrupted_or_empty_reply(ad, how):  # noqa: F811
    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        if how != "silent":
            await ad.write_streaming_tts(h, sine(0.3))
        if how == "aborted":
            await ad.abort_streaming_tts(h, "boom")
            await ad.write_streaming_tts(h, sine(0.3))  # late chunks are dropped
        await ad.finish_streaming_tts(h, interrupted=how == "interrupted")
        return h

    h = asyncio.run(go())
    assert ad._server.posted == [] and not h.pcm


@needs_ffmpeg
def test_a_runaway_reply_is_cut_at_the_audio_limit(ad, adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(adapter_mod, "MAX_SPEECH_SECONDS", 1)

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        await ad.write_streaming_tts(h, sine(0.8))
        await ad.write_streaming_tts(h, sine(0.8))
        await ad.write_streaming_tts(h, sine(0.8))
        assert len(h.pcm) == RATE * 2
        await ad.finish_streaming_tts(h)

    asyncio.run(go())
    assert abs(ad._server.posted[0]["attach"][0]["ms"] - 1000) <= 2


def test_an_unencodable_reply_raises_so_hermes_logs_it(ad, adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(adapter_mod, "build_speech_attachment", lambda *a, **k: None)

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        await ad.write_streaming_tts(h, sine(0.2))
        await ad.finish_streaming_tts(h)

    with pytest.raises(RuntimeError, match="encoded"):
        asyncio.run(go())
    assert ad._server.posted == []


# --- speech in groups (an app that plays voice messages one after another) -------------------------

def test_pacer_posts_the_first_audio_at_once_then_keeps_the_app_fed():
    clock = [100.0]
    p = Pacer(lambda: clock[0])
    assert not p.due(0)
    assert p.due(0.3)              # nothing posted yet: as early as possible
    p.posted(3.0)                  # the app now has 3 s queued
    assert not p.due(1.0)          # a short group waits while the app has plenty (3 s > LEAD)
    assert p.wait() == pytest.approx(2.0)
    clock[0] += 0.6                # 2.4 s left (< LEAD)
    assert not p.due(1.0)          # still too short a group...
    assert p.due(2.0)              # ...but a decent one goes
    clock[0] += 1.5                # 0.9 s left (< DRY)
    assert p.due(0.2)              # about to run dry: anything goes
    assert p.wait() == 0.0
    p.posted(2.0)
    assert p.queued() == pytest.approx(2.9)


def test_pacer_never_lets_a_group_outgrow_the_maximum_and_forgets_idle_time():
    clock = [0.0]
    p = Pacer(lambda: clock[0])
    p.posted(1.0)
    clock[0] += 60                 # the app played it long ago and went quiet
    assert p.queued() == 0.0 and p.due(0.1)
    p.posted(1.0)                  # a new post counts from now, not from the first one
    assert p.queued() == pytest.approx(1.0)
    p.posted(30.0)
    assert p.due(Pacer.MAX_GROUP)  # a full group goes whatever the queue holds


def test_the_server_cap_name_is_voiceq():
    assert VOICE_QUEUE_CAP == "voiceq"


def _clock(handle, t):
    handle.pacer.clock = lambda: t[0]


@needs_ffmpeg
def test_grouped_speech_posts_parts_as_the_reply_is_written(ad):  # noqa: F811
    ad._server.caps = {VOICE_QUEUE_CAP}
    t = [0.0]

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        assert h.grouped
        _clock(h, t)
        await ad.write_streaming_tts(h, sine(3.0))
        assert len(ad._server.posted) == 1       # the first sentence goes out at once
        await ad.write_streaming_tts(h, sine(2.0))
        assert len(ad._server.posted) == 1       # the app still has 3 s queued: this one waits
        t[0] += 1.0                              # 2 s left (< LEAD) and 2 s pending (>= MIN_GROUP)
        await ad.write_streaming_tts(h, sine(1.0))
        assert len(ad._server.posted) == 2       # 3 s of speech in one message
        await ad.write_streaming_tts(h, sine(0.5))
        assert len(ad._server.posted) == 2
        await ad.finish_streaming_tts(h)          # the tail goes with the end
        return h

    h = asyncio.run(go())
    assert [round(m["attach"][0]["ms"] / 1000, 1) for m in ad._server.posted] == [3.0, 3.0, 0.5]
    assert all(m["attach"][0]["voice"] is True and m["final"] is True and m["text"] == "" for m in ad._server.posted)
    assert len({m["mid"] for m in ad._server.posted}) == 3 and not h.pcm and h.posts == 3


@needs_ffmpeg
def test_speech_stays_one_message_for_an_app_without_the_queue(ad):  # noqa: F811
    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        assert not h.grouped
        for _ in range(3):
            await ad.write_streaming_tts(h, sine(1.0))
        assert ad._server.posted == []
        await ad.finish_streaming_tts(h)

    asyncio.run(go())
    assert len(ad._server.posted) == 1 and abs(ad._server.posted[0]["attach"][0]["ms"] - 3000) <= 2


@needs_ffmpeg
def test_the_config_flag_turns_grouping_off_even_for_an_app_with_the_queue(ad):  # noqa: F811
    ad._server.caps = {VOICE_QUEUE_CAP}
    ad.speech_groups = False

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        assert not h.grouped
        await ad.write_streaming_tts(h, sine(1.0))
        await ad.write_streaming_tts(h, sine(1.0))
        await ad.finish_streaming_tts(h)

    asyncio.run(go())
    assert len(ad._server.posted) == 1


@pytest.mark.parametrize("value, expected", [(None, True), ("true", True), ("false", False), ("0", False),
                                              ("off", False), (False, False), ("yes", True)])
def test_speech_groups_config(adapter_mod, value, expected):
    extra = {} if value is None else {"speech_groups": value}
    a = adapter_mod.ThreeDSAdapter(types.SimpleNamespace(extra=extra))
    assert a.speech_groups is expected


@needs_ffmpeg
def test_pending_speech_goes_out_before_the_app_runs_dry(ad):  # noqa: F811
    ad._server.caps = {VOICE_QUEUE_CAP}

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        h.pacer.DRY, h.pacer.LEAD = 0.05, 0.1  # real time, scaled down
        await ad.write_streaming_tts(h, sine(0.4))
        assert len(ad._server.posted) == 1
        await ad.write_streaming_tts(h, sine(0.2))  # the model went quiet: no more writes
        assert len(ad._server.posted) == 1 and h.timer is not None
        await asyncio.sleep(0.6)
        assert len(ad._server.posted) == 2 and h.timer is None
        await ad.finish_streaming_tts(h)
        assert len(ad._server.posted) == 2  # nothing left over, nothing twice

    asyncio.run(go())


@needs_ffmpeg
def test_finishing_cancels_the_timer_and_keeps_the_order(ad):  # noqa: F811
    ad._server.caps = {VOICE_QUEUE_CAP}

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        await ad.write_streaming_tts(h, sine(5.0))
        await ad.write_streaming_tts(h, sine(0.5))
        assert h.timer is not None
        await ad.finish_streaming_tts(h)
        assert h.timer is None
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert [round(m["attach"][0]["ms"] / 1000, 1) for m in ad._server.posted] == [5.0, 0.5]


@needs_ffmpeg
@pytest.mark.parametrize("how", ["aborted", "interrupted"])
def test_a_cut_reply_drops_what_is_pending_and_posts_nothing_more(ad, how):  # noqa: F811
    ad._server.caps = {VOICE_QUEUE_CAP}

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        await ad.write_streaming_tts(h, sine(5.0))
        await ad.write_streaming_tts(h, sine(1.0))
        assert h.timer is not None and h.pcm
        if how == "aborted":
            await ad.abort_streaming_tts(h, "boom")
            await ad.write_streaming_tts(h, sine(1.0))
        await ad.finish_streaming_tts(h, interrupted=how == "interrupted")
        await asyncio.sleep(0.05)
        return h

    h = asyncio.run(go())
    assert len(ad._server.posted) == 1  # the first part had already gone out
    assert not h.pcm and h.timer is None


# --- speak events (an app that plays parts without putting them in the chat) -----------------------

def test_the_server_cap_name_is_speak():
    assert SPEAK_CAP == "speak"


def _speak_events(ad):  # noqa: F811
    return [e for e in ad._server.events if e.get("t") == "speak"]


@needs_ffmpeg
def test_speak_events_carry_the_parts_and_the_chat_gets_no_voice_message(ad):  # noqa: F811
    ad._server.caps = {SPEAK_CAP, VOICE_QUEUE_CAP}  # the app says both: speak events win
    t = [0.0]

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        assert h.speech_id and not h.grouped
        _clock(h, t)
        await ad.write_streaming_tts(h, sine(3.0))
        assert len(_speak_events(ad)) == 1 and ad._server.posted == []  # the first part plays at once, off the chat
        await ad.write_streaming_tts(h, sine(2.0))
        t[0] += 1.0
        await ad.write_streaming_tts(h, sine(1.0))
        assert len(_speak_events(ad)) == 2 and ad._server.posted == []
        await ad.write_streaming_tts(h, sine(0.5))
        await ad.finish_streaming_tts(h)
        return h

    h = asyncio.run(go())
    parts = _speak_events(ad)
    assert [round(e["attach"]["ms"] / 1000, 1) for e in parts] == [3.0, 3.0, 0.5]
    assert all(e["speech"] == h.speech_id and e["attach"]["voice"] is True for e in parts)
    assert ad._server.posted == [] and not h.pcm  # heard, not shown: no reply.wav under the reply


@needs_ffmpeg
@pytest.mark.parametrize("how", ["aborted", "interrupted"])
def test_a_cut_spoken_reply_stops_where_it_was_cut(ad, how):  # noqa: F811
    ad._server.caps = {SPEAK_CAP}

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        await ad.write_streaming_tts(h, sine(2.0))
        await ad.write_streaming_tts(h, sine(1.0))
        if how == "aborted":
            await ad.abort_streaming_tts(h, "boom")
        await ad.finish_streaming_tts(h, interrupted=how == "interrupted")
        await asyncio.sleep(0.05)
        return h

    h = asyncio.run(go())
    assert len(_speak_events(ad)) == 1 and ad._server.posted == []  # what had played stays played
    assert not h.pcm


@needs_ffmpeg
def test_the_config_flag_keeps_speech_whole_for_an_app_with_speak_events(ad):  # noqa: F811
    ad._server.caps = {SPEAK_CAP}
    ad.speech_groups = False

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        assert not h.speech_id and not h.grouped
        await ad.write_streaming_tts(h, sine(1.0))
        await ad.finish_streaming_tts(h)

    asyncio.run(go())
    assert _speak_events(ad) == [] and len(ad._server.posted) == 1
    assert "speech" not in ad._server.posted[0]["attach"][0]


def _wav(path, seconds=1.0):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(sine(seconds))
    return path


@needs_ffmpeg
def test_a_one_piece_spoken_reply_is_a_speak_event_for_an_app_that_plays_them(ad, tmp_path):  # noqa: F811
    ad._server.caps = {SPEAK_CAP}
    res = asyncio.run(ad.play_tts(chat_id="dev:fp1", audio_path=str(_wav(tmp_path / "reply.wav")), metadata={}))
    assert res.success and ad._server.posted == []
    (ev,) = _speak_events(ad)
    assert ev["speech"] and ev["attach"]["voice"] is True and ev["attach"]["kind"] == "audio"


@needs_ffmpeg
def test_a_one_piece_spoken_reply_is_a_voice_message_for_an_older_app(ad, tmp_path):  # noqa: F811
    res = asyncio.run(ad.play_tts(chat_id="dev:fp1", audio_path=str(_wav(tmp_path / "reply.wav")), metadata={}))
    assert res.success and _speak_events(ad) == []
    (msg,) = ad._server.posted
    assert msg["attach"][0]["voice"] is True


def test_a_missing_one_piece_reply_fails_without_sending(ad, tmp_path):  # noqa: F811
    ad._server.caps = {SPEAK_CAP}
    res = asyncio.run(ad.play_tts(chat_id="dev:fp1", audio_path=str(tmp_path / "gone.wav")))
    assert not res.success and _speak_events(ad) == [] and ad._server.posted == []


@needs_ffmpeg
def test_the_audio_limit_covers_all_parts_of_a_reply(ad, adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(adapter_mod, "MAX_SPEECH_SECONDS", 2)
    ad._server.caps = {VOICE_QUEUE_CAP}

    async def go():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        for _ in range(4):
            await ad.write_streaming_tts(h, sine(1.0))
        await ad.finish_streaming_tts(h)

    asyncio.run(go())
    assert round(sum(m["attach"][0]["ms"] for m in ad._server.posted) / 1000, 1) == 2.0


def test_a_part_that_cannot_be_encoded_raises_for_the_first_and_is_logged_for_a_timed_one(ad, adapter_mod, monkeypatch, caplog):  # noqa: F811
    ad._server.caps = {VOICE_QUEUE_CAP}
    monkeypatch.setattr(adapter_mod, "build_speech_attachment", lambda *a, **k: None)

    async def first():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        await ad.write_streaming_tts(h, sine(0.5))

    with pytest.raises(RuntimeError, match="encoded"):
        asyncio.run(first())

    async def timed():
        h = await ad.begin_streaming_tts("dev:fp1", FMT)
        h.pacer.posted(5.0)  # pretend a part went out
        h.pcm += sine(0.5)
        await adapter_mod.ThreeDSAdapter._post_speech(ad, h, quiet=True)
        assert not h.pcm and h.posts == 0

    with caplog.at_level("WARNING", logger="hermes3ds"):
        asyncio.run(timed())
    assert "not sent" in caplog.text and ad._server.posted == []


# --- the provider -------------------------------------------------------------------------------

class _TTSProvider(abc.ABC):
    """Stand-in for agent.tts_provider.TTSProvider: `name` and `synthesize` are abstract."""
    streams_pcm = False
    stream_sample_rate = None

    @property
    @abc.abstractmethod
    def name(self): ...

    @abc.abstractmethod
    def synthesize(self, text, output_path, **kw): ...


class _Voice:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, syn_config=None):
        self.calls.append((text, syn_config))
        return [types.SimpleNamespace(audio_int16_bytes=text.encode()), types.SimpleNamespace(audio_int16_bytes=b"!!")]

    def synthesize_wav(self, text, wav, syn_config=None):
        self.calls.append((text, syn_config))
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 160)


@pytest.fixture
def hermes(adapter_mod, tmp_path, monkeypatch):  # noqa: F811
    """Fake agent.* / hermes_cli.config / hermes_constants / piper modules plus a Piper voice on disk; returns the
    `plugin.speech` module and state."""
    state = types.SimpleNamespace(cfg={"piper": {"voice": "en_US-test"}, "streaming": {"provider": "piper-stream"}},
                                  voice=_Voice(), loaded=[])
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "en_US-test.onnx").write_bytes(b"onnx")
    (voices / "en_US-test.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 16000}}))

    def load(model, config_path=None, use_cuda=False):
        state.loaded.append((model, config_path, use_cuda))
        return state.voice

    mods = {"agent": types.ModuleType("agent"), "agent.tts_provider": types.ModuleType("agent.tts_provider"),
            "hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.config": types.ModuleType("hermes_cli.config"),
            "hermes_constants": types.ModuleType("hermes_constants"), "piper": types.ModuleType("piper")}
    mods["agent.tts_provider"].TTSProvider = _TTSProvider
    mods["hermes_cli.config"].load_config = lambda: {"tts": state.cfg}
    mods["hermes_constants"].get_hermes_dir = lambda new, old: voices
    mods["piper"].PiperVoice = types.SimpleNamespace(load=load)
    mods["piper"].SynthesisConfig = lambda **kw: types.SimpleNamespace(**kw)
    mods["piper"].__spec__ = importlib_spec("piper")
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(adapter_mod.speech, "_voices", {})
    state.voices = voices
    return adapter_mod.speech, state


def importlib_spec(name):
    import importlib.machinery
    return importlib.machinery.ModuleSpec(name, None)


def test_register_adds_the_provider_and_tolerates_old_hermes(hermes):
    speech, _ = hermes
    seen = []
    assert speech.register_provider(types.SimpleNamespace(register_tts_provider=seen.append))
    (prov,) = seen
    assert prov.name == "piper-stream" and prov.streams_pcm is True
    assert not speech.register_provider(types.SimpleNamespace())  # a Hermes without TTS provider plugins

    def refuse(_):
        raise ValueError("name clash")

    assert not speech.register_provider(types.SimpleNamespace(register_tts_provider=refuse))


def test_provider_reports_the_voice_rate_and_availability_without_loading_it(hermes):
    speech, state = hermes
    prov = speech.make_provider()
    assert prov.stream_sample_rate == 16000 and prov.is_available()
    assert state.loaded == []
    state.cfg = {"piper": {"voice": "not-downloaded"}}
    assert prov.stream_sample_rate is None and not prov.is_available()  # never waits for a download
    state.cfg = {"piper": {"voice": str(state.voices / "en_US-test.onnx")}}  # a path works too
    assert prov.stream_sample_rate == 16000


def test_a_voice_json_without_a_rate_falls_back_to_22050(hermes):
    speech, state = hermes
    (state.voices / "en_US-test.onnx.json").write_text("{}")
    assert speech.make_provider().stream_sample_rate == 22050


def test_stream_yields_the_pcm_of_each_sentence_chunk(hermes):
    speech, state = hermes
    assert list(speech.make_provider().stream("Hello there.")) == [b"Hello there.", b"!!"]
    assert state.voice.calls == [("Hello there.", None)]


def test_stream_passes_piper_knobs_through(hermes):
    speech, state = hermes
    state.cfg["piper"].update(length_scale=0.9, volume=1.5, speaker_id=True)  # a bool is not a speaker id
    list(speech.make_provider().stream("Hi."))
    (_, syn), = state.voice.calls
    assert (syn.length_scale, syn.volume, syn.speaker_id, syn.noise_scale) == (0.9, 1.5, 0, 0.667)


def test_synthesize_writes_the_whole_reply_as_wav(hermes, tmp_path):
    speech, state = hermes
    out = speech.make_provider().synthesize("Hello.", str(tmp_path / "x.mp3"), format="mp3")
    assert out == str(tmp_path / "x.wav")  # Piper writes WAV; the provider contract allows the other extension
    import wave
    with wave.open(out) as w:
        assert (w.getframerate(), w.getnframes()) == (16000, 160)
    assert state.voice.calls == [("Hello.", None)]


def test_a_voice_is_loaded_once_with_its_config_and_device(hermes):
    speech, state = hermes
    state.cfg["piper"]["use_cuda"] = True
    prov = speech.make_provider()
    list(prov.stream("One."))
    list(prov.stream("Two."))
    model = state.voices / "en_US-test.onnx"
    assert state.loaded == [(str(model), str(model) + ".json", True)]


def test_a_voice_that_is_not_downloaded_raises_instead_of_fetching_it(hermes):
    speech, state = hermes
    state.cfg = {"piper": {"voice": "not-downloaded"}}
    with pytest.raises(FileNotFoundError):
        list(speech.make_provider().stream("Hi."))
    assert state.loaded == []


def test_pinned_only_when_tts_streaming_provider_names_it(hermes):
    speech, state = hermes
    assert speech.is_pinned()
    state.cfg = {"streaming": {"provider": "Piper-Stream "}}
    assert speech.is_pinned()
    state.cfg = {"streaming": {"provider": "xai"}}
    assert not speech.is_pinned()
    state.cfg = {}
    assert not speech.is_pinned()


def test_warm_voice_loads_once_and_never_raises(hermes):
    speech, state = hermes
    assert speech.warm_voice() and len(state.loaded) == 1
    assert speech.warm_voice() and len(state.loaded) == 1  # cached
    speech._voices.clear()
    sys.modules["piper"].PiperVoice = types.SimpleNamespace(load=lambda *a, **k: 1 / 0)
    assert speech.warm_voice() is False
