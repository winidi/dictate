import json
import threading
import time

import pytest

import core


# --- vocabulary ------------------------------------------------------------

def test_parse_vocabulary_terms_replacements_and_comments():
    terms, repl = core.parse_vocabulary(
        "Parakeet\n# a comment\nPost Gres -> PostgreSQL   # trailing comment\n\n  ->  broken\n"
    )
    assert terms == ["Parakeet", "PostgreSQL"]
    assert repl == [("Post Gres", "PostgreSQL")]


def test_apply_replacements_whole_words_any_case_longest_first():
    repl = [("Gres", "X"), ("Post Gres", "PostgreSQL"), ("parakit", "Parakeet")]
    text = "Wir nutzen post gres und PARAKIT, aber nicht Postgresql."
    assert core.apply_replacements(text, repl) == "Wir nutzen PostgreSQL und Parakeet, aber nicht Postgresql."


def test_whisper_prompt_is_unique_and_bounded():
    assert core.whisper_prompt(["a", "b", "a"]) == "a, b"
    long = ["x" * 100] * 20
    prompt = core.whisper_prompt(long)
    assert prompt == "x" * 100          # duplicates collapse
    many = [f"term{i:03d}" + "y" * 90 for i in range(20)]
    assert len(core.whisper_prompt(many)) <= core.PROMPT_MAX_CHARS


def test_vocabulary_terms_from_config():
    cfg = {"vocabulary": "Dirk Winiecki\nParakit -> Parakeet\nParakeet"}
    assert core.vocabulary_terms(cfg) == ["Dirk Winiecki", "Parakeet"]


def test_fresh_config_starts_on_the_local_model(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "config.json")
    cfg = core.load_config()
    assert cfg["provider"] == "parakeet_local"
    assert cfg["model"] == core.PROVIDERS["parakeet_local"]["default_model"]


def test_config_without_provider_stays_on_groq(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"api_key": "gsk_x", "key": "f9"}))
    cfg = core.load_config()
    assert (cfg["provider"], cfg["model"]) == ("groq", "whisper-large-v3-turbo")
    (tmp_path / "config.json").write_text(json.dumps({"provider": "openai"}))
    assert core.load_config()["provider"] == "openai"


def test_load_config_migrates_polish_vocabulary(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"polish_vocabulary": "Alt", "key": "f8"}))
    cfg = core.load_config()
    assert cfg["vocabulary"] == "Alt"
    assert "polish_vocabulary" not in cfg
    assert cfg["key"] == "f8"
    assert cfg["output_method"] == "paste"


# --- paste shortcut per window -----------------------------------------------

@pytest.mark.parametrize("wm_class, shortcut", [
    ("gnome-terminal-server Gnome-terminal", "ctrl+shift+v"),
    ("konsole konsole", "ctrl+shift+v"),
    ("Alacritty Alacritty", "ctrl+shift+v"),
    ("xterm XTerm", "shift+Insert"),
    ("urxvt URxvt", "shift+Insert"),
    ("firefox Navigator", "ctrl+v"),
    ("", "ctrl+v"),
    (None, "ctrl+v"),
])
def test_paste_shortcut(wm_class, shortcut):
    assert core.paste_shortcut(wm_class) == shortcut


def test_paste_x11_unavailable_on_wayland(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    with pytest.raises(core.PasteUnavailable):
        core.paste_x11("hallo")


def test_type_text_falls_back_to_typing_once_told(monkeypatch):
    told = []
    typed = []
    monkeypatch.setattr(core, "IS_LINUX", True)
    monkeypatch.setattr(core, "IS_WINDOWS", False)
    monkeypatch.setattr(core, "_paste_fallback_told", False)
    monkeypatch.setattr(core, "notify", lambda msg, **kw: told.append(msg))
    monkeypatch.setattr(core, "_type_keys", lambda text: typed.append(text) or True)

    def no_paste(text):
        raise core.PasteUnavailable("no clipboard tool")
    monkeypatch.setattr(core, "paste_x11", no_paste)
    assert core.type_text("eins", method="paste") is True
    assert core.type_text("zwei", method="paste") is True
    assert typed == ["eins", "zwei"]
    assert len(told) == 1                 # the notice comes once per run
    assert core.type_text("", method="paste") is True


def test_type_text_method_type_skips_paste(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "_paste", lambda text, app: calls.append("paste") or True)
    monkeypatch.setattr(core, "_type_keys", lambda text: calls.append("type") or True)
    core.type_text("x", method="type")
    core.type_text("y", method="paste")
    assert calls == ["type", "paste"]


# --- download progress -------------------------------------------------------

def test_format_progress_lines():
    assert core.format_progress("Model", 212e6, 640e6, 14.2e6, 30) == "Model: 212 / 640 MB · 14.2 MB/s · about 30 s left"
    assert core.format_progress("", 5e6, 0) == "5 MB downloaded"
    assert core.format_progress("M", 1e6, 640e6) == "M: 1 / 640 MB · calculating time left ..."
    assert core.format_progress("M", 640e6, 640e6, 1e6, 0) == "M: 640 / 640 MB · 1.0 MB/s"
    assert core.format_duration(90) == "1 min 30 s"
    assert core.format_duration(3700) == "1 h 1 min"
    assert core.format_speed(512e3) == "512 kB/s"


def test_progress_meter_speed_eta_and_throttle():
    clock = [100.0]
    lines, fractions = [], []
    m = core.ProgressMeter("f", lines.append, fractions.append, clock=lambda: clock[0])
    m.update(0, 1000)                     # first report: no speed yet
    clock[0] += 0.1
    assert m.update(10, 1000) is None     # too soon
    clock[0] += 2.0
    text = m.update(210, 1000)            # 210 bytes in 2.1 s -> 100 B/s, 790 left -> ~8 s
    assert "about 8 s left" in text
    assert fractions[-1] == pytest.approx(0.21)
    clock[0] += 0.05
    assert m.update(1000, 1000) is not None   # finished reports even when it is soon
    assert fractions[-1] == 1.0
    assert lines[0] == "f: 0 / 1000 MB · calculating time left ...".replace("0 / 1000 MB", "0 / 0 MB")


def test_run_watching_folder_reports_growth(tmp_path):
    seen = []
    meter = core.ProgressMeter("x", fraction_cb=seen.append)

    def download():
        (tmp_path / "a.part").write_bytes(b"x" * 50)
        time.sleep(0.4)                   # longer than the meter's report interval
        (tmp_path / "a.part").write_bytes(b"x" * 100)
        time.sleep(0.1)
        return "done"

    assert core.run_watching_folder(download, tmp_path, 100, meter, interval=0.02) == "done"
    assert seen[0] == 0.0 and seen[-1] == 1.0
    assert any(0 < f < 1 for f in seen)


def test_run_watching_folder_unknown_total(tmp_path):
    fractions = []
    meter = core.ProgressMeter("x", fraction_cb=fractions.append)
    core.run_watching_folder(lambda: None, tmp_path, 0, meter)
    assert fractions == [None, None]


# --- failed recordings -------------------------------------------------------

def test_keep_failed_audio_keeps_newest_three(tmp_path, monkeypatch):
    import numpy as np
    monkeypatch.setattr(core, "FAILED_DIR", tmp_path)
    counter = iter(range(100))
    monkeypatch.setattr(core.time, "strftime", lambda fmt: f"{next(counter):04d}.wav")
    for _ in range(5):
        core.keep_failed_audio(np.zeros((160, 1), dtype=np.int16))
    assert sorted(p.name for p in tmp_path.glob("*.wav")) == ["0002.wav", "0003.wav", "0004.wav"]
