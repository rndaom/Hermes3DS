"""The update check (h3proto/update.py): version comparing, the cached answer and the daily loop's one step."""

import json

from h3proto import update
from h3proto.update import INTERVAL, RETRY, UpdateChecker, newer, parse, read


def test_parse_and_newer():
    assert parse("0.2.1") == parse("v0.2.1") == parse("3ds-0.2.1") == (0, 2, 1)
    for junk in ("3ds-dev", "", "0.2", "0.2.1-rc1", "v0.2.1\nx", "1.2.3.4", "12345.0.0", None, 3):
        assert parse(junk) is None
    assert newer("0.2.2", "0.2.1") and newer("0.10.0", "3ds-0.9.9") and newer("v1.0.0", "0.99.99")
    assert not newer("0.2.1", "0.2.1") and not newer("0.2.0", "0.2.1")
    assert not newer("0.2.2", "3ds-dev") and not newer("junk", "0.2.1")


def test_read_tolerates_anything(tmp_path):
    assert read(tmp_path) == ("", 0.0)
    for bad in ("", "[]", '{"latest": 5}', '{"latest": "<b>", "checked": 1}', '{"latest": "0.2.2", "checked": "x"}'):
        (tmp_path / update.FILE).write_text(bad, "utf-8")
        assert read(tmp_path)[0] == ""
    (tmp_path / update.FILE).write_text('{"latest": "v0.2.2", "checked": 7}', "utf-8")
    assert read(tmp_path) == ("0.2.2", 7.0)


async def test_once_fetches_saves_and_then_waits(tmp_path):
    told, calls, clock = [], [], [1000.0]

    def fetch():
        calls.append(1)
        return "0.2.2"

    chk = UpdateChecker(tmp_path, told.append, fetch=fetch, now=lambda: clock[0])
    assert await chk.once() == INTERVAL
    assert told == ["0.2.2"] and len(calls) == 1
    assert json.loads((tmp_path / update.FILE).read_text("utf-8")) == {"latest": "0.2.2", "checked": 1000.0}
    assert sorted(p.name for p in tmp_path.iterdir()) == [update.FILE]

    clock[0] += 3600  # a restart an hour later: the saved answer is used, GitHub isn't asked again
    told.clear()
    assert await UpdateChecker(tmp_path, told.append, fetch=fetch, now=lambda: clock[0]).once() == INTERVAL - 3600
    assert told == ["0.2.2"] and len(calls) == 1

    clock[0] += INTERVAL  # stale: asked again
    assert await chk.once() == INTERVAL
    assert len(calls) == 2


async def test_once_keeps_what_it_knew_when_the_fetch_fails(tmp_path):
    (tmp_path / update.FILE).write_text('{"latest": "0.2.2", "checked": 1}', "utf-8")
    told = []

    def fetch():
        raise OSError("offline")

    assert await UpdateChecker(tmp_path, told.append, fetch=fetch, now=lambda: 10 * INTERVAL).once() == RETRY
    assert told == ["0.2.2"]
    assert read(tmp_path) == ("0.2.2", 1.0)

    empty = tmp_path / "empty"
    empty.mkdir()
    told.clear()
    assert await UpdateChecker(empty, told.append, fetch=fetch).once() == RETRY
    assert told == [] and not list(empty.iterdir())


def test_fetch_latest_reads_the_tag(monkeypatch):
    seen = {}

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.body

    def urlopen(req, timeout):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        return Resp(seen["body"])

    monkeypatch.setattr(update.urllib.request, "urlopen", urlopen)
    seen["body"] = b'{"tag_name": "v0.3.0", "name": "x"}'
    assert update.fetch_latest() == "0.3.0"
    assert seen["url"] == update.RELEASE_URL and seen["url"].startswith("https://api.github.com/")
    assert seen["headers"]["User-agent"] == "hermes3ds-update-check"  # names nobody
    for body in (b'{"tag_name": "nightly"}', b'{}', b'[]', b'not json'):
        seen["body"] = body
        try:
            update.fetch_latest()
        except Exception:
            continue
        raise AssertionError(body)
