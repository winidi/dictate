"""The HTTP API (FastAPI).

POST /v1/audio/transcriptions   OpenAI transcription API: multipart with
                                `file`; `response_format` text (default) or
                                json / verbose_json. Dictate extras as form
                                fields: `polish` (1/0, default: the server's
                                setting), `mode` (a pie-menu mode name or free
                                instructions), `app`, `title` (the window the
                                text goes into, for the per-window notes).
GET  /api/tokens                200 with the caller's name when the token is
                                valid (what the desktop's "Test API key" calls).
GET  /health                    no auth; model and pool state.
"""
import time
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

import core
from server.pool import RecognizerPool, decode_audio
from server.tokens import TokenStore

MAX_UPLOAD_BYTES = 64 * 1024 * 1024   # 30 minutes of 16 kHz WAV, more than any dictation


def resolve_mode(cfg, mode: str) -> str:
    """A pie-menu mode name ("Email") becomes its instructions; anything else
    is taken as instructions itself."""
    mode = (mode or "").strip()
    if not mode:
        return ""
    modes = cfg.get("polish_modes")
    if modes is None:
        try:
            import pie                 # pulls in PyQt6; absent on a bare server
            modes = pie.DEFAULT_MODES
        except Exception:
            modes = []
    for m in modes:
        if (m.get("name") or "").strip().lower() == mode.lower():
            return m.get("instructions") or ""
    return mode


def create_app(cfg, tokens: TokenStore, pool: RecognizerPool) -> FastAPI:
    app = FastAPI(title="Dictate server", docs_url=None, redoc_url=None)
    polish_mod = None
    if cfg.get("polish_enabled"):
        import polish as polish_mod   # noqa: F811

    def caller(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        name = tokens.lookup(token)
        if name is None:
            raise HTTPException(401, "invalid or missing token")
        return name

    @app.get("/health")
    def health():
        return {"status": "ok", "pool": pool.status(),
                "polish": bool(cfg.get("polish_enabled")), "tokens": len(tokens)}

    @app.get("/api/tokens")
    def api_tokens(name: str = Depends(caller)):
        return {"tokens": [{"name": name}]}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        request: Request,
        file: UploadFile = File(...),
        model: str = Form("parakeet"),
        response_format: str = Form("text"),
        prompt: str = Form(""),
        language: str = Form(""),
        polish: Optional[str] = Form(None),
        mode: str = Form(""),
        app_name: str = Form("", alias="app"),
        title: str = Form(""),
        name: str = Depends(caller),
    ):
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "audio too large")
        try:
            audio = decode_audio(data)
        except Exception as e:
            raise HTTPException(400, f"could not read the audio: {e}")
        secs = len(audio) / core.SAMPLE_RATE
        if secs < 0.1:
            raise HTTPException(400, "audio is empty")
        t0 = time.monotonic()
        try:
            raw = pool.transcribe(audio)
        except TimeoutError:
            raise HTTPException(503, "server busy, try again", headers={"Retry-After": "2"})
        decode_s = time.monotonic() - t0
        _, repl = core.parse_vocabulary(cfg.get("vocabulary"))
        raw = core.apply_replacements(raw, repl)
        text, polished = raw, False
        want_polish = cfg.get("polish_enabled") if polish is None else polish.lower() in ("1", "true", "yes")
        if want_polish and polish_mod is not None and raw:
            t1 = time.monotonic()
            try:
                text = polish_mod.polish(cfg, raw, app_name, title, mode=resolve_mode(cfg, mode))
                polished = True
            except Exception as e:
                core.log.warning("polish skipped after %.1fs: %s", time.monotonic() - t1, e)
        core.log.info("%s: %.1fs audio -> %d chars in %.2fs%s", name, secs, len(text),
                      decode_s, ", polished" if polished else "")
        if response_format == "text":
            return PlainTextResponse(text)
        body = {"text": text}
        if response_format == "verbose_json" or polished or mode:
            body.update({"raw": raw, "polished": polished, "duration": round(secs, 2),
                         "decode_s": round(decode_s, 3), "language": "de"})
        return JSONResponse(body)

    return app
