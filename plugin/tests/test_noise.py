import json
from pathlib import Path

import pytest

from h3proto.noise import Initiator, KeyPair, NoiseError, Responder

VECTORS = json.loads((Path(__file__).parent / "vectors" / "noise_ik.json").read_text())["vectors"]


@pytest.mark.parametrize("v", VECTORS, ids=[v["source"] for v in VECTORS])
def test_ik_vectors(v):
    h = bytes.fromhex
    init = Initiator(KeyPair(h(v["init_static"])), h(v["init_remote_static"]),
                     prologue=h(v["init_prologue"]), ephemeral=KeyPair(h(v["init_ephemeral"])))
    resp = Responder(KeyPair(h(v["resp_static"])), prologue=h(v["resp_prologue"]),
                     ephemeral=KeyPair(h(v["resp_ephemeral"])))
    msgs = v["messages"]

    ct = init.write_message_1(h(msgs[0]["payload"]))
    assert ct.hex() == msgs[0]["ciphertext"]
    assert resp.read_message_1(ct) == h(msgs[0]["payload"])
    assert resp.remote_static == KeyPair(h(v["init_static"])).public

    ct, t_resp = resp.write_message_2(h(msgs[1]["payload"]))
    assert ct.hex() == msgs[1]["ciphertext"]
    payload, t_init = init.read_message_2(ct)
    assert payload == h(msgs[1]["payload"])
    if "handshake_hash" in v:
        assert t_init.handshake_hash.hex() == v["handshake_hash"]
        assert t_resp.handshake_hash.hex() == v["handshake_hash"]

    for i, m in enumerate(msgs[2:]):
        sender, receiver = (t_init, t_resp) if i % 2 == 0 else (t_resp, t_init)
        ct = sender.encrypt(h(m["payload"]))
        assert ct.hex() == m["ciphertext"]
        assert receiver.decrypt(ct) == h(m["payload"])


def test_tamper_and_wrong_key():
    server, client = KeyPair(), KeyPair()
    resp = Responder(server)
    m1 = bytearray(Initiator(client, server.public).write_message_1(b"hello"))
    m1[-1] ^= 1
    with pytest.raises(NoiseError):
        resp.read_message_1(bytes(m1))

    # Client pinned the wrong server key: server cannot decrypt.
    m1 = Initiator(client, KeyPair().public).write_message_1(b"hello")
    with pytest.raises(NoiseError):
        Responder(server).read_message_1(m1)


def test_roundtrip_and_replay():
    server, client = KeyPair(), KeyPair()
    init, resp = Initiator(client, server.public), Responder(server)
    assert resp.read_message_1(init.write_message_1(b"hi")) == b"hi"
    m2, tr = resp.write_message_2(b"welcome")
    _, ti = init.read_message_2(m2)
    c = ti.encrypt(b"one")
    assert tr.decrypt(c) == b"one"
    with pytest.raises(NoiseError):
        tr.decrypt(c)  # replayed frame uses a stale nonce
