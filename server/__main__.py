"""Command line of the Dictate server.

    python -m server serve [--host auto|tailscale|0.0.0.0|IP] [--port 8000] [--threads 4,2]
    python -m server token add <name>      print a new token for a person or device
    python -m server token list
    python -m server token remove <name>
    python -m server download-model        fetch Parakeet without the GUI
    python -m server bench [wav]           decode a file with every planned size

The server reads the same config.json as the desktop app of the user it runs
as (~/.config/dictate on Linux). On your PC that means your vocabulary, your
written-text settings and your Gemma model serve the phone as well; on a VPS
the service user has its own, with polishing off.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402


def tailscale_ip():
    """This machine's Tailscale IPv4, or None."""
    if not shutil.which("tailscale"):
        return None
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout
        return out.strip().splitlines()[0] if out.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def cmd_serve(args):
    import uvicorn

    import local_stt
    from server.app import create_app
    from server.pool import RecognizerPool
    from server.tokens import TokenStore

    cfg = core.load_config()
    if not local_stt.model_installed():
        print("The Parakeet model is not installed; run: python -m server download-model", file=sys.stderr)
        sys.exit(1)
    tokens = TokenStore()
    if not len(tokens):
        print("No tokens yet; nobody could log in. Create one: python -m server token add <name>", file=sys.stderr)
    host = args.host
    if host == "tailscale":
        # A service at boot: Tailscale may still be coming up. Wait a while,
        # then fail so systemd restarts us; never fall back to a wider bind.
        for _ in range(60):
            host = tailscale_ip()
            if host:
                break
            time.sleep(1)
        else:
            print("No Tailscale address after 60 s; is tailscaled running and logged in?", file=sys.stderr)
            sys.exit(1)
    elif host == "auto":
        host = tailscale_ip() or "127.0.0.1"
        if host == "127.0.0.1":
            print("No Tailscale address found, listening on 127.0.0.1 only. Use --host 0.0.0.0 "
                  "to serve the LAN.", file=sys.stderr)
    sizes = [int(x) for x in args.threads.split(",")] if args.threads else None
    pool = RecognizerPool(sizes)
    pool.preload()
    core.log.info("dictate server on %s:%d, recognizers %s, polish %s", host, args.port, pool.sizes,
                  "on" if cfg.get("polish_enabled") else "off")
    if cfg.get("polish_enabled"):
        import polish
        polish.get_server(cfg)      # bring llama-server up in the background
    print(f"Dictate server listening on http://{host}:{args.port}  (recognizers: {pool.sizes})")
    uvicorn.run(create_app(cfg, tokens, pool), host=host, port=args.port, log_level="warning")


def cmd_token(args):
    from server.tokens import TokenStore
    store = TokenStore()
    if args.action == "add":
        token = store.add(args.name)
        print(token)
    elif args.action == "list":
        for name, n in sorted(store.names().items()):
            print(f"{name}  ({n} token{'s' if n != 1 else ''})")
        if not len(store):
            print("(no tokens)")
    elif args.action == "remove":
        print(f"removed {store.remove(args.name)} token(s) of {args.name}")


def cmd_download(args):
    import local_stt
    if local_stt.model_installed():
        print("model already installed in", local_stt.MODEL_DIR)
        return
    local_stt.download_model(lambda m: print("\r" + m.ljust(78), end="", flush=True))
    print("\ninstalled in", local_stt.MODEL_DIR)


def cmd_bench(args):
    """How long one recording takes with each planned thread count, and
    whether two decodes in parallel overlap (they do when sherpa-onnx lets go
    of the GIL)."""
    import threading

    import local_stt
    from server.pool import decode_audio, plan_workers, total_ram_mb

    wav = Path(args.wav) if args.wav else Path(__file__).resolve().parent.parent / "windows-setup" / "testsatz.wav"
    audio = decode_audio(wav.read_bytes())
    secs = len(audio) / core.SAMPLE_RATE
    cores, ram = os.cpu_count() or 1, total_ram_mb()
    print(f"{cores} cores, {ram} MB RAM -> planned recognizers: {plan_workers(cores, ram)}")
    print(f"audio: {secs:.1f} s from {wav.name}")
    for threads in sorted(set([1, 2, min(4, cores), cores])):
        rec = local_stt.LocalRecognizer(threads)
        rec.wait_ready()
        t = time.monotonic(); text = rec.transcribe(audio); one = time.monotonic() - t
        times = []
        for _ in range(3):
            t = time.monotonic(); rec.transcribe(audio); times.append(time.monotonic() - t)
        print(f"  {threads} thread(s): {min(times):.2f} s per decode (first {one:.2f} s)  -> {text[:60]}")
    a, b = local_stt.LocalRecognizer(max(1, cores // 2)), local_stt.LocalRecognizer(max(1, cores // 2))
    a.wait_ready(); b.wait_ready()
    t = time.monotonic(); a.transcribe(audio); single = time.monotonic() - t
    t = time.monotonic()
    ts = [threading.Thread(target=r.transcribe, args=(audio,)) for r in (a, b)]
    [x.start() for x in ts]; [x.join() for x in ts]
    both = time.monotonic() - t
    print(f"  two decodes at once on {cores // 2} threads each: {both:.2f} s, one alone {single:.2f} s "
          f"-> {'parallel' if both < 1.6 * single else 'serialised (GIL held?)'}")


def main():
    p = argparse.ArgumentParser(prog="python -m server", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the server")
    s.add_argument("--host", default="auto", help="auto = this machine's Tailscale address, else 127.0.0.1 (default); "
                   "tailscale = wait for the Tailscale address, fail without one (for a service); or an IP")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--threads", default="", help="recognizer sizes, e.g. 4,2,2 (default: planned from cores and RAM)")
    s.set_defaults(fn=cmd_serve)
    t = sub.add_parser("token", help="manage tokens")
    t.add_argument("action", choices=["add", "list", "remove"])
    t.add_argument("name", nargs="?", default="")
    t.set_defaults(fn=cmd_token)
    d = sub.add_parser("download-model", help="fetch the Parakeet model")
    d.set_defaults(fn=cmd_download)
    b = sub.add_parser("bench", help="measure decode times on this machine")
    b.add_argument("wav", nargs="?", default="")
    b.set_defaults(fn=cmd_bench)
    args = p.parse_args()
    if args.cmd == "token" and args.action in ("add", "remove") and not args.name:
        p.error("token add/remove needs a name")
    core.setup_logging()
    args.fn(args)


if __name__ == "__main__":
    main()
