#!/usr/bin/env python3
"""One-click auto login watcher for Shuttle.

Launches Microsoft Edge or Chrome with remote debugging (CDP) enabled and monitors
active tabs and Windows clipboard for Google OAuth authorization codes (e.g. 4/0A...).
Once captured, it automatically submits the code to the signin daemon and closes the tab.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brains as B  # noqa: E402
import signin  # noqa: E402

CODE_RE = re.compile(r"4/[0-9A-Za-z_-]{20,}")
URL_CODE_RE = re.compile(r"[?&]code=([^&\s#]+)")


def find_browser() -> Optional[str]:
    """Find installed Edge or Chrome executable."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


def get_clipboard_text() -> str:
    """Read plain text from system clipboard using ctypes on Windows."""
    if not B.IS_WIN:
        return ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            return ""
        try:
            CF_UNICODETEXT = 13
            h_clip = user32.GetClipboardData(CF_UNICODETEXT)
            if not h_clip:
                return ""
            ptr = kernel32.GlobalLock(h_clip)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                kernel32.GlobalUnlock(h_clip)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


def extract_code(text: str) -> Optional[str]:
    """Extract Google OAuth authorization code from URL, title, or raw text."""
    if not text:
        return None
    # 1. Check query parameter `code=...`
    qm = URL_CODE_RE.search(text)
    if qm:
        raw_code = urllib.parse.unquote(qm.group(1).strip())
        if raw_code.startswith("4/") and len(raw_code) >= 20:
            return raw_code
    # 2. Check direct pattern `4/...`
    m = CODE_RE.search(text)
    if m:
        return m.group(0).strip()
    return None


def find_free_port(start_port: int = 9229) -> int:
    """Find an available localhost port for remote debugging."""
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start_port


def close_tab(port: int, tab_id: str) -> bool:
    """Close tab via CDP HTTP API."""
    try:
        url = f"http://127.0.0.1:{port}/json/close/{tab_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "ShuttleAutoLogin"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def watch_and_capture(cli: str, account: str, cdp_port: int, browser_proc: Optional[subprocess.Popen] = None,
                      timeout: int = 600) -> Optional[str]:
    """Monitor CDP tabs and clipboard for OAuth authorization code."""
    end = time.time() + timeout
    last_clip = ""
    while time.time() < end:
        # Check if signin already completed or failed
        state = signin.read_state(cli, account)
        if state.get("state") in ("signed_in", "failed"):
            return None

        # 1. Check Windows Clipboard
        clip = get_clipboard_text().strip()
        if clip and clip != last_clip:
            last_clip = clip
            code = extract_code(clip)
            if code:
                try:
                    signin.submit_code(cli, account, code)
                    return code
                except Exception:
                    pass

        # 2. Check CDP tabs
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json", headers={"User-Agent": "ShuttleAutoLogin"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                tabs = json.loads(resp.read().decode("utf-8"))
            for tab in tabs:
                tab_url = tab.get("url", "")
                tab_title = tab.get("title", "")
                code = extract_code(tab_url) or extract_code(tab_title)
                if code:
                    try:
                        signin.submit_code(cli, account, code)
                        close_tab(cdp_port, tab["id"])
                        return code
                    except Exception:
                        pass
        except Exception:
            pass

        time.sleep(0.8)

    return None


def start_auto_login(cli: str, account: str, auth_url: str) -> bool:
    """Launch browser with CDP and begin background watcher."""
    browser_exe = find_browser()
    if not browser_exe:
        return False

    port = find_free_port(9229)
    profile_dir = B.HOME / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        browser_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        auth_url,
    ]

    kw = {"creationflags": 0x08000000} if B.IS_WIN else {}
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except Exception:
        return False

    t = threading.Thread(target=watch_and_capture, args=(cli, account, port, proc), daemon=True)
    t.start()
    return True


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit("usage: python auto_login.py <cli> <account> <auth_url>")
    ok = start_auto_login(sys.argv[1], sys.argv[2], sys.argv[3])
    sys.exit(0 if ok else 1)
