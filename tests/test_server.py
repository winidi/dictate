import io
import threading
import time
import wave

import numpy as np
import pytest

import core
from server import pool as pool_mod
from server.pool import RecognizerPool, decode_audio, plan_workers
from server.tokens import TokenStore

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")


# --- planning ----------------------------------------------------------------

@pytest.mark.parametrize("cores, ram, expected", [
    (1, 1000, [1]),
    (2, 2000, [2]),
    (2, 4000, [2, 1]),
    (4, 2000, [4]),
    (4, 4000, [4, 2]),
    (4, 8000, [4, 2, 2]),
    (8, 16000, [4, 4, 4, 4, 4]),
])
def test_plan_workers(cores, ram, expected):
    assert plan_workers(cores, ram) == expected


# --- audio -------------------------------------------------------------------

def wav_bytes(samples, rate, channels=1, width=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


def test_decode_audio_passthrough_and_resample():
    tone = (np.sin(np.linspace(0, 200 * np.pi, 16000)) * 20000).astype(np.int16)
    out = decode_audio(wav_bytes(tone, 16000))
    assert out.shape == (16000, 1) and out.dtype == np.int16
    assert abs(int(out.max()) - 20000) < 50
    stereo = np.stack([tone, tone], axis=1).reshape(-1)
    out = decode_audio(wav_bytes(stereo, 16000, channels=2))
    assert out.shape == (16000, 1)
    out = decode_audio(wav_bytes(tone, 48000))     # 1 s at 48 kHz -> 1 s at 16 kHz
    assert abs(out.shape[0] - 16000 / 3) < 2


def test_decode_audio_rejects_garbage():
    with pytest.raises(ValueError):
        decode_audio(b"this is not audio")


# --- pool with fake recognizers ----------------------------------------------

class FakeRecognizer:
    created = []

    def __init__(self, num_threads):
        self.num_threads = num_threads
        FakeRecognizer.created.append(num_threads)
        self.delay = 0.2

    def is_ready(self):
        return True

    def transcribe(self, audio):
        time.sleep(self.delay)
        return f"t{self.num_threads}"


@pytest.fixture
def fake_pool(monkeypatch):
    FakeRecognizer.created = []
    monkeypatch.setattr(pool_mod.local_stt, "LocalRecognizer", FakeRecognizer)
    return RecognizerPool([4, 2])


def test_pool_hands_out_big_instance_first_then_smaller(fake_pool):
    audio = np.zeros((1600, 1), dtype=np.int16)
    results = []
    threads = [threading.Thread(target=lambda: results.append(fake_pool.transcribe(audio))) for _ in range(3)]
    t = time.monotonic()
    for x in threads:
        x.start()
        time.sleep(0.02)
    for x in threads:
        x.join()
    took = time.monotonic() - t
    assert sorted(results) == ["t2", "t4", "t4"]       # the third waited for a free one
    assert FakeRecognizer.created == [4, 2]           # created lazily, never more than planned
    assert 0.35 < took < 0.7
    assert fake_pool.status()["busy"] == 0


def test_pool_times_out_when_everything_is_busy(fake_pool, monkeypatch):
    monkeypatch.setattr(pool_mod, "WAIT_S", 0.05)
    audio = np.zeros((1600, 1), dtype=np.int16)
    blockers = [threading.Thread(target=fake_pool.transcribe, args=(audio,)) for _ in range(2)]
    for b in blockers:
        b.start()
    time.sleep(0.03)
    with pytest.raises(TimeoutError):
        fake_pool.transcribe(audio)
    for b in blockers:
        b.join()


# --- tokens ------------------------------------------------------------------

def test_token_store(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    t1 = store.add("anna")
    t2 = store.add("anna")
    t3 = store.add("ben")
    assert t1.startswith("dk_") and t1 != t2
    assert store.lookup(t1) == "anna" and store.lookup(t3) == "ben"
    assert store.lookup("dk_nope") is None and store.lookup(None) is None
    assert store.names() == {"anna": 2, "ben": 1}
    assert store.remove("anna") == 2 and len(store) == 1
    assert oct((tmp_path / "tokens.json").stat().st_mode)[-3:] == "600"
    again = TokenStore(tmp_path / "tokens.json")
    assert again.lookup(t3) == "ben"


# --- the API -----------------------------------------------------------------

@pytest.fixture
def client(tmp_path, fake_pool):
    from server.app import create_app
    store = TokenStore(tmp_path / "tokens.json")
    token = store.add("anna")
    cfg = dict(core.DEFAULT_CONFIG, vocabulary="t4 -> vier", polish_enabled=False)
    from fastapi.testclient import TestClient
    app = create_app(cfg, store, fake_pool)
    return TestClient(app), token


def test_api_health_and_auth(client):
    c, token = client
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/api/tokens").status_code == 401
    assert c.get("/api/tokens", headers={"Authorization": "Bearer dk_wrong"}).status_code == 401
    r = c.get("/api/tokens", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json() == {"tokens": [{"name": "anna"}]}


def test_api_transcription_text_json_and_vocabulary(client):
    c, token = client
    FakeRecognizer.delay = 0.0
    audio = wav_bytes(np.zeros(16000, dtype=np.int16), 16000)
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post("/v1/audio/transcriptions", headers=auth, files={"file": ("a.wav", audio, "audio/wav")},
               data={"model": "parakeet", "response_format": "text"})
    assert r.status_code == 200 and r.text == "vier"      # the fake says "t4", the vocabulary fixes it
    r = c.post("/v1/audio/transcriptions", headers=auth, files={"file": ("a.wav", audio, "audio/wav")},
               data={"response_format": "verbose_json"})
    body = r.json()
    assert body["text"] == "vier" and body["polished"] is False and abs(body["duration"] - 1.0) < 0.01
    r = c.post("/v1/audio/transcriptions", headers=auth, files={"file": ("a.bin", b"junk", "audio/wav")})
    assert r.status_code == 400
    r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", audio, "audio/wav")})
    assert r.status_code == 401


def test_api_modes(tmp_path, fake_pool):
    from fastapi.testclient import TestClient
    from server.app import create_app
    store = TokenStore(tmp_path / "tokens.json")
    token = store.add("anna")
    cfg = dict(core.DEFAULT_CONFIG, polish_enabled=False,
               polish_modes=[{"name": "Email", "instructions": "Write an email."}, {"name": " ", "instructions": "x"}])
    c = TestClient(create_app(cfg, store, fake_pool))
    assert c.get("/api/modes").status_code == 401
    body = c.get("/api/modes", headers={"Authorization": f"Bearer {token}"}).json()
    assert body == {"polish": False, "modes": [{"name": "Email", "instructions": "Write an email."}]}


def test_resolve_mode():
    from server.app import resolve_mode
    cfg = {"polish_modes": [{"name": "Email", "instructions": "Write an email."}]}
    assert resolve_mode(cfg, "email") == "Write an email."
    assert resolve_mode(cfg, "Be brief.") == "Be brief."
    assert resolve_mode(cfg, "") == ""
