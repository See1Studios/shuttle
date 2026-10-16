#!/usr/bin/env python3
"""Local management dashboard: who is signed in (with a button that opens the sign-in window), resting quota
groups, opencode's free models, the difficulty chains, and the jobs. Stdlib only; listens on 127.0.0.1.

    python dashboard.py [--port 8765] [--open]

Writes go through POST with an `X-Delegate: 1` header, which a page from another origin cannot send without a
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
import delegate as d  # noqa: E402

DEFAULT_PORT = 8765
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
    for tier in (B.config().get("tiers") or {}):
        try:
            chains[tier] = d.chain_view(difficulty=tier)
        except (RuntimeError, ValueError) as e:
            chains[tier] = [{"brain": "error: %s" % e, "resting_until": "", "rest_kind": ""}]
    return {"jobs": d.list_jobs(False, 40), "limits": B.limits(), "chains": chains,
            "free": B.opencode_free_models(), "free_top": B.config().get("opencode_free_top"),
            "auth": auth_view(), "now": time.time()}


def act(path: str, body: dict) -> object:
    if path == "/api/login":
        res = d.login(body["cli"], body.get("account") or "default")
        threading.Timer(1.0, _refresh_auth).start()
        return res
    if path == "/api/auth":
        return auth_view(refresh=True)
    if path == "/api/clear":
        return {"cleared": B.clear_limits(body.get("group", ""))}
    if path == "/api/models":
        return {"free": B.opencode_free_models(refresh=True)}
    if path == "/api/cancel":
        return d.cancel(body["id"])
    if path == "/api/discard":
        return d.discard(body["id"])
    if path == "/api/job":
        return d.result(body["id"])
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
        if self.headers.get("X-Delegate") != "1":
            return self._send(403, {"error": "missing X-Delegate header"})
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
    print("delegate dashboard: %s" % url, flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    srv.serve_forever()


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delegate 관리</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--mute:#667085;--line:#e4e7ec;--ok:#12805c;--okbg:#e7f6ef;
--bad:#b42318;--badbg:#fdecea;--warn:#a15c07;--warnbg:#fef4e6;--acc:#2f5bea;--accfg:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ec;--mute:#98a2b3;--line:#2a2f3a;
--ok:#4ade80;--okbg:#12291e;--bad:#f87171;--badbg:#2d1414;--warn:#fbbf24;--warnbg:#2c2210;--acc:#6d8cff;--accfg:#0f1115}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
h1{font-size:17px;margin:0}main{max-width:1200px;margin:0 auto;padding:16px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
section.wide{grid-column:1/-1}h2{font-size:14px;margin:0 0 10px;display:flex;align-items:center;gap:8px}
h2 .sp{flex:1}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 6px;border-top:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--mute);font-size:12px;border-top:0}.mute{color:var(--mute)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;word-break:break-all}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}.warn{background:var(--warnbg);color:var(--warn)}.idle{background:var(--line);color:var(--mute)}
button{font:inherit;font-size:12.5px;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;white-space:nowrap}
button.p{background:var(--acc);color:var(--accfg);border-color:var(--acc)}button:disabled{opacity:.5;cursor:default}
.chain{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 10px}.chain span{padding:2px 8px;border:1px solid var(--line);border-radius:6px;font-size:12px}
.chain span.rest{opacity:.55;text-decoration:line-through}#toast{position:fixed;right:16px;bottom:16px;max-width:420px;background:var(--fg);color:var(--bg);padding:10px 14px;border-radius:8px;display:none;z-index:9}
.scroll{overflow-x:auto}.empty{color:var(--mute);padding:8px 0}
</style></head><body>
<header><h1>Delegate 관리</h1><span class="mute" id="upd"></span><span style="flex:1"></span><button onclick="load()">새로고침</button></header>
<main>
<section><h2>워커 로그인<span class="sp"></span><span class="mute" id="authAt"></span><button onclick="post('/api/auth',{}).then(load)">다시 확인</button></h2>
<div class="scroll"><table id="auth"></table></div>
<p class="mute" style="margin:8px 0 0">로그인 버튼을 누르면 이 PC에 해당 CLI의 로그인 창이 뜹니다. 끝내면 자동으로 다시 확인합니다. opencode는 로그인 없이 무료 모델을 씁니다.</p></section>
<section><h2>쉬는 중 (한도 / 로그인 / 권한)<span class="sp"></span><button onclick="clearAll()">모두 해제</button></h2><div class="scroll"><table id="limits"></table></div></section>
<section><h2>opencode 무료 모델<span class="sp"></span><button onclick="post('/api/models',{}).then(load)">목록 갱신</button></h2><div id="free"></div></section>
<section><h2>난이도별 체인</h2><div id="chains"></div></section>
<section class="wide"><h2>작업</h2><div class="scroll"><table id="jobs"></table></div></section>
</main><div id="toast"></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function toast(t){const e=$("toast");e.textContent=t;e.style.display="block";clearTimeout(e._t);e._t=setTimeout(()=>e.style.display="none",5000)}
async function post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json","X-Delegate":"1"},body:JSON.stringify(b)});
 const j=await r.json();if(!r.ok){toast(j.error||("오류 "+r.status));throw j}return j}
async function login(cli,account){try{const r=await post("/api/login",{cli,account});
 toast(r.opened_window?`${cli} 로그인 창을 열었습니다. 그 창에서 로그인을 마치세요.`:`이 PC에서 실행: ${r.command}`);
 setTimeout(()=>post("/api/auth",{}).then(load),20000)}catch(e){}}
async function clearAll(){await post("/api/clear",{group:""});load()}
async function clr(g){await post("/api/clear",{group:g});load()}
async function cancelJob(id){if(confirm(id+" 작업을 취소할까요?")){await post("/api/cancel",{id});load()}}
async function discardJob(id){if(confirm(id+" 작업과 워크트리를 버릴까요?")){await post("/api/discard",{id});load()}}
const KIND={unavailable:"한도",auth:"로그인 필요",blocked:"권한 막힘"};
const ST={running:"warn",starting:"warn",done:"ok",merged:"ok",failed:"bad",cancelled:"idle",discarded:"idle"};
function render(s){
 const a=s.auth;$("authAt").textContent=a.busy?"확인 중…":(a.checked?new Date(a.checked*1000).toLocaleTimeString()+" 확인":"");
 $("auth").innerHTML="<tr><th>CLI</th><th>계정</th><th>상태</th><th>내용</th><th></th></tr>"+(a.rows.length?a.rows.map(r=>`<tr>
  <td><b>${esc(r.cli)}</b></td><td>${esc(r.account)}</td>
  <td>${r.logged_in===null?'<span class="pill idle">확인 안 함</span>':r.logged_in?'<span class="pill ok">로그인됨</span>':'<span class="pill bad">로그인 필요</span>'}</td>
  <td class="mono mute">${esc((r.detail||"").split("\n").pop())}</td>
  <td>${r.logged_in===false?`<button class="p" onclick="login('${esc(r.cli)}','${esc(r.account)}')">로그인</button>`:""}</td></tr>`).join("")
  :`<tr><td colspan=5 class="empty">${a.busy?"확인 중… (agy는 1분 가까이 걸릴 수 있습니다)":"없음"}</td></tr>`);
 $("limits").innerHTML=s.limits.length?"<tr><th>그룹</th><th>이유</th><th>까지</th><th></th></tr>"+s.limits.map(l=>`<tr>
  <td class="mono">${esc(l.group)}</td><td><span class="pill ${l.kind==="unavailable"?"warn":"bad"}">${esc(KIND[l.kind]||l.kind)}</span>
  <div class="mute" style="font-size:12px;margin-top:3px">${esc(l.why)}</div></td><td>${esc(l.until)}</td>
  <td><button onclick="clr('${esc(l.group)}')">해제</button></td></tr>`).join(""):'<tr><td class="empty">쉬는 워커 없음</td></tr>';
 $("free").innerHTML=s.free.length?`<div class="chain">${s.free.map((m,i)=>`<span ${i<s.free_top?'style="border-color:var(--acc)"':""}>${esc(m.replace("opencode/",""))}</span>`).join("")}</div>
  <div class="mute">파란 테두리 ${s.free_top}개를 차례로 씁니다 (한도에 걸리면 다음 모델로). 순서는 brains.json의 <span class="mono">opencode_prefer</span>.</div>`
  :'<div class="empty">목록을 못 받았습니다. 목록 갱신을 눌러보세요.</div>';
 $("chains").innerHTML=Object.entries(s.chains).map(([t,c])=>`<div><b>${esc(t)}</b><div class="chain">${c.map(b=>
  `<span class="${b.resting_until?"rest":""}" title="${esc(b.resting_until?(KIND[b.rest_kind]||b.rest_kind)+" ~"+b.resting_until:"")}">${esc(b.brain.replace("@default","").replace("opencode/",""))}</span>`).join("")}</div></div>`).join("");
 $("jobs").innerHTML=s.jobs.length?"<tr><th>작업</th><th>상태</th><th>워커</th><th>시작</th><th>내용</th><th></th></tr>"+s.jobs.map(j=>{
  const live=j.status==="running"||j.status==="starting";
  const st=j.phase==="waiting_login"?'<span class="pill bad">로그인 대기</span>':`<span class="pill ${ST[j.status]||"idle"}">${esc(j.status)}${j.phase?" · "+esc(j.phase):""}</span>`;
  const note=[j.error,j.check&&("check "+j.check),(j.needs_login||[]).join("; "),(j.fell_back_from||[]).join(", ")].filter(Boolean).join(" · ");
  return `<tr><td><b>${esc(j.title||"")}</b><div class="mono mute">${esc(j.id)}</div></td><td>${st}</td>
  <td class="mono">${esc(j.cli)}${j.model?":"+esc(String(j.model).replace("opencode/","")):""}</td><td class="mute">${esc((j.started||"").replace("T"," ").slice(5,16))}</td>
  <td class="mute" style="font-size:12px">${esc(note)}</td>
  <td>${live?`<button onclick="cancelJob('${esc(j.id)}')">취소</button>`:(j.status!=="discarded"&&j.status!=="merged"&&j.kind==="work"?`<button onclick="discardJob('${esc(j.id)}')">버리기</button>`:"")}</td></tr>`}).join("")
  :'<tr><td class="empty">작업 없음</td></tr>';
 $("upd").textContent=new Date(s.now*1000).toLocaleTimeString();
}
async function load(){try{const r=await fetch("/api/state");const s=await r.json();if(!r.ok)throw s;render(s)}catch(e){toast("불러오기 실패: "+(e.error||e))}}
load();setInterval(load,5000);
</script></body></html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="open it in the default browser")
    a = ap.parse_args()
    serve(a.port, a.open)
