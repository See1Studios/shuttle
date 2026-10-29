#!/usr/bin/env python3
"""Local management dashboard: who is signed in (with a button that opens the sign-in window), resting quota
groups, opencode's free models, the difficulty chains, and the jobs. Stdlib only; listens on 127.0.0.1.

    python dashboard.py [--port 8790] [--open]

Writes go through POST with an `X-Shuttle: 1` header, which a page from another origin cannot send without a
preflight this server never answers, so only this page can act.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brains as B  # noqa: E402
import ratings as RT  # noqa: E402
import shuttle as d  # noqa: E402
import signin as S  # noqa: E402

DEFAULT_PORT = 8790
_auth = {"at": 0.0, "rows": [], "busy": False}
_auth_lock = threading.Lock()


def _refresh_auth() -> None:
    with _auth_lock:
        if _auth["busy"]:
            return
        _auth["busy"] = True
    try:
        rows = d.auth()
        _auth.update(rows=rows, at=time.time())
    finally:
        _auth["busy"] = False


def auth_view(refresh: bool = False) -> dict:
    if refresh or not _auth["at"]:
        threading.Thread(target=_refresh_auth, daemon=True).start()
    return {"rows": _auth["rows"], "checked": _auth["at"], "busy": _auth["busy"] or not _auth["at"]}


def state() -> dict:
    chains = {}
    for tier in B.tiers():
        try:
            chains[tier] = d.chain_view(difficulty=tier)
        except (RuntimeError, ValueError) as e:
            chains[tier] = [{"brain": "error: %s" % e, "resting_until": "", "rest_kind": ""}]
    return {"jobs": d.list_jobs(False, 40), "limits": B.limits(), "chains": chains,
            "free": B.opencode_free_models(), "free_top": B.config().get("opencode_free_top"),
            "ladder": [{**r, "group": B.quota_group({"cli": "agy", "model": B.agy_model(r, "high"), "account": "default"})
                        .split("#")[-1]} for r in B.agy_ladder()],
            "auth": auth_view(), "now": time.time(),
            "ratings": RT.table(),
            "signin": {"%s@%s" % (r["cli"], r["account"]): S.read_state(r["cli"], r["account"])
                       for r in _auth["rows"] if r.get("cli") == "agy"}}


def act(path: str, body: dict) -> object:
    if path == "/api/login":
        res = d.login(body["cli"], body.get("account") or "default")
        threading.Timer(1.0, _refresh_auth).start()
        return res
    if path == "/api/auto-login":
        cli = body.get("cli", "agy")
        account = body.get("account") or "default"
        res = d.login(cli, account)
        st = S.read_state(cli, account)
        url = st.get("url")
        if url:
            try:
                import auto_login
                auto_login.start_auto_login(cli, account, url)
            except Exception:
                pass
        threading.Timer(1.0, _refresh_auth).start()
        return {"ok": True, "res": res}
    if path == "/api/account/add":
        cli = body.get("cli", "agy")
        account = (body.get("account") or "").strip()
        B.add_account(cli, account)
        if body.get("login"):
            d.login(cli, account)
            st = S.read_state(cli, account)
            url = st.get("url")
            if url:
                try:
                    import auto_login
                    auto_login.start_auto_login(cli, account, url)
                except Exception:
                    pass
        threading.Timer(1.0, _refresh_auth).start()
        return {"ok": True, "cli": cli, "account": account}
    if path == "/api/account/delete":
        cli = body.get("cli", "")
        account = (body.get("account") or "").strip()
        if account in ("", "default"):
            raise ValueError("기본 계정(default)은 삭제할 수 없습니다.")
        p = B.account_dir(cli, account)
        if p and p.exists():
            import shutil
            shutil.rmtree(p)
        threading.Timer(1.0, _refresh_auth).start()
        return {"ok": True}
    if path == "/api/signin-code":
        res = S.submit_code(body["cli"], body.get("account") or "default", body.get("code", ""))
        threading.Timer(8.0, _refresh_auth).start()
        return res
    if path == "/api/auth":
        return auth_view(refresh=True)
    if path == "/api/clear":
        return {"cleared": B.clear_limits(body.get("group", ""))}
    if path == "/api/models":
        return {"free": B.opencode_free_models(refresh=True), "agy": B.agy_models(refresh=True)}
    if path == "/api/cancel":
        return d.cancel(body["id"])
    if path == "/api/discard":
        return d.discard(body["id"])
    if path == "/api/job":
        return d.result(body["id"], max_diff=int(body.get("max_diff") or 100000))
    if path == "/api/merge":
        return d.merge(body["id"], mode=body.get("mode") or "squash", message=body.get("message") or "")
    if path == "/api/followup":
        return d.followup(body["id"], body.get("feedback") or "", cli=body.get("cli") or "", model=body.get("model") or "")
    if path == "/api/check":
        return d.check(body["id"], body.get("command") or "")
    if path == "/api/start":
        return d.start(body.get("cli") or "", body.get("task") or "", repo=body.get("repo") or ".",
                       title=body.get("title") or "", model=body.get("model") or "",
                       check=body.get("check") or "", difficulty=body.get("difficulty") or "normal")
    raise KeyError(path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, data, ctype: str = "application/json; charset=utf-8") -> None:
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if self.path == "/api/state":
            try:
                return self._send(200, state())
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.headers.get("X-Shuttle") != "1":
            return self._send(403, {"error": "missing X-Shuttle header"})
        try:
            body = json.loads(raw or b"{}")
            return self._send(200, act(self.path, body))
        except KeyError as e:
            return self._send(400, {"error": "bad request: %s" % e})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})


def serve(port: int = DEFAULT_PORT, open_browser: bool = False) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("shuttle dashboard: %s" % url, flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    srv.serve_forever()


PAGE = r"""<!doctype html>
<html lang="ko" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shuttle Dashboard</title>
<style>
:root {
  --font-sans: -apple-system, BlinkMacSystemFont, "Pretendard", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", "JetBrains Mono", Consolas, Menlo, monospace;

  /* Light Theme */
  --bg: #f8fafc;
  --card: #ffffff;
  --surface: #f1f5f9;
  --surface-hover: #e2e8f0;
  --fg: #0f172a;
  --fg-sec: #334155;
  --mute: #64748b;
  --line: #e2e8f0;
  --line-light: #f1f5f9;
  
  --acc: #2563eb;
  --acc-hover: #1d4ed8;
  --acc-bg: #eff6ff;
  --acc-fg: #ffffff;

  --ok: #059669;
  --ok-bg: #ecfdf5;
  --ok-border: #a7f3d0;

  --warn: #d97706;
  --warn-bg: #fffbeb;
  --warn-border: #fde68a;

  --bad: #dc2626;
  --bad-bg: #fef2f2;
  --bad-border: #fecaca;

  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.05);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.07), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
  --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.08), 0 4px 6px -4px rgba(0, 0, 0, 0.04);
  --shadow-drawer: -4px 0 24px rgba(0, 0, 0, 0.15);
}

@media (prefers-color-scheme: dark) {
  :root[data-theme="auto"] {
    --bg: #090d16;
    --card: #111726;
    --surface: #192236;
    --surface-hover: #222d46;
    --fg: #f1f5f9;
    --fg-sec: #cbd5e1;
    --mute: #8191a6;
    --line: #1e293b;
    --line-light: #162032;

    --acc: #3b82f6;
    --acc-hover: #60a5fa;
    --acc-bg: #1e293b;
    --acc-fg: #ffffff;

    --ok: #10b981;
    --ok-bg: #064e3b33;
    --ok-border: #065f46;

    --warn: #f59e0b;
    --warn-bg: #78350f33;
    --warn-border: #92400e;

    --bad: #ef4444;
    --bad-bg: #7f1d1d33;
    --bad-border: #991b1b;

    --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.3);
    --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.3), 0 2px 4px -2px rgba(0, 0, 0, 0.2);
    --shadow-lg: 0 10px 20px -3px rgba(0, 0, 0, 0.4), 0 4px 6px -4px rgba(0, 0, 0, 0.3);
    --shadow-drawer: -6px 0 30px rgba(0, 0, 0, 0.5);
  }
}

:root[data-theme="dark"] {
  --bg: #090d16;
  --card: #111726;
  --surface: #192236;
  --surface-hover: #222d46;
  --fg: #f1f5f9;
  --fg-sec: #cbd5e1;
  --mute: #8191a6;
  --line: #1e293b;
  --line-light: #162032;

  --acc: #3b82f6;
  --acc-hover: #60a5fa;
  --acc-bg: #1e293b;
  --acc-fg: #ffffff;

  --ok: #10b981;
  --ok-bg: #064e3b33;
  --ok-border: #065f46;

  --warn: #f59e0b;
  --warn-bg: #78350f33;
  --warn-border: #92400e;

  --bad: #ef4444;
  --bad-bg: #7f1d1d33;
  --bad-border: #991b1b;

  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.3);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.3), 0 2px 4px -2px rgba(0, 0, 0, 0.2);
  --shadow-lg: 0 10px 20px -3px rgba(0, 0, 0, 0.4), 0 4px 6px -4px rgba(0, 0, 0, 0.3);
  --shadow-drawer: -6px 0 30px rgba(0, 0, 0, 0.5);
}

:root[data-theme="light"] {
  --bg: #f8fafc;
  --card: #ffffff;
  --surface: #f1f5f9;
  --surface-hover: #e2e8f0;
  --fg: #0f172a;
  --fg-sec: #334155;
  --mute: #64748b;
  --line: #e2e8f0;
  --line-light: #f1f5f9;

  --acc: #2563eb;
  --acc-hover: #1d4ed8;
  --acc-bg: #eff6ff;
  --acc-fg: #ffffff;

  --ok: #059669;
  --ok-bg: #ecfdf5;
  --ok-border: #a7f3d0;

  --warn: #d97706;
  --warn-bg: #fffbeb;
  --warn-border: #fde68a;

  --bad: #dc2626;
  --bad-bg: #fef2f2;
  --bad-border: #fecaca;

  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.05);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.07), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
  --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.08), 0 4px 6px -4px rgba(0, 0, 0, 0.04);
  --shadow-drawer: -4px 0 24px rgba(0, 0, 0, 0.15);
}

* { box-sizing: border-box; }
::selection { background: var(--acc); color: #fff; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: 13.5px;
  line-height: 1.5;
  transition: background 0.2s ease, color 0.2s ease;
  overflow-y: scroll;
}

/* Scrollbars */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--line); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--mute); }

/* Header */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 24px;
  background: var(--card);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  z-index: 50;
  box-shadow: var(--shadow-sm);
}

.brand-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
}

.logo-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--acc), #8b5cf6);
  color: #fff;
  font-weight: 800;
  font-size: 14px;
  box-shadow: 0 2px 8px rgba(59, 130, 246, 0.35);
}

h1 {
  font-size: 16px;
  font-weight: 700;
  margin: 0;
  letter-spacing: -0.01em;
  display: flex;
  align-items: center;
  gap: 8px;
}

.live-beacon {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
  background: var(--ok-bg);
  color: var(--ok);
  border: 1px solid var(--ok-border);
}

.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
  animation: pulse-ring 1.8s infinite;
}

.live-dot.paused {
  background: var(--mute);
  animation: none;
}

@keyframes pulse-ring {
  0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
  70% { box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
  100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sync-info {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--mute);
  font-variant-numeric: tabular-nums;
  background: var(--surface);
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--line);
}

.countdown-bar-wrap {
  width: 36px;
  height: 4px;
  background: var(--line);
  border-radius: 2px;
  overflow: hidden;
}

.countdown-bar {
  height: 100%;
  width: 100%;
  background: var(--acc);
  transition: width 0.95s linear;
}

/* Button & Controls */
button {
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: var(--card);
  color: var(--fg);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: all 0.15s ease;
  white-space: nowrap;
}

button:hover:not(:disabled) {
  background: var(--surface);
  border-color: var(--mute);
  transform: translateY(-0.5px);
}

button:active:not(:disabled) {
  transform: translateY(0.5px);
}

button.primary {
  background: var(--acc);
  color: var(--acc-fg);
  border-color: var(--acc);
}

button.primary:hover:not(:disabled) {
  background: var(--acc-hover);
  border-color: var(--acc-hover);
}

button.sm {
  padding: 3px 8px;
  font-size: 11.5px;
}

button.ghost {
  background: transparent;
  border-color: transparent;
}

button.ghost:hover:not(:disabled) {
  background: var(--surface);
}

button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
  transform: none;
}

/* Layout */
main {
  max-width: 1320px;
  margin: 0 auto;
  padding: 20px 24px 60px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* KPI Ribbon */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}

.kpi-card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 14px 18px;
  display: flex;
  align-items: center;
  gap: 14px;
  box-shadow: var(--shadow-sm);
  transition: border-color 0.2s, transform 0.2s;
}

.kpi-card:hover {
  border-color: var(--mute);
}

.kpi-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex-shrink: 0;
}

.kpi-icon.jobs { background: var(--acc-bg); color: var(--acc); }
.kpi-icon.auth { background: var(--ok-bg); color: var(--ok); }
.kpi-icon.limits { background: var(--warn-bg); color: var(--warn); }
.kpi-icon.models { background: var(--surface); color: var(--fg); }

.kpi-content {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.kpi-label {
  font-size: 11.5px;
  color: var(--mute);
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.kpi-value {
  font-size: 19px;
  font-weight: 700;
  color: var(--fg);
  font-variant-numeric: tabular-nums;
  margin-top: 1px;
}

.kpi-sub {
  font-size: 11.5px;
  color: var(--mute);
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Dashboard Sections Grid */
.dashboard-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 580px), 1fr));
  gap: 18px;
}

section {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 18px 20px;
  min-width: 0;
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
}

section.wide {
  grid-column: 1 / -1;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 14px;
}

h2 {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fg);
}

.badge-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--surface);
  color: var(--mute);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

/* Tables */
.table-wrap {
  overflow-x: auto;
  border-radius: 8px;
  border: 1px solid var(--line-light);
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th {
  text-align: left;
  padding: 9px 12px;
  background: var(--surface);
  color: var(--mute);
  font-size: 11.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  border-bottom: 1px solid var(--line);
}

td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line-light);
  vertical-align: middle;
  color: var(--fg);
}

tr:last-child td {
  border-bottom: none;
}

tbody tr {
  transition: background 0.15s;
}

tbody tr:hover {
  background: var(--surface);
}

tr.interactive-row {
  cursor: pointer;
}

/* Status Pills */
.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 600;
  white-space: nowrap;
}

.pill-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.pill.ok { background: var(--ok-bg); color: var(--ok); border: 1px solid var(--ok-border); }
.pill.warn { background: var(--warn-bg); color: var(--warn); border: 1px solid var(--warn-border); }
.pill.bad { background: var(--bad-bg); color: var(--bad); border: 1px solid var(--bad-border); }
.pill.idle { background: var(--surface); color: var(--mute); border: 1px solid var(--line); }
.pill.acc { background: var(--acc-bg); color: var(--acc); border: 1px solid var(--acc); }

.pill.pulse .pill-dot {
  animation: pulse-dot-anim 1.2s infinite ease-in-out;
}
@keyframes pulse-dot-anim {
  0% { transform: scale(0.85); opacity: 0.5; }
  50% { transform: scale(1.35); opacity: 1; }
  100% { transform: scale(0.85); opacity: 0.5; }
}

.live-status-banner {
  background: var(--surface);
  border: 1px solid var(--line);
  border-left: 4px solid var(--ok);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
}
.live-status-banner.thinking {
  border-left-color: var(--acc);
}
.live-status-banner.idle {
  border-left-color: var(--warn);
}

.mono {
  font-family: var(--font-mono);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.mute {
  color: var(--mute);
}

.info-note {
  font-size: 12px;
  color: var(--mute);
  line-height: 1.5;
  margin-top: 10px;
  background: var(--surface);
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--line-light);
}

/* Chains visual */
.chain-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chain-tier {
  background: var(--surface);
  border: 1px solid var(--line-light);
  border-radius: 8px;
  padding: 10px 14px;
}

.chain-tier-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.chain-tier-name {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--acc);
}

.chain-steps {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.chain-step {
  padding: 3px 9px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--fg);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.chain-step.rest {
  opacity: 0.55;
  text-decoration: line-through;
  background: var(--bad-bg);
  border-color: var(--bad-border);
  color: var(--bad);
}

.chain-arrow {
  color: var(--mute);
  font-size: 11px;
}

/* Free models chips */
.free-models-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.free-chip {
  padding: 4px 10px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.free-chip.top-priority {
  border-color: var(--acc);
  background: var(--acc-bg);
  color: var(--acc);
  font-weight: 600;
}

.free-chip-rank {
  font-size: 10px;
  padding: 1px 4px;
  background: var(--acc);
  color: #fff;
  border-radius: 4px;
}

/* Jobs Toolbar */
.jobs-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.jobs-filters {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.filter-btn {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  background: var(--surface);
  border: 1px solid var(--line);
  color: var(--mute);
  cursor: pointer;
}

.filter-btn.active {
  background: var(--acc);
  color: #fff;
  border-color: var(--acc);
}

.search-input-wrap {
  position: relative;
  min-width: 240px;
}

.search-input {
  width: 100%;
  padding: 6px 10px 6px 30px;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: var(--card);
  color: var(--fg);
  font-size: 12.5px;
  outline: none;
  transition: border-color 0.15s;
}

.search-input:focus {
  border-color: var(--acc);
}

.search-icon {
  position: absolute;
  left: 9px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--mute);
  pointer-events: none;
}

/* Slide-over Drawer */
.drawer-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(2px);
  z-index: 90;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.25s ease;
}

.drawer-backdrop.open {
  opacity: 1;
  pointer-events: auto;
}

.drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(680px, 94vw);
  background: var(--card);
  z-index: 100;
  box-shadow: var(--shadow-drawer);
  transform: translateX(100%);
  transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  display: flex;
  flex-direction: column;
}

.drawer.open {
  transform: translateX(0);
}

.drawer-head {
  padding: 16px 20px;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.drawer-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--fg);
  margin: 0 0 4px;
}

.drawer-sub {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--mute);
}

.drawer-tabs {
  display: flex;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  padding: 0 16px;
}

.drawer-tab {
  padding: 9px 14px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--mute);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
}

.drawer-tab.active {
  color: var(--acc);
  border-bottom-color: var(--acc);
  background: var(--card);
}

.drawer-body {
  padding: 18px 20px;
  overflow-y: auto;
  flex: 1;
}

.drawer-footer {
  padding: 12px 20px;
  border-top: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  background: var(--surface);
}

/* Code & Terminal Output in Drawer */
.log-box {
  background: #090d16;
  color: #e2e8f0;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.5;
  padding: 12px 14px;
  border-radius: 8px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  border: 1px solid #1e293b;
  max-height: 380px;
}

.report-box {
  font-size: 13px;
  line-height: 1.6;
  color: var(--fg-sec);
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--surface);
  padding: 14px;
  border-radius: 8px;
  border: 1px solid var(--line);
}

/* Toast */
#toast {
  position: fixed;
  right: 24px;
  bottom: 24px;
  max-width: 420px;
  background: var(--fg);
  color: var(--bg);
  padding: 10px 16px;
  border-radius: 8px;
  font-size: 13px;
  box-shadow: var(--shadow-lg);
  display: flex;
  align-items: center;
  gap: 10px;
  z-index: 1000;
  transform: translateY(30px);
  opacity: 0;
  pointer-events: none;
  transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.2s;
}

#toast.show {
  transform: translateY(0);
  opacity: 1;
  pointer-events: auto;
}

/* Empty State */
.empty-state {
  text-align: center;
  padding: 28px 16px;
  color: var(--mute);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.empty-icon {
  font-size: 24px;
  opacity: 0.6;
}

/* Spinner */
.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.75s linear infinite;
  display: inline-block;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Diff Viewer */
.diff-box {
  background: #090d16;
  color: #e2e8f0;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.5;
  padding: 12px 14px;
  border-radius: 8px;
  overflow-x: auto;
  white-space: pre;
  border: 1px solid #1e293b;
  max-height: 480px;
}
.diff-line-add { color: #34d399; background: rgba(16, 185, 129, 0.12); display: block; margin: 0 -14px; padding: 0 14px; }
.diff-line-del { color: #f87171; background: rgba(239, 68, 68, 0.12); display: block; margin: 0 -14px; padding: 0 14px; }
.diff-line-hunk { color: #a78bfa; font-weight: 600; display: block; margin: 4px -14px 2px; padding: 0 14px; }
.diff-line-meta { color: #94a3b8; font-weight: 700; display: block; }

/* Modal & Form */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(2px);
  z-index: 100;
}
.modal {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  width: min(92vw, 560px);
  box-shadow: var(--shadow-lg);
  z-index: 101;
  display: flex;
  flex-direction: column;
}
.modal-head {
  padding: 16px 20px;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.modal-body {
  padding: 20px;
}
.modal-footer {
  padding: 14px 20px;
  border-top: 1px solid var(--line);
  background: var(--surface);
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  border-radius: 0 0 12px 12px;
}
.form-label {
  display: block;
  font-size: 12.5px;
  font-weight: 600;
  margin-bottom: 6px;
  color: var(--fg);
}
.form-input, .form-textarea, .form-select {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  color: var(--fg);
  font: inherit;
  font-size: 13px;
  outline: none;
}
.form-input:focus, .form-textarea:focus, .form-select:focus {
  border-color: var(--acc);
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
}
</style>
</head>
<body>

<header>
  <div class="brand-wrap">
    <div class="logo-badge">S</div>
    <div>
      <h1>
        Shuttle 대시보드
        <span class="live-beacon">
          <span class="live-dot" id="liveDot"></span>
          <span id="liveText">LIVE</span>
        </span>
      </h1>
    </div>
  </div>

  <div class="header-actions">
    <button class="primary sm" onclick="openNewJobModal()" title="새로운 Shuttle 작업 발사">
      <span>🚀 새 작업 (New Task)</span>
    </button>
    <div class="sync-info" title="5초마다 자동 갱신됩니다">
      <div class="countdown-bar-wrap">
        <div class="countdown-bar" id="syncBar"></div>
      </div>
      <span id="syncTimer">5s</span>
      <span class="mute">·</span>
      <span id="updTime">대기 중</span>
    </div>

    <button id="pauseBtn" class="sm" onclick="togglePause()" title="자동 갱신 일시정지 / 재개">
      <span id="pauseIcon">⏸</span>
    </button>
    <button id="refreshBtn" class="primary sm" onclick="manualRefresh()" title="지금 상태 새로고침">
      <span>새로고침</span>
    </button>
    <button class="sm ghost" onclick="cycleTheme()" id="themeBtn" title="테마 전환 (Auto/Dark/Light)">
      <span id="themeIcon">🌓</span>
    </button>
  </div>
</header>

<main>
  <!-- KPI Metrics Ribbon -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-icon jobs">⚡</div>
      <div class="kpi-content">
        <span class="kpi-label">활성 작업</span>
        <span class="kpi-value" id="kpiActiveJobs">0</span>
        <span class="kpi-sub" id="kpiTotalJobs">전체 0건 등록됨</span>
      </div>
    </div>

    <div class="kpi-card">
      <div class="kpi-icon auth">🔑</div>
      <div class="kpi-content">
        <span class="kpi-label">워커 인증</span>
        <span class="kpi-value" id="kpiAuth">0 / 0</span>
        <span class="kpi-sub" id="kpiAuthSub">확인 중…</span>
      </div>
    </div>

    <div class="kpi-card">
      <div class="kpi-icon limits">⏳</div>
      <div class="kpi-content">
        <span class="kpi-label">쉬는 그룹 (한도)</span>
        <span class="kpi-value" id="kpiLimits">0</span>
        <span class="kpi-sub" id="kpiLimitsSub">모든 워커 가용 가능</span>
      </div>
    </div>

    <div class="kpi-card">
      <div class="kpi-icon models">🚀</div>
      <div class="kpi-content">
        <span class="kpi-label">최상위 무료 모델</span>
        <span class="kpi-value" id="kpiFree" style="font-size:15px;text-overflow:ellipsis;overflow:hidden;white-space:nowrap">-</span>
        <span class="kpi-sub" id="kpiFreeSub">opencode 무료 풀 대기</span>
      </div>
    </div>
  </div>

  <!-- Main 2-Column Grid -->
  <div class="dashboard-grid">
    <!-- Worker Login -->
    <section>
      <div class="section-head">
        <h2>워커 인증 및 다중 계정 <span class="badge-count" id="authCount">0</span></h2>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="mute" id="authAt" style="font-size:12px"></span>
          <button class="sm primary" onclick="openAddAccountModal()">+ 계정 추가</button>
          <button class="sm" id="btnReauth" onclick="triggerAuthCheck()">다시 확인</button>
        </div>
      </div>
      <div class="table-wrap">
        <table id="authTable">
          <thead>
            <tr>
              <th>CLI</th>
              <th>계정</th>
              <th>상태</th>
              <th>상세 내용</th>
              <th style="text-align:right">작업</th>
            </tr>
          </thead>
          <tbody id="authBody"></tbody>
        </table>
      </div>
      <div class="info-note">
        💡 <b>로그인 안내</b>: agy는 브라우저 구글 로그인 페이지가 열리며, 다른 CLI는 로컬 터미널 창에서 인증합니다. opencode는 로그인 없이 무료 모델을 즉시 호출합니다.
      </div>
    </section>

    <!-- Resting Limits -->
    <section>
      <div class="section-head">
        <h2>쉬는 중인 워커 (한도 / 권한) <span class="badge-count" id="limitsCount">0</span></h2>
        <button class="sm" onclick="clearAllLimits()">모두 즉시 해제</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>쿼터 그룹</th>
              <th>제한 유형</th>
              <th>해제 예정</th>
              <th style="text-align:right">작업</th>
            </tr>
          </thead>
          <tbody id="limitsBody"></tbody>
        </table>
      </div>
      <div class="info-note">
        한도에 도달한 모델은 자동으로 휴식에 들어가며 체인의 다음 워커로 폴백합니다. 테스트를 위해 즉시 제한을 풀려면 해제를 누르세요.
      </div>
    </section>

    <!-- opencode Free Models -->
    <section>
      <div class="section-head">
        <h2>opencode 무료 모델 풀 <span class="badge-count" id="freeCount">0</span></h2>
        <button class="sm" id="btnRefreshModels" onclick="refreshModels()">모델 목록 갱신</button>
      </div>
      <div class="free-models-wrap" id="freeChips"></div>
      <div class="info-note">
        파란색 하이라이트 배지는 우선 폴백되는 상위 모델입니다. 한도 또는 장애 시 다음 무료 모델로 순차 전환됩니다.
      </div>
    </section>

    <!-- Difficulty Chains -->
    <section>
      <div class="section-head">
        <h2>난이도별 폴백 체인 (Easy / Normal / Hard)</h2>
      </div>
      <div class="chain-grid" id="chainTiers"></div>
    </section>

    <!-- agy Ladder Ranking -->
    <section class="wide">
      <div class="section-head">
        <h2>agy 지능 순위 (Model Ladder)</h2>
        <span class="mute" style="font-size:12px">brains.json 순위 기반</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width:60px">순위</th>
              <th>모델 패밀리</th>
              <th>지원 노력 레벨 (Efforts)</th>
              <th>쿼터 그룹</th>
            </tr>
          </thead>
          <tbody id="ladderBody"></tbody>
        </table>
      </div>
    </section>

    <!-- Ratings & Performance Section (Wide) -->
    <section class="wide">
      <div class="section-head">
        <h2>워커 모델별 성과 및 평점 (Ratings & Track Record) <span class="badge-count" id="ratingsCount">0</span></h2>
        <span class="mute" style="font-size:12px">ratings.jsonl 기반 Laplace-smoothed 성공률</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>워커 모델 (Brain)</th>
              <th>난이도</th>
              <th style="width:190px">성공률 (Score)</th>
              <th>평가 횟수</th>
              <th>결과 세부 (Counts)</th>
              <th>평균 소요 시간</th>
            </tr>
          </thead>
          <tbody id="ratingsBody"></tbody>
        </table>
      </div>
      <div class="info-note">
        과거 작업에서 acceptance check 통과(+), 디렉터 승인(+), 재작업(~), 실패(-)를 종합 집계하여, 점수가 낮은 워커는 체인 라우팅에서 자동 스킵됩니다.
      </div>
    </section>

    <!-- Jobs Section (Wide) -->
    <section class="wide">
      <div class="section-head">
        <h2>작업 관리 (Jobs) <span class="badge-count" id="jobsCount">0</span></h2>
        <button class="primary sm" onclick="openNewJobModal()">+ 새 작업 시작</button>
      </div>

      <div class="jobs-toolbar">
        <div class="jobs-filters">
          <button class="filter-btn active" data-filter="all" onclick="setJobFilter('all')">전체 <span class="badge-count" id="filterCountAll">0</span></button>
          <button class="filter-btn" data-filter="running" onclick="setJobFilter('running')">실행 중 <span class="badge-count" id="filterCountRunning">0</span></button>
          <button class="filter-btn" data-filter="done" onclick="setJobFilter('done')">완료 <span class="badge-count" id="filterCountDone">0</span></button>
          <button class="filter-btn" data-filter="failed" onclick="setJobFilter('failed')">실패/오류 <span class="badge-count" id="filterCountFailed">0</span></button>
          <button class="filter-btn" data-filter="discarded" onclick="setJobFilter('discarded')">폐기됨 <span class="badge-count" id="filterCountDiscarded">0</span></button>
        </div>

        <div style="display:flex;align-items:center;gap:10px">
          <div class="search-input-wrap">
            <span class="search-icon">🔍</span>
            <input type="text" class="search-input" id="jobSearch" placeholder="작업 제목, ID, 워커 검색…" oninput="filterJobs()">
          </div>
        </div>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>작업 정보</th>
              <th>상태</th>
              <th>워커 / 모델</th>
              <th>시작 / 소요 시간</th>
              <th>진행 및 비고</th>
              <th style="text-align:right">작업</th>
            </tr>
          </thead>
          <tbody id="jobsBody"></tbody>
        </table>
      </div>
    </section>
  </div>
</main>

<!-- Job Detail Slide-over Drawer -->
<div class="drawer-backdrop" id="drawerBackdrop" onclick="closeDrawer()"></div>
<div class="drawer" id="jobDrawer">
  <div class="drawer-head">
    <div>
      <h3 class="drawer-title" id="dTitle">-</h3>
      <div class="drawer-sub" id="dId">-</div>
    </div>
    <button class="ghost sm" onclick="closeDrawer()" style="font-size:16px;padding:4px 8px">✕</button>
  </div>

  <div id="dLiveBanner" style="display:none;padding:12px 24px 0 24px"></div>

  <div class="drawer-tabs">
    <div class="drawer-tab active" onclick="switchDrawerTab('report')">워커 보고서</div>
    <div class="drawer-tab" onclick="switchDrawerTab('diff')">코드 변경 (Diff)</div>
    <div class="drawer-tab" onclick="switchDrawerTab('logs')">실행 로그</div>
    <div class="drawer-tab" onclick="switchDrawerTab('meta')">상세 정보</div>
  </div>

  <div class="drawer-body">
    <!-- Tab 1: Report -->
    <div id="tabReport">
      <div id="dReport" class="report-box">보고서를 불러오는 중…</div>
    </div>

    <!-- Tab 2: Diff -->
    <div id="tabDiff" style="display:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span class="mute" style="font-size:12px" id="dDiffStat">Diffstat</span>
        <button class="sm" onclick="copyDrawerDiff()">Diff 복사</button>
      </div>
      <div id="dDiff" class="diff-box">변경사항을 불러오는 중…</div>
    </div>

    <!-- Tab 3: Logs -->
    <div id="tabLogs" style="display:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span class="mute" style="font-size:12px">Stdout / Stderr Tail</span>
        <button class="sm" onclick="copyDrawerLogs()">로그 복사</button>
      </div>
      <div id="dLogs" class="log-box">로그를 불러오는 중…</div>
    </div>

    <!-- Tab 4: Meta -->
    <div id="tabMeta" style="display:none">
      <table style="width:100%">
        <tbody id="dMetaBody"></tbody>
      </table>
    </div>
  </div>

  <div class="drawer-footer" id="dFooter"></div>
</div>

<!-- New Task Modal -->
<div class="modal-backdrop" id="newTaskModalBackdrop" onclick="closeNewTaskModal()" style="display:none"></div>
<div class="modal" id="newTaskModal" style="display:none">
  <div class="modal-head">
    <h3 style="margin:0;font-size:16px">새 Shuttle 작업 발사 🚀</h3>
    <button class="ghost sm" onclick="closeNewTaskModal()" style="font-size:16px;padding:2px 6px">✕</button>
  </div>
  <div class="modal-body">
    <label class="form-label">작업 내용 (Task Brief) <span style="color:var(--bad)">*</span></label>
    <textarea id="ntTask" class="form-textarea" rows="4" placeholder="워커에게 지시할 작업 내용을 상세히 적어주세요. 예: HTTP 클라이언트에 지수 백오프 재시도 로직 추가하고 단위 테스트 작성"></textarea>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
      <div>
        <label class="form-label">난이도 (Difficulty)</label>
        <select id="ntDifficulty" class="form-select">
          <option value="normal" selected>Normal (체인: opencode → agy Gemini/Claude)</option>
          <option value="easy">Easy (무료 및 저비용 Flash 우선)</option>
          <option value="hard">Hard (Argon / Opus 고성능 우선)</option>
        </select>
      </div>
      <div>
        <label class="form-label">특정 CLI 지정 (선택)</label>
        <select id="ntCli" class="form-select">
          <option value="" selected>자동 라우팅 (추천)</option>
          <option value="opencode">OpenCode (무료 모델)</option>
          <option value="agy">Antigravity (agy)</option>
          <option value="claude">Claude Code</option>
          <option value="codex">OpenAI Codex</option>
          <option value="grok">Grok Build</option>
        </select>
      </div>
    </div>

    <div style="margin-top:12px">
      <label class="form-label">수락 테스트 명령어 (--check, 선택)</label>
      <input type="text" id="ntCheck" class="form-input" placeholder="예: pytest tests/test_client.py 또는 npm test">
      <div class="mute" style="font-size:11.5px;margin-top:4px">워커가 라운드를 마칠 때마다 실행되며, 실패 시 워커에게 출력을 넘겨 자동 수정합니다.</div>
    </div>
  </div>
  <div class="modal-footer">
    <button onclick="closeNewTaskModal()">취소</button>
    <button class="primary" id="btnLaunchTask" onclick="submitNewTask()">🚀 작업 시작 (Launch)</button>
  </div>
</div>

<!-- Add Account Modal -->
<div class="modal-backdrop" id="addAccountBackdrop" onclick="closeAddAccountModal()" style="display:none"></div>
<div class="modal" id="addAccountModal" style="display:none">
  <div class="modal-head">
    <h3 style="margin:0;font-size:16px">새 워커 계정 등록 ➕</h3>
    <button class="ghost sm" onclick="closeAddAccountModal()" style="font-size:16px;padding:2px 6px">✕</button>
  </div>
  <div class="modal-body">
    <div style="margin-bottom:14px">
      <label class="form-label">대상 CLI <span style="color:var(--bad)">*</span></label>
      <select id="accCli" class="form-select">
        <option value="agy">agy (Google Antigravity CLI)</option>
        <option value="codex">codex (OpenAI Codex CLI)</option>
        <option value="claude">claude (Anthropic Claude Code)</option>
        <option value="grok">grok (xAI Grok CLI)</option>
      </select>
    </div>
    <div style="margin-bottom:14px">
      <label class="form-label">계정 식별 이름 (영문/숫자/._-) <span style="color:var(--bad)">*</span></label>
      <input type="text" id="accName" class="form-input" placeholder="예: work, acc2, sub1" autocomplete="off">
      <div class="mute" style="font-size:11.5px;margin-top:4px">
        원하는 만큼 여러 계정을 등록할 수 있습니다. 한 계정의 할당량(Quota)이 소진되면 자동으로 다음 계정으로 전환되어 작업을 이어갑니다.
      </div>
    </div>
  </div>
  <div class="modal-footer">
    <button onclick="closeAddAccountModal()">취소</button>
    <button class="primary" id="btnAddAccount" onclick="submitAddAccount()">⚡ 계정 등록 및 원클릭 로그인</button>
  </div>
</div>

<div id="toast"></div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

let lastState = null;
let isPaused = false;
let countdown = 5;
let timerInterval = null;
let currentFilter = "all";
let currentJobDetail = null;

// Notification Toast
function toast(msg, isError = false) {
  const el = $("toast");
  el.textContent = (isError ? "⚠️ " : "✓ ") + msg;
  el.className = "show";
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = "", 4000);
}

// API Post helper
async function post(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Shuttle": "1" },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) {
      const err = data.error || ("오류 " + res.status);
      toast(err, true);
      throw new Error(err);
    }
    return data;
  } catch (err) {
    if (!err.message) toast("네트워크 요청 실패", true);
    throw err;
  }
}

// Auto Refresh & Countdown Engine
function initTimer() {
  if (timerInterval) clearInterval(timerInterval);
  countdown = 5;
  updateCountdownUI();

  timerInterval = setInterval(() => {
    if (isPaused) return;
    countdown--;
    if (countdown <= 0) {
      countdown = 5;
      load();
    }
    updateCountdownUI();
  }, 1000);
}

function updateCountdownUI() {
  $("syncTimer").textContent = countdown + "s";
  const pct = (countdown / 5) * 100;
  $("syncBar").style.width = pct + "%";
}

function togglePause() {
  isPaused = !isPaused;
  $("pauseIcon").textContent = isPaused ? "▶" : "⏸";
  $("liveDot").className = isPaused ? "live-dot paused" : "live-dot";
  $("liveText").textContent = isPaused ? "PAUSED" : "LIVE";
  toast(isPaused ? "자동 갱신을 일시정지했습니다" : "자동 갱신을 재개했습니다");
}

async function manualRefresh() {
  const btn = $("refreshBtn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 갱신 중';
  try {
    await load();
    countdown = 5;
    updateCountdownUI();
    toast("최신 상태로 갱신되었습니다");
  } finally {
    btn.disabled = false;
    btn.innerHTML = "<span>새로고침</span>";
  }
}

// Actions
async function triggerAuthCheck() {
  const btn = $("btnReauth");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 확인 중';
  try {
    await post("/api/auth", {});
    await load();
    toast("워커 인증 상태를 갱신했습니다");
  } finally {
    btn.disabled = false;
    btn.innerHTML = "다시 확인";
  }
}

async function refreshModels() {
  const btn = $("btnRefreshModels");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 갱신 중';
  try {
    await post("/api/models", {});
    await load();
    toast("무료 및 agy 모델 목록을 갱신했습니다");
  } finally {
    btn.disabled = false;
    btn.innerHTML = "모델 목록 갱신";
  }
}

async function login(cli, account) {
  try {
    toast(`${cli} 로그인 요청을 전송했습니다…`);
    const r = await post("/api/login", { cli, account });
    if (r.browser) {
      toast(`${cli} 구글 로그인 페이지를 브라우저에 엽니다.`);
    } else if (r.opened_window) {
      toast(`${cli} 로그인 터미널 창을 열었습니다.`);
    } else if (r.command) {
      toast(`실행 안내: ${r.command}`);
    }
    setTimeout(() => { post("/api/auth", {}).then(load); }, 15000);
  } catch (e) {}
}

async function autoLogin(cli, account) {
  try {
    toast(`⚡ ${cli}@${account} 원클릭 자동 로그인을 시작합니다…`);
    await post("/api/auto-login", { cli, account });
    toast("브라우저가 열렸습니다. Google 계정을 선택하시면 인증 코드가 자동 감지됩니다.");
    setTimeout(() => { post("/api/auth", {}).then(load); }, 12000);
  } catch (err) {
    toast("원클릭 로그인 실패: " + (err.error || err.message || err), true);
  }
}

function openAddAccountModal() {
  $("addAccountBackdrop").style.display = "block";
  $("addAccountModal").style.display = "flex";
  $("accName").value = "";
  $("accName").focus();
}

function closeAddAccountModal() {
  $("addAccountBackdrop").style.display = "none";
  $("addAccountModal").style.display = "none";
}

async function submitAddAccount() {
  const cli = $("accCli").value;
  const account = ($("accName").value || "").trim();
  if (!account) {
    toast("계정 식별 이름을 입력해 주세요 (예: work, acc2)", true);
    $("accName").focus();
    return;
  }
  const btn = $("btnAddAccount");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 등록 및 로그인 중…';
  try {
    await post("/api/account/add", { cli, account, login: true });
    toast(`계정 '${account}'이 등록되었습니다! 로그인을 진행합니다.`);
    closeAddAccountModal();
    if (cli === "agy") {
      await autoLogin(cli, account);
    }
    await load();
  } catch (err) {
    toast("계정 등록 실패: " + (err.error || err.message || err), true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = "⚡ 계정 등록 및 원클릭 로그인";
  }
}

async function deleteAccount(cli, account) {
  if (!confirm(`${cli} 계정 '${account}'을 정말 삭제하시겠습니까?`)) return;
  try {
    await post("/api/account/delete", { cli, account });
    toast(`계정 '${account}'이 삭제되었습니다.`);
    await load();
  } catch (err) {
    toast("계정 삭제 실패: " + (err.error || err.message || err), true);
  }
}

async function sendCode(cli, account, inputId) {
  const el = $(inputId);
  const code = (el && el.value || "").trim();
  if (!code) { toast("코드를 붙여 넣어 주세요."); return; }
  try {
    await post("/api/signin-code", { cli, account, code });
    el.value = "";
    toast(`${cli} 코드를 넘겼습니다. 로그인을 확인합니다…`);
    setTimeout(() => { post("/api/auth", {}).then(load); }, 10000);
  } catch (e) {}
}

async function clearLimit(group) {
  await post("/api/clear", { group });
  toast(`한도 제한을 해제했습니다: ${group}`);
  load();
}

async function clearAllLimits() {
  await post("/api/clear", { group: "" });
  toast("모든 워커의 휴식/한도 제한을 즉시 해제했습니다");
  load();
}

async function cancelJob(id) {
  if (confirm(`'${id}' 작업을 취소하시겠습니까?`)) {
    await post("/api/cancel", { id });
    toast(`'${id}' 작업을 취소했습니다`);
    closeDrawer();
    load();
  }
}

async function discardJob(id) {
  if (confirm(`'${id}' 작업과 연결된 워크트리를 영구 폐기하시겠습니까?`)) {
    await post("/api/discard", { id });
    toast(`'${id}' 작업을 버렸습니다`);
    closeDrawer();
    load();
  }
}

// Formatters
const KIND_LABELS = { unavailable: "쿼터 소진", auth: "로그인 필요", blocked: "권한 차단", stalled: "응답 지연" };
const STATUS_META = {
  running: { label: "실행 중", cls: "warn", icon: "⚡" },
  starting: { label: "시작 중", cls: "warn", icon: "⏳" },
  done: { label: "완료됨", cls: "ok", icon: "✓" },
  merged: { label: "머지됨", cls: "ok", icon: "✓" },
  failed: { label: "실패", cls: "bad", icon: "✕" },
  cancelled: { label: "취소됨", cls: "idle", icon: "⊘" },
  discarded: { label: "폐기됨", cls: "idle", icon: "🗑" }
};

function formatDuration(sec) {
  if (!sec && sec !== 0) return "-";
  const s = Math.round(sec);
  if (s < 60) return s + "초";
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return `${m}분 ${rem}초`;
  const h = Math.floor(m / 60);
  return `${h}시간 ${m % 60}분`;
}

// Render Functions
function render(s) {
  lastState = s;

  // Header Time
  $("updTime").textContent = new Date(s.now * 1000).toLocaleTimeString();

  // KPIs
  const runningJobs = s.jobs.filter(j => j.status === "running" || j.status === "starting");
  $("kpiActiveJobs").textContent = runningJobs.length;
  $("kpiTotalJobs").textContent = `전체 ${s.jobs.length}건 등록됨`;

  const authRows = (s.auth && s.auth.rows) || [];
  const loggedInCount = authRows.filter(r => r.logged_in === true).length;
  $("kpiAuth").textContent = `${loggedInCount} / ${authRows.length}`;
  $("kpiAuthSub").textContent = s.auth.busy ? "확인 작업 진행 중…" : "모든 인증 상태 확인됨";

  $("kpiLimits").textContent = s.limits.length;
  $("kpiLimitsSub").textContent = s.limits.length ? `${s.limits.length}개 그룹 휴식 중` : "모든 워커 정상 가용";

  const topFree = s.free && s.free[0] ? s.free[0].replace("opencode/", "") : "-";
  $("kpiFree").textContent = topFree;
  $("kpiFreeSub").textContent = `opencode 풀 총 ${s.free ? s.free.length : 0}개 모델`;

  // Auth Section
  renderAuth(s);

  // Limits Section
  renderLimits(s);

  // Free Models Section
  renderFreeModels(s);

  // Chains Section
  renderChains(s);

  // Ladder Section
  renderLadder(s);

  // Ratings Section
  renderRatings(s);

  // Jobs Section
  filterJobs();
}

function renderAuth(s) {
  const a = s.auth || { rows: [], busy: false };
  $("authCount").textContent = a.rows.length;
  $("authAt").textContent = a.busy ? "확인 중…" : (a.checked ? new Date(a.checked * 1000).toLocaleTimeString() + " 기준" : "");

  // the 5 s refresh would wipe a response code being typed: leave the table alone until it is sent
  if ([...document.querySelectorAll('#authBody input[id^="code-"]')].some(i => i.value || i === document.activeElement)) return;

  if (!a.rows.length) {
    $("authBody").innerHTML = `<tr><td colspan="5" class="empty-state"><div class="empty-icon">🔍</div><div>${a.busy ? "워커 인증 정보를 읽는 중입니다…" : "등록된 워커가 없습니다."}</div></td></tr>`;
    return;
  }

  $("authBody").innerHTML = a.rows.map(r => {
    let statusPill = '<span class="pill idle"><span class="pill-dot"></span>미확인</span>';
    if (r.logged_in === true) {
      statusPill = '<span class="pill ok"><span class="pill-dot"></span>로그인됨</span>';
    } else if (r.logged_in === false) {
      statusPill = '<span class="pill bad"><span class="pill-dot"></span>인증 필요</span>';
    }

    const signinKey = `${r.cli}@${r.account}`;
    const signin = (s.signin || {})[signinKey] || {};
    let actionBtn = "";
    if (r.logged_in === false) {
      if (r.cli === "agy") {
        actionBtn = `
          <button class="primary sm" style="background:#2563eb" onclick="autoLogin('${esc(r.cli)}','${esc(r.account)}')">⚡ 원클릭 로그인</button>
          <button class="sm ghost" onclick="login('${esc(r.cli)}','${esc(r.account)}')">콘솔</button>
        `;
      } else {
        actionBtn = `<button class="primary sm" onclick="login('${esc(r.cli)}','${esc(r.account)}')">로그인</button>`;
      }
    }
    if (r.account !== "default") {
      actionBtn += ` <button class="sm ghost" style="color:var(--bad)" onclick="deleteAccount('${esc(r.cli)}','${esc(r.account)}')">삭제</button>`;
    }
    let directLink = "";
    if (signin.state === "awaiting_browser" && signin.url) {
      const codeId = `code-${esc(r.cli)}-${esc(r.account)}`;
      directLink = `
        <div style="margin-top:6px;padding:8px;border-radius:6px;background:var(--surface);border:1px solid var(--line)">
          <div style="display:flex;align-items:center;gap:6px;font-weight:600;color:var(--acc);font-size:12px">
            <span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span>
            브라우저에서 로그인하면 코드를 자동으로 감지합니다
          </div>
          <div style="margin-top:4px"><a href="${esc(signin.url)}" target="_blank" rel="noopener" style="font-size:11.5px;color:var(--acc);text-decoration:none">↗ 브라우저 수동 열기</a></div>
          <div style="margin-top:6px;display:flex;gap:6px;align-items:center">
            <input id="${codeId}" type="password" autocomplete="off" placeholder="자동 감지 실패 시 수동 코드 붙여넣기" style="font-size:11.5px;flex:1;min-width:0">
            <button class="sm" onclick="sendCode('${esc(r.cli)}','${esc(r.account)}','${codeId}')">제출</button>
          </div>
          ${signin.error ? `<div class="mute" style="font-size:11.5px;color:var(--bad);margin-top:4px">${esc(signin.error)}</div>` : ""}
        </div>`;
    } else if (signin.state === "code_sent") {
      directLink = `<div class="mute" style="font-size:12px;margin-top:4px">⚡ 코드를 수신하여 제출했습니다. 검증 중…</div>`;
    }

    return `<tr>
      <td><b>${esc(r.cli)}</b></td>
      <td class="mute">${esc(r.account)}</td>
      <td>${statusPill}</td>
      <td class="mono mute" style="font-size:12px">${esc((r.detail || "").split("\n").pop())}${directLink}</td>
      <td style="text-align:right">${actionBtn}</td>
    </tr>`;
  }).join("");
}

function renderLimits(s) {
  $("limitsCount").textContent = s.limits.length;
  if (!s.limits.length) {
    $("limitsBody").innerHTML = `<tr><td colspan="4" class="empty-state"><div class="empty-icon">✓</div><div>쉬고 있는 워커가 없습니다. 모든 모델이 가용합니다.</div></td></tr>`;
    return;
  }

  $("limitsBody").innerHTML = s.limits.map(l => {
    const kindLabel = KIND_LABELS[l.kind] || l.kind;
    const kindCls = l.kind === "unavailable" ? "warn" : "bad";
    return `<tr>
      <td class="mono"><b>${esc(l.group)}</b></td>
      <td>
        <span class="pill ${kindCls}"><span class="pill-dot"></span>${esc(kindLabel)}</span>
        <div class="mute" style="font-size:11.5px;margin-top:3px">${esc(l.why || "")}</div>
      </td>
      <td class="mono mute">${esc(l.until)}</td>
      <td style="text-align:right">
        <button class="sm" onclick="clearLimit('${esc(l.group)}')">해제</button>
      </td>
    </tr>`;
  }).join("");
}

function renderFreeModels(s) {
  const list = s.free || [];
  const top = s.free_top || 3;
  $("freeCount").textContent = list.length;

  if (!list.length) {
    $("freeChips").innerHTML = '<div class="empty-state">무료 모델 목록을 받지 못했습니다. 목록 갱신을 눌러보세요.</div>';
    return;
  }

  $("freeChips").innerHTML = list.map((m, i) => {
    const isTop = i < top;
    const name = m.replace("opencode/", "");
    return `<div class="free-chip ${isTop ? 'top-priority' : ''}">
      <span class="free-chip-rank">${i + 1}</span>
      <span>${esc(name)}</span>
      ${isTop ? '<span style="font-size:10px;opacity:0.8">우선</span>' : ''}
    </div>`;
  }).join("");
}

function renderChains(s) {
  const tiers = s.chains || {};
  $("chainTiers").innerHTML = Object.entries(tiers).map(([tierName, chain]) => {
    return `<div class="chain-tier">
      <div class="chain-tier-head">
        <span class="chain-tier-name">${esc(tierName)}</span>
        <span class="mute" style="font-size:11.5px">${chain.length}단계 폴백</span>
      </div>
      <div class="chain-steps">
        ${chain.map((step, idx) => {
          const isRest = !!step.resting_until;
          const cleanName = step.brain.replace("@default", "").replace("opencode/", "");
          const restNote = isRest ? `${KIND_LABELS[step.rest_kind] || step.rest_kind} (~${step.resting_until})` : "";
          return `
            <span class="chain-step ${isRest ? 'rest' : ''}" title="${esc(restNote || step.quota_group)}">
              ${esc(cleanName)}
            </span>
            ${idx < chain.length - 1 ? '<span class="chain-arrow">→</span>' : ''}
          `;
        }).join("")}
      </div>
    </div>`;
  }).join("");
}

function renderLadder(s) {
  const ladder = s.ladder || [];
  $("ladderBody").innerHTML = ladder.map(r => {
    const rankBadge = r.rank === null ? '<span class="mute">-</span>' : `<span class="pill idle" style="font-weight:700">#${r.rank + 1}</span>`;
    const efforts = (r.efforts || []).map(e => `<span class="pill idle" style="font-size:11px">${esc(e)}</span>`).join(" ");
    return `<tr>
      <td>${rankBadge}</td>
      <td class="mono"><b>${esc(r.family)}</b>${r.older ? ' <span class="mute" style="font-size:11px">(구버전)</span>' : ''}</td>
      <td>${efforts}</td>
      <td class="mono mute">${esc(r.group)}</td>
    </tr>`;
  }).join("");
}

function renderRatings(s) {
  const ratings = s.ratings || [];
  $("ratingsCount").textContent = ratings.length;
  if (!ratings.length) {
    $("ratingsBody").innerHTML = `<tr><td colspan="6" class="empty-state"><div class="empty-icon">📊</div><div>아직 기록된 모델 평가 이력이 없습니다.</div></td></tr>`;
    return;
  }
  $("ratingsBody").innerHTML = ratings.map(r => {
    const pct = Math.round(r.score * 100);
    const scoreColor = r.score >= 0.7 ? "var(--ok)" : (r.score >= 0.4 ? "var(--warn)" : "var(--bad)");
    const counts = Object.entries(r.counts || {}).map(([k, v]) => `${k}: ${v}`).join(", ");
    const cleanBrain = r.brain.replace("opencode/", "");
    return `<tr>
      <td class="mono"><b>${esc(cleanBrain)}</b></td>
      <td><span class="pill idle">${esc(r.difficulty)}</span></td>
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <div style="flex:1;height:7px;background:var(--line);border-radius:4px;overflow:hidden">
            <div style="width:${pct}%;height:100%;background:${scoreColor}"></div>
          </div>
          <span style="font-weight:700;color:${scoreColor};font-size:12px">${r.score.toFixed(2)}</span>
        </div>
      </td>
      <td class="mono">${r.events}회</td>
      <td class="mute" style="font-size:11.5px">${esc(counts || "-")}</td>
      <td class="mute">${r.avg_sec ? formatDuration(r.avg_sec) : "-"}</td>
    </tr>`;
  }).join("");
}

// Jobs Filtering and Rendering
function setJobFilter(filter) {
  currentFilter = filter;
  document.querySelectorAll(".filter-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-filter") === filter);
  });
  filterJobs();
}

function filterJobs() {
  if (!lastState || !lastState.jobs) return;
  const jobs = lastState.jobs;
  const q = ($("jobSearch").value || "").trim().toLowerCase();

  // Counts
  $("jobsCount").textContent = jobs.length;
  $("filterCountAll").textContent = jobs.length;
  $("filterCountRunning").textContent = jobs.filter(j => j.status === "running" || j.status === "starting").length;
  $("filterCountDone").textContent = jobs.filter(j => j.status === "done" || j.status === "merged").length;
  $("filterCountFailed").textContent = jobs.filter(j => j.status === "failed").length;
  $("filterCountDiscarded").textContent = jobs.filter(j => j.status === "discarded" || j.status === "cancelled").length;

  const filtered = jobs.filter(j => {
    // Filter tab
    if (currentFilter === "running" && !(j.status === "running" || j.status === "starting")) return false;
    if (currentFilter === "done" && !(j.status === "done" || j.status === "merged")) return false;
    if (currentFilter === "failed" && j.status !== "failed") return false;
    if (currentFilter === "discarded" && !(j.status === "discarded" || j.status === "cancelled")) return false;

    // Search query
    if (q) {
      const haystack = `${j.id} ${j.title || ''} ${j.cli || ''} ${j.model || ''} ${j.error || ''}`.toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  });

  if (!filtered.length) {
    $("jobsBody").innerHTML = `<tr><td colspan="6" class="empty-state"><div class="empty-icon">📁</div><div>조건에 일치하는 작업이 없습니다.</div></td></tr>`;
    return;
  }

  $("jobsBody").innerHTML = filtered.map(j => {
    const meta = STATUS_META[j.status] || { label: j.status, cls: "idle", icon: "·" };
    const live = j.status === "running" || j.status === "starting";
    let statusPill = `<span class="pill ${meta.cls}"><span class="pill-dot"></span>${meta.label}</span>`;
    let liveSub = "";
    if (j.phase === "waiting_login") {
      statusPill = '<span class="pill bad pulse"><span class="pill-dot"></span>로그인 대기</span>';
    } else if (live && j.heartbeat) {
      const hb = j.heartbeat;
      const ago = hb.active_sec_ago;
      if (hb.state === "active") {
        statusPill = `<span class="pill ok pulse" title="활동 감지: ${ago}초 전"><span class="pill-dot"></span>활동 중 (${ago}s 전)</span>`;
      } else if (hb.state === "thinking") {
        statusPill = `<span class="pill acc pulse" title="추론/생성 중: ${ago}초 전"><span class="pill-dot"></span>추론 중 (${ago}s 전)</span>`;
      } else {
        statusPill = `<span class="pill warn pulse" title="무반응: ${ago}초 전 (스톨 한도 ${hb.stall_sec}초)"><span class="pill-dot"></span>대기 중 (${ago}s 전)</span>`;
      }
      if (hb.last_line) {
        liveSub = `<div class="mute" style="font-size:11px;margin-top:3px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(hb.last_line)}">⚡ ${esc(hb.last_line)}</div>`;
      }
    }

    const elapsed = j.elapsed_sec ? formatDuration(j.elapsed_sec) : (live ? "진행 중" : "-");
    const started = (j.started || "").replace("T", " ").slice(5, 16);

    const hbNotes = (live && j.heartbeat) ? [
      j.heartbeat.files_changed ? `${j.heartbeat.files_changed}개 파일 수정 중` : "",
      (j.heartbeat.stall_in !== undefined && j.heartbeat.stall_in !== null) ? `스톨 감지까지 ${j.heartbeat.stall_in}초` : ""
    ].filter(Boolean).join(" · ") : "";

    const notes = [
      hbNotes,
      j.error,
      j.check && ("체크: " + j.check),
      (j.needs_login || []).length ? "로그인 필요" : "",
      (j.fell_back_from || []).length ? "폴백: " + j.fell_back_from.join(", ") : ""
    ].filter(Boolean).join(" · ");

    return `<tr class="interactive-row" onclick="openJobDrawer('${esc(j.id)}')">
      <td>
        <div style="font-weight:600;color:var(--fg)">${esc(j.title || "제목 없음")}</div>
        <div class="mono mute" style="font-size:11.5px">${esc(j.id)}</div>
      </td>
      <td>${statusPill}${liveSub}</td>
      <td class="mono">
        <b>${esc(j.cli)}</b>${j.model ? '<span class="mute">:' + esc(String(j.model).replace("opencode/", "")) + '</span>' : ''}
      </td>
      <td>
        <div>${esc(started)}</div>
        <div class="mute" style="font-size:11.5px">${esc(elapsed)}</div>
      </td>
      <td class="mute" style="font-size:12px;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(notes)}">
        ${esc(notes || "-")}
      </td>
      <td style="text-align:right" onclick="event.stopPropagation()">
        <button class="sm ghost" onclick="openJobDrawer('${esc(j.id)}')">상세</button>
        ${live ? `<button class="sm" onclick="cancelJob('${esc(j.id)}')">취소</button>` : ''}
        ${!live && j.status !== "discarded" && j.status !== "merged" && j.kind === "work" ? `<button class="sm" onclick="discardJob('${esc(j.id)}')">버리기</button>` : ''}
      </td>
    </tr>`;
  }).join("");
}

// Job Drawer
async function openJobDrawer(jid, isBackgroundUpdate = false) {
  if (!isBackgroundUpdate) {
    $("drawerBackdrop").className = "drawer-backdrop open";
    $("jobDrawer").className = "drawer open";

    $("dTitle").textContent = "불러오는 중…";
    $("dId").textContent = jid;
    $("dReport").textContent = "보고서 로딩 중…";
    $("dDiffStat").textContent = "Diffstat 불러오는 중…";
    $("dDiff").textContent = "변경사항 로딩 중…";
    $("dLogs").textContent = "로그 로딩 중…";
    $("dMetaBody").innerHTML = '<tr><td class="mute">불러오는 중…</td></tr>';
    $("dFooter").innerHTML = "";
    $("dLiveBanner").style.display = "none";
  }

  try {
    const job = await post("/api/job", { id: jid, max_diff: 100000 });
    currentJobDetail = job;

    $("dTitle").textContent = job.title || "제목 없음";
    $("dId").textContent = `${job.id} (${job.cli}${job.model ? ' : ' + job.model : ''})`;

    // Live Progress Banner
    const live = job.status === "running" || job.status === "starting";
    if (live && job.heartbeat) {
      const hb = job.heartbeat;
      const hbClass = hb.state === "active" ? "ok" : (hb.state === "thinking" ? "acc" : "warn");
      const hbText = hb.state === "active" ? "정상 활동 중 (Active)" : (hb.state === "thinking" ? "추론 및 생성 중 (Thinking)" : "대기 중 (Idle)");
      $("dLiveBanner").innerHTML = `
        <div class="live-status-banner ${hb.state}">
          <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
            <div style="display:flex;align-items:center;gap:8px">
              <span class="pill ${hbClass} pulse"><span class="pill-dot"></span>${hbText}</span>
              <span style="font-size:12.5px;font-weight:600;color:var(--fg)">최근 활동 감지: ${hb.active_sec_ago}초 전</span>
            </div>
            ${(hb.stall_in !== undefined && hb.stall_in !== null) ? `<div class="mute" style="font-size:12px">스톨 방지 감지: <b>${hb.stall_in}초 남음</b> (한도 ${hb.stall_sec}초)</div>` : ''}
          </div>
          <div style="margin-top:8px;font-size:12px;display:flex;gap:16px;flex-wrap:wrap" class="mute">
            <div>수정 중인 파일: <b style="color:var(--fg)">${hb.files_changed || 0}개</b></div>
            <div>에이전트 경과 시간: <b style="color:var(--fg)">${formatDuration(hb.elapsed_sec || job.elapsed_sec)}</b></div>
            <div>워커 프로세스 PID: <span class="mono">${job.agent_pid || '-'}</span></div>
          </div>
          ${hb.last_line ? `<div style="margin-top:8px;padding:6px 10px;border-radius:4px;background:var(--card);font-size:12px;font-family:var(--font-mono);color:var(--fg);border:1px solid var(--line);word-break:break-all">최근 감지 로그: ${esc(hb.last_line)}</div>` : ''}
        </div>
      `;
      $("dLiveBanner").style.display = "block";
    } else {
      $("dLiveBanner").style.display = "none";
    }

    // Report
    $("dReport").textContent = job.report || "(작업자가 작성한 REPORT.md가 없습니다)";

    // Diff
    const diffStat = (job.diffstat || "").trim();
    $("dDiffStat").textContent = diffStat.split("\n")[0] || "Diffstat";
    $("dDiff").innerHTML = renderDiff(job.diff);

    // Logs
    const logContent = [
      job.stderr_tail ? "=== STDERR ===\n" + job.stderr_tail : "",
      job.stdout_tail ? "=== STDOUT ===\n" + job.stdout_tail : "",
      job.answer ? "=== REVIEW ANSWER ===\n" + job.answer : ""
    ].filter(Boolean).join("\n\n");
    $("dLogs").textContent = logContent || "(기록된 로그가 없습니다)";

    // Metadata Table
    $("dMetaBody").innerHTML = `
      <tr><th style="width:120px">상태</th><td>${esc(job.status)} ${job.phase ? '· ' + esc(job.phase) : ''}</td></tr>
      <tr><th>소요 시간</th><td>${formatDuration(job.elapsed_sec)} (에이전트: ${formatDuration(job.agent_sec)})</td></tr>
      <tr><th>브랜치</th><td class="mono">${esc(job.branch || '-')}</td></tr>
      <tr><th>워크트리</th><td class="mono" style="font-size:11px">${esc(job.worktree || '-')}</td></tr>
      <tr><th>종료 코드</th><td class="mono">${job.exit_code !== undefined ? job.exit_code : '-'}</td></tr>
      ${job.error ? `<tr><th>오류 내용</th><td style="color:var(--bad)">${esc(job.error)}</td></tr>` : ''}
    `;

    // Footer actions
    const live = job.status === "running" || job.status === "starting";
    let footerBtns = `<button onclick="closeDrawer()">닫기</button>`;
    if (live) {
      footerBtns += `<button class="sm" style="color:var(--bad)" onclick="cancelJob('${esc(job.id)}')">작업 취소</button>`;
    } else {
      if (job.status === "done" && job.kind === "work") {
        footerBtns += `<button class="sm" onclick="recheckJob('${esc(job.id)}')">체크 재실행</button>`;
        footerBtns += `<button class="sm" onclick="followupJob('${esc(job.id)}')">추가 수정 (Followup)</button>`;
        footerBtns += `<button class="sm primary" onclick="mergeJob('${esc(job.id)}')">머지 (Merge to main)</button>`;
      } else if (job.status === "failed" && job.kind === "work") {
        footerBtns += `<button class="sm" onclick="recheckJob('${esc(job.id)}')">체크 재실행</button>`;
        footerBtns += `<button class="sm" onclick="followupJob('${esc(job.id)}')">재시도 (Retry)</button>`;
      }
      if (job.status !== "discarded" && job.status !== "merged") {
        footerBtns += `<button class="sm" style="color:var(--bad)" onclick="discardJob('${esc(job.id)}')">작업 및 워크트리 버리기</button>`;
      }
    }
    $("dFooter").innerHTML = footerBtns;

  } catch (err) {
    $("dTitle").textContent = "불러오기 실패";
    $("dReport").textContent = "작업 상세 정보를 가져오지 못했습니다: " + (err.message || err);
  }
}

function closeDrawer() {
  $("drawerBackdrop").className = "drawer-backdrop";
  $("jobDrawer").className = "drawer";
}

function switchDrawerTab(tab) {
  document.querySelectorAll(".drawer-tab").forEach((el, idx) => {
    const isTarget = (tab === 'report' && idx === 0) || (tab === 'diff' && idx === 1) || (tab === 'logs' && idx === 2) || (tab === 'meta' && idx === 3);
    el.classList.toggle("active", isTarget);
  });
  $("tabReport").style.display = tab === 'report' ? 'block' : 'none';
  $("tabDiff").style.display = tab === 'diff' ? 'block' : 'none';
  $("tabLogs").style.display = tab === 'logs' ? 'block' : 'none';
  $("tabMeta").style.display = tab === 'meta' ? 'block' : 'none';
}

function renderDiff(diffText) {
  if (!diffText || diffText.startsWith("(not included") || diffText.trim() === "") {
    return `<div class="mute" style="padding:20px 0;text-align:center">${esc(diffText || "변경된 코드가 없습니다.")}</div>`;
  }
  return diffText.split("\n").map(line => {
    if (line.startsWith("+++") || line.startsWith("---")) {
      return `<span class="diff-line-meta">${esc(line)}</span>`;
    }
    if (line.startsWith("+")) {
      return `<span class="diff-line-add">${esc(line)}</span>`;
    }
    if (line.startsWith("-")) {
      return `<span class="diff-line-del">${esc(line)}</span>`;
    }
    if (line.startsWith("@@")) {
      return `<span class="diff-line-hunk">${esc(line)}</span>`;
    }
    if (line.startsWith("diff --git") || line.startsWith("index ")) {
      return `<span class="diff-line-meta" style="color:var(--acc);margin-top:8px">${esc(line)}</span>`;
    }
    return `<span>${esc(line)}</span>`;
  }).join("\n");
}

function copyDrawerDiff() {
  const text = (currentJobDetail && currentJobDetail.diff) || $("dDiff").textContent;
  navigator.clipboard.writeText(text).then(() => {
    toast("Diff가 클립보드에 복사되었습니다");
  });
}

function copyDrawerLogs() {
  const text = $("dLogs").textContent;
  navigator.clipboard.writeText(text).then(() => {
    toast("로그가 클립보드에 복사되었습니다");
  });
}

async function mergeJob(jid) {
  const msg = prompt("커밋 메시지 (비워두면 기본 메시지가 사용됩니다):");
  if (msg === null) return;
  try {
    toast("작업을 main 브랜치로 머지하는 중…");
    const r = await post("/api/merge", { id: jid, message: msg });
    toast(`머지 완료! 커밋: ${(r.merged_as || "").slice(0, 8)} (${r.land_sec}초)`);
    closeDrawer();
    await load();
  } catch (err) {
    toast("머지 실패: " + (err.error || err.message || err), true);
  }
}

async function followupJob(jid) {
  const feedback = prompt("워커에게 전달할 추가 수정 / 피드백 지시사항:");
  if (!feedback || !feedback.trim()) return;
  try {
    toast("워커에게 후속 수정을 요청했습니다…");
    await post("/api/followup", { id: jid, feedback: feedback.trim() });
    toast("후속 작업이 시작되었습니다!");
    closeDrawer();
    await load();
  } catch (err) {
    toast("후속 요청 실패: " + (err.error || err.message || err), true);
  }
}

async function recheckJob(jid) {
  try {
    toast("Acceptance check 실행 중…");
    const r = await post("/api/check", { id: jid });
    if (r.passed) {
      toast("체크 성공: 테스트를 통과했습니다! (exit 0)");
    } else {
      toast("체크 실패 (exit " + r.exit_code + ")", true);
    }
    await openJobDrawer(jid);
    await load();
  } catch (err) {
    toast("체크 실행 실패: " + (err.error || err.message || err), true);
  }
}

// New Task Modal
function openNewJobModal() {
  $("newTaskModalBackdrop").style.display = "block";
  $("newTaskModal").style.display = "flex";
  $("ntTask").focus();
}

function closeNewTaskModal() {
  $("newTaskModalBackdrop").style.display = "none";
  $("newTaskModal").style.display = "none";
}

async function submitNewTask() {
  const task = ($("ntTask").value || "").trim();
  if (!task) {
    toast("작업 내용을 입력해 주세요", true);
    $("ntTask").focus();
    return;
  }
  const difficulty = $("ntDifficulty").value;
  const cli = $("ntCli").value;
  const check = ($("ntCheck").value || "").trim();

  const btn = $("btnLaunchTask");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 발사 중…';

  try {
    const res = await post("/api/start", { task, difficulty, cli, check });
    toast(`🚀 Shuttle 작업 발사 완료! ID: ${res.id}`);
    closeNewTaskModal();
    $("ntTask").value = "";
    $("ntCheck").value = "";
    await load();
    if (res.id) {
      setTimeout(() => openJobDrawer(res.id), 500);
    }
  } catch (err) {
    toast("작업 시작 실패: " + (err.error || err.message || err), true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = "🚀 작업 시작 (Launch)";
  }
}

// Theme Engine
const THEMES = ["auto", "dark", "light"];
function getTheme() {
  return localStorage.getItem("shuttle_theme") || "auto";
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("shuttle_theme", theme);
  $("themeIcon").textContent = theme === "dark" ? "🌙" : (theme === "light" ? "☀️" : "🌓");
}

function cycleTheme() {
  const cur = getTheme();
  const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  setTheme(next);
  toast(`테마: ${next === 'dark' ? '다크 모드' : next === 'light' ? '라이트 모드' : '시스템 설정 동기화'}`);
}

// Initial Data Load
async function load() {
  try {
    const res = await fetch("/api/state");
    const data = await res.json();
    if (!res.ok) throw data;
    render(data);

    // If job drawer is open and viewing an active job, refresh the drawer in background
    if ($("jobDrawer").classList.contains("open") && currentJobDetail) {
      const curJob = (data.jobs || []).find(j => j.id === currentJobDetail.id);
      if (curJob && (curJob.status === "running" || curJob.status === "starting")) {
        openJobDrawer(curJob.id, true);
      }
    }
  } catch (err) {
    toast("상태 동기화 실패: " + (err.error || err.message || err), true);
  }
}

// Keyboard Shortcuts
window.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    closeNewTaskModal();
    closeAddAccountModal();
    closeDrawer();
  }
});

// Boot
setTheme(getTheme());
load();
initTimer();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="open it in the default browser")
    a = ap.parse_args()
    serve(a.port, a.open)
