#!/usr/bin/env python3
"""Background sign-in for agy: run its TUI in a pseudo console, pick Google sign-in, open the link in the default
browser, and keep agy alive until the sign-in lands. A console window cannot be clicked from a phone or a remote
desktop and its long link wraps, so the link is opened (and shown on the dashboard) instead.

    python signin.py agy <account>

State for the dashboard: $DELEGATE_HOME/signin-<cli>-<account>.json ({state, url, started, error}). Nothing typed
by the operator passes through here; with agy's file auth the browser completes the sign-in on its own.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brains as B  # noqa: E402
from agy_login import ANSI, URL_RE  # noqa: E402

TIMEOUT = 15 * 60


def state_path(cli: str, account: str) -> Path:
    return B.HOME / ("signin-%s-%s.json" % (cli, account or "default"))


def read_state(cli: str, account: str) -> dict:
    try:
        return json.loads(state_path(cli, account).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(cli: str, account: str, **kw) -> None:
    s = {**read_state(cli, account), **kw, "at": time.time()}
    B.HOME.mkdir(parents=True, exist_ok=True)
    state_path(cli, account).write_text(json.dumps(s), encoding="utf-8")


def agy(account: str) -> int:
    from winpty import PtyProcess
    exe = shutil.which("agy-real") or shutil.which("agy")
    if not exe:
        _write("agy", account, state="failed", error="agy is not installed")
        return 1
    env = {**os.environ, **B.account_env("agy", account)}
    home = Path(env.get("HOME") or B.USER_HOME)
    room = home / "signin-room"
    room.mkdir(parents=True, exist_ok=True)
    _write("agy", account, state="starting", url="", error="", started=time.time(), pid=os.getpid())
    proc = PtyProcess.spawn(exe, dimensions=(50, 2000), cwd=str(room), env=env)   # wide: the URL never wraps
    chunks, lock = [], threading.Lock()

    def reader():
        while True:
            try:
                data = proc.read(65536)
            except Exception:  # noqa: BLE001 - EOF
                break
            with lock:
                chunks.append(data)
    threading.Thread(target=reader, daemon=True).start()

    def text() -> str:
        with lock:
            return ANSI.sub("", "".join(chunks)).replace("\r", "\n")

    trusted = picked = False
    url = ""
    end = time.time() + TIMEOUT
    try:
        while time.time() < end and proc.isalive():
            t = text()
            if not trusted and "Do you trust" in t:
                proc.write("\r")
                trusted = True
            if not picked and ("Select login method" in t or "Google OAuth" in t):
                time.sleep(0.5)
                proc.write("\r")
                picked = True
            m = URL_RE.search(t)
            if m and not url:
                url = m.group(0).rstrip(").,;]'\"")
                _write("agy", account, state="awaiting_browser", url=url)
                webbrowser.open(url)
            if url and ("for shortcuts" in t[t.find(url):] or B.auth_status("agy", account, os.environ).get("logged_in")):
                _write("agy", account, state="signed_in", url="")
                return 0
            if not url and "for shortcuts" in t:   # already signed in
                _write("agy", account, state="signed_in", url="")
                return 0
            time.sleep(2 if url else 0.5)
        ok = B.auth_status("agy", account, os.environ).get("logged_in")
        _write("agy", account, state="signed_in" if ok else "failed", url="",
               error="" if ok else "no sign-in within %d min; screen tail: %s" % (TIMEOUT // 60, text()[-400:]))
        return 0 if ok else 1
    finally:
        try:
            proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    cli, account = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "default")
    if cli != "agy":
        sys.exit("only agy uses the background sign-in")
    sys.exit(agy(account))
