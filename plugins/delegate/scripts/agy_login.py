"""Swap agy's Google account through its own OAuth sign-in: logout, open the link, paste the response code.

On Windows agy keeps one login in the OS credential store, so a second account cannot run side by side. Instead
this drives agy's interactive TUI in a pseudo console (pywinpty / ConPTY): it sends `/logout`, picks Google OAuth,
and returns the sign-in URL; the operator opens it, signs in with the other Google account, and the response code
they paste is typed into agy. agy's quota rests are keyed by the signed-in email (brains.quota_group), so the new
account's quota is usable at once and the old one's rests are remembered for when it comes back.

Never stores or logs the pasted code. Prior art: PrivateEngine engine/providers/account_login.py (Linux PTY).
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import brains as B

ROOM = B.HOME / "agy-room"   # an empty folder agy is asked to trust once
ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>]")
URL_RE = re.compile(r"https://accounts\.google\.com/[^\s\"'<>]+")
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\s*\(")
FAIL_RE = re.compile(r"token exchange failed|invalid_grant|authorization code[^\n]{0,40}(invalid|expired)|"
                     r"login failed|authentication failed|access denied", re.I)


class AgyLogin:
    """One sign-in at a time. start() -> url; submit(code) -> email. Keep the object alive between the two."""

    def __init__(self) -> None:
        self.proc = None
        self.chunks: list = []
        self.lock = threading.Lock()
        self.state = "idle"          # idle | awaiting_code | signed_in | failed | cancelled
        self.url = ""
        self.error = ""
        self.email_before = B.agy_email()
        self.email = ""
        self.started = 0.0

    # ---------------------------------------------------------------- pseudo console
    def _spawn(self) -> None:
        try:
            from winpty import PtyProcess
        except ImportError as e:
            raise RuntimeError("pywinpty is required for the agy sign-in flow: pip install pywinpty") from e
        exe = shutil.which("agy-real") or shutil.which("agy")
        if not exe:
            raise RuntimeError("agy is not installed")
        ROOM.mkdir(parents=True, exist_ok=True)
        # very wide, so the long OAuth URL is never soft-wrapped across lines
        self.proc = PtyProcess.spawn(exe, dimensions=(50, 2000), cwd=str(ROOM))
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc) -> None:
        while True:
            try:
                data = proc.read(65536)
            except Exception:  # noqa: BLE001 - EOF or closed
                break
            with self.lock:
                self.chunks.append(data)

    def text(self) -> str:
        with self.lock:
            raw = "".join(self.chunks)
        return ANSI.sub("", raw).replace("\r", "\n")

    def _write(self, s: str) -> None:
        self.proc.write(s)

    def _alive(self) -> bool:
        try:
            return bool(self.proc and self.proc.isalive())
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        try:
            if self.proc:
                self.proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
        self.proc = None

    # ---------------------------------------------------------------- flow
    def start(self, timeout: float = 120) -> Dict:
        """Log the current account out and bring up Google OAuth; returns the URL to open."""
        self.started = time.time()
        self._spawn()
        trusted = logout_sent = oauth_picked = respawned = False
        mark = 0
        end = time.time() + timeout
        while time.time() < end:
            t = self.text()
            new = t[mark:]
            m = URL_RE.search(new)
            if m:
                self.url, self.state = m.group(0).rstrip(").,;]'\""), "awaiting_code"
                return self.public()
            if not trusted and "Do you trust" in t:
                self._write("\r")
                trusted = True
            elif not logout_sent and "for shortcuts" in t:
                self._write("/logout")
                time.sleep(0.8)
                self._write("\r")
                logout_sent, mark = True, len(t)
            elif not oauth_picked and ("Select login method" in new or "Google OAuth" in new):
                time.sleep(0.5)
                self._write("\r")                  # option 1: Google OAuth
                oauth_picked = True
            elif not self._alive():
                if logout_sent and not respawned:  # agy may exit after /logout: start it again to sign in
                    respawned, oauth_picked, mark = True, False, 0
                    with self.lock:
                        self.chunks = []
                    self._spawn()
                else:
                    break
            time.sleep(0.3)
        self.state, self.error = "failed", "no sign-in link from agy; screen tail:\n" + self.text()[-1200:]
        self.close()
        return self.public()

    def submit(self, code: str, timeout: float = 120) -> Dict:
        """Type the response code the operator copied from the browser, and wait for agy to sign in."""
        code = (code or "").strip()
        if self.state != "awaiting_code" or not self._alive():
            raise RuntimeError("no agy sign-in is waiting for a code (start one first)")
        if not code:
            raise ValueError("code is empty")
        mark = len(self.text())
        self._write(code)
        time.sleep(0.5)
        self._write("\r")                          # agy's code field submits on CR
        end = time.time() + timeout
        while time.time() < end:
            new = self.text()[mark:].replace(code, "[code]")
            if FAIL_RE.search(new):
                self.state, self.error = "failed", FAIL_RE.search(new).group(0)
                self.close()
                return self.public()
            m = EMAIL_RE.search(new)
            if m or "for shortcuts" in new:
                time.sleep(2)
                self.email = (m.group(1) if m else "") or B.agy_email()
                self.state = "signed_in"
                self.close()
                return self.public()
            time.sleep(0.5)
        self.state, self.error = "failed", "agy did not confirm the sign-in in time"
        self.close()
        return self.public()

    def public(self) -> Dict:
        out = {"state": self.state, "email_before": self.email_before}
        if self.url and self.state == "awaiting_code":
            out["url"] = self.url
            out["next"] = ("Open the link, sign in with the Google account to use, copy the response code the page "
                           "shows, and send it back (delegate_agy_code / the prompt).")
        if self.email:
            out["email"] = self.email
        if self.error:
            out["error"] = self.error
        return out


_current: Optional[AgyLogin] = None


def begin() -> Dict:
    """Start a sign-in swap (the MCP server keeps the session between this and finish())."""
    global _current
    if _current:
        _current.close()
    _current = AgyLogin()
    return _current.start()


def finish(code: str) -> Dict:
    if not _current:
        raise RuntimeError("no agy sign-in is waiting for a code (start one first)")
    return _current.submit(code)


def cancel() -> Dict:
    global _current
    if _current:
        _current.close()
        _current.state = "cancelled"
        out, _current = _current.public(), None
        return out
    return {"state": "idle"}


def interactive() -> int:
    """Command-line flow: print the link, read the code from the terminal."""
    s = AgyLogin()
    r = s.start()
    if r["state"] != "awaiting_code":
        print("failed:", r.get("error"))
        return 1
    print("Signed out of agy (%s). Open this link and sign in with the account to use:\n\n%s\n"
          % (r.get("email_before") or "unknown account", r["url"]))
    code = input("Paste the response code: ")
    r = s.submit(code)
    print("signed in as %s" % r.get("email") if r["state"] == "signed_in" else "failed: %s" % r.get("error"))
    return 0 if r["state"] == "signed_in" else 1
