"""Brains for delegation: which CLI, model and account does the work, and what to try next when one is out.

A brain is (cli, model, account). A job runs down an ordered chain of brains: when one hits a quota / rate limit /
outage or is not signed in, it is rested until its reset and the next brain continues in the same worktree.

Accounts never touch the user's own login: the `default` account is the CLI's normal login; any other account is a
directory under $DELEGATE_HOME/accounts/<cli>/<name>/ that the CLI is pointed at through its config-home variable
(CODEX_HOME, CLAUDE_CONFIG_DIR, GROK_HOME, HOME/USERPROFILE for agy, XDG_DATA_HOME for opencode). An account dir may
hold env.json with extra variables (an API key such as XAI_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN from
`claude setup-token`). Quotas are rested per quota group: one group per account, except agy, whose limits are per
model family ("Gemini models" vs "Claude and GPT models"), so a spent Gemini quota still leaves Claude models.

Prior art: PrivateEngine engine/tools/brain_limits.py (UNAVAILABLE_RE, rest_for) and providers/accounts.py.
Configuration: $DELEGATE_HOME/brains.json (see README), all keys optional.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HOME = Path(os.environ.get("DELEGATE_HOME") or Path.home() / ".claude-delegate")
ACCOUNTS = HOME / "accounts"
LIMITS = HOME / "limits.json"
LOGIN_PROMPTS = HOME / "login-prompts.json"   # when a sign-in window was last opened per cli@account
OPENCODE_MODELS = HOME / "opencode-models.json"   # cached `opencode models` (free built-in list)
CONFIG = HOME / "brains.json"
IS_WIN = os.name == "nt"
USER_HOME = Path.home()

# Which variables point each CLI at an account directory (all set to the same dir).
ACCOUNT_ENV = {"codex": ["CODEX_HOME"], "claude": ["CLAUDE_CONFIG_DIR"], "grok": ["GROK_HOME"],
               "agy": ["HOME", "USERPROFILE"], "opencode": ["XDG_DATA_HOME"]}

# The CLI's own interactive login, run in a new console with the account's environment.
LOGIN_ARGV = {"codex": ["codex", "login"], "claude": ["claude", "auth", "login"], "grok": ["grok", "login"],
              "agy": ["agy-real"], "opencode": ["opencode", "auth", "login"]}
LOGIN_HINT = {"codex": "browser sign-in to ChatGPT, or `codex login --with-api-key` with OPENAI_API_KEY on stdin",
              "claude": "browser sign-in; or run `claude setup-token` and put CLAUDE_CODE_OAUTH_TOKEN in the account's env.json",
              "grok": "browser sign-in (`grok login --device-auth` without a browser); or XAI_API_KEY in env.json",
              "agy": "click the Google sign-in link agy prints (no time limit); the token is saved under the account's home",
              "opencode": "pick a provider and paste its key or sign in"}

DEFAULTS = {
    "models": {"agy": ["gemini-3.8-flash-high", "claude-sonnet-5-5-high"]},
    # The point of delegating is to spend the workers' quota instead of the director's (Claude's), so the default
    # worker is agy and `claude` is never a fallback: it runs only when asked for by name or in an explicit chain.
    "default_cli": "agy",
    # Cost order on this host: opencode free models < agy (paid, plentiful) < Claude (scarce; never automatic).
    # A job without an explicit cli/chain starts at its difficulty tier's first brain; when a brain cannot make the
    # job's check pass (after its automatic fix rounds) or fails outright, the next brain takes over the worktree.
    "tiers": {
        "easy":   ["opencode", "agy:gemini-3.8-flash-medium", "agy:gemini-3.8-flash-high"],
        "normal": ["opencode", "agy:gemini-3.8-flash-high", "agy:claude-sonnet-5-5-high"],
        "hard":   ["agy:claude-sonnet-5-5-high", "agy:gemini-3.8-flash-high"],
    },
    "default_difficulty": "normal",
    # Implemented but not used: never in a default chain or fallback, not checked by `auth`, and refused when named.
    # Remove a name here (in brains.json) to use that CLI. codex and grok: no subscription on this host.
    "disabled": ["codex", "grok"],
    # opencode needs no sign-in (free built-in models), so it is a fallback everywhere.
    "fallback": {"agy": ["codex", "opencode", "grok"], "codex": ["agy", "opencode", "grok"],
                 "grok": ["agy", "codex", "opencode"], "opencode": ["agy", "codex", "grok"],
                 "claude": ["agy", "codex", "opencode", "grok"]},
    "order": "model-first",      # model-first: a model on every account before the next model; or account-first
    "auth_rest_sec": 6 * 3600,
    # A worker found signed out gets its own sign-in window opened on this machine (at most once per
    # `login_prompt_sec` per account); when the rest of the chain is out too, the job waits up to `login_wait_sec`
    # for the sign-in to land and then carries on with that worker. 0 turns the window / the wait off.
    "login_prompt_sec": 15 * 60,
    "login_wait_sec": 15 * 60,
    # opencode without a model runs its free built-in models (no sign-in): the first `opencode_free_top` of
    # `opencode models`' opencode/* list, those matching `opencode_prefer` first, each with its own quota rest.
    "opencode_prefer": ["deepseek", "glm", "nemotron-3-ultra", "longcat", "mimo", "big-pickle"],
    "opencode_free_top": 3,
    "commit_rounds": False,      # True: commit each round on the job branch (runs the repo's hooks every round)
    # agy keeps its login in a token file under its home instead of the OS credential store (see FILE_AUTH_ENV).
    "agy_file_auth": True,
    # Paths a worker's tools write that must never be committed (added to the repo's .git/info/exclude).
    "exclude": ["/.delegate/", "graft/.cache/", "graft/.graph/"],
}

UNAVAILABLE_RE = re.compile(r"quota|rate.?limit|usage limit|session limit|limit reached|resets? (at|in)|\b429\b|"
                            r"exhausted|capacity|overloaded|too many requests|"
                            r"invalid model|not recognized as a known model|unknown model|model not found|"
                            r"internal server error|service unavailable|bad gateway|gateway timeout|"
                            r"connection refused|connection reset|temporary failure|\b50[0234]\b", re.I)
AUTH_RE = re.compile(r"not (signed|logged) in|login required|please (log ?in|sign in)|run `?\w+ login|"
                     r"unauthori[sz]ed|\b401\b|authenticat\w* (failed|required|error)|failed to authenticate|"
                     r"oauth (session|token)[^\n]{0,20}(expired|invalid|revoked)|invalid (api|x-api) key|"
                     r"credentials? (expired|not found|missing|invalid)|token (has )?expired", re.I)
# A headless worker whose tool call needed a permission it could not ask for (agy: "no output produced -- a tool
# required the "command" permission that headless mode cannot prompt for, so it was auto-denied"). It exits 0.
BLOCKED_RE = re.compile(r"headless mode cannot prompt|was auto-denied|permission[^\n]{0,40}(denied|required)[^\n]{0,40}headless",
                        re.I)
RESET_IN_RE = re.compile(r"resets? in\s+(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", re.I)
UNKNOWN_REST = 900
MAX_REST = 8 * 24 * 3600


# ------------------------------------------------------------------ config and labels

def config() -> Dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def disabled() -> set:
    return set(config().get("disabled") or [])


def label(b: Dict) -> str:
    return "%s:%s@%s" % (b["cli"], b.get("model") or "default", b.get("account") or "default")


def parse(spec: str) -> Dict:
    """'cli[:model][@account]' -> brain."""
    spec = spec.strip()
    cli_model, _, account = spec.partition("@")
    cli, _, model = cli_model.partition(":")
    return {"cli": cli.strip(), "model": model.strip() if model.strip() != "default" else "",
            "account": account.strip() or "default"}


AGY_LOG_DIR = USER_HOME / ".gemini" / "antigravity-cli" / "log"
_AGY_AUTH_RE = re.compile(rb"authenticated successfully as (\S+@\S+)")


def agy_email() -> str:
    """The Google account agy is signed in with, from its newest CLI logs ("" when unknown)."""
    try:
        logs = sorted(AGY_LOG_DIR.glob("cli-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    except OSError:
        return ""
    for log in logs:
        try:
            with open(log, "rb") as f:
                f.seek(max(0, log.stat().st_size - 400000))
                found = _AGY_AUTH_RE.findall(f.read())
        except OSError:
            continue
        if found:
            return found[-1].decode("utf-8", "replace").strip(".,;'\"")
    return ""


def quota_group(b: Dict) -> str:
    """The pool a quota hit empties: per account, and for agy per model family as well. agy's single login can be
    swapped to another Google account (agy_login.py), so its default account is keyed by the signed-in email."""
    group, account = "*", b.get("account") or "default"
    if b["cli"] == "opencode" and b.get("model"):   # each free model has its own limits
        group = b["model"]
    if b["cli"] == "agy":
        m = (b.get("model") or "").lower()
        group = "gemini" if (not m or m.startswith("gemini")) else "claude-gpt"
        if account == "default":
            account = agy_email() or "default"
    return "%s@%s#%s" % (b["cli"], account, group)


# ------------------------------------------------------------------ accounts

def account_dir(cli: str, account: str) -> Optional[Path]:
    return None if account in ("", "default") else ACCOUNTS / cli / account


def accounts(cli: str) -> List[str]:
    """`default` first, then every account directory made for this CLI (sorted)."""
    d = ACCOUNTS / cli
    extra = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []
    return ["default"] + extra


# agy run outside ssh keeps its login in the OS credential store under one fixed name (gemini:antigravity on
# Windows), whatever HOME is. With SSH_CONNECTION set it takes the ssh path and keeps a token file under its home
# (~/.gemini/antigravity-cli/) instead, so each account home holds its own login and a sign-in is a plain link to
# click, with no pasted-code deadline. Set for every agy run (workers, probes, sign-in) so they all read one place.
FILE_AUTH_ENV = {"agy": {"SSH_CONNECTION": "local"}}


def file_auth_env(cli: str) -> Dict[str, str]:
    if cli == "agy" and not config().get("agy_file_auth", True):
        return {}
    return dict(FILE_AUTH_ENV.get(cli, {}))


def account_env(cli: str, account: str) -> Dict[str, str]:
    """Variables that point `cli` at `account` (none for the default login's home), plus the account's env.json."""
    d = account_dir(cli, account)
    if not d:
        return file_auth_env(cli)
    env = {k: str(d) for k in ACCOUNT_ENV.get(cli, [])}
    env.update(file_auth_env(cli))
    try:
        extra = json.loads((d / "env.json").read_text(encoding="utf-8"))
        env.update({str(k): str(v) for k, v in extra.items()})
    except (OSError, ValueError):
        pass
    return env


def add_account(cli: str, name: str) -> Path:
    if cli not in ACCOUNT_ENV:
        raise ValueError("unknown cli %r" % cli)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}", name) or name == "default":
        raise ValueError("account name must be letters, digits, . _ - (and not 'default')")
    if cli == "agy" and IS_WIN and not file_auth_env(cli):
        raise ValueError("with agy_file_auth off, agy keeps its login in the Windows credential store under one fixed "
                         "name (gemini:antigravity), whatever HOME is, so a second agy account cannot run side by side. "
                         "Turn agy_file_auth back on in brains.json, or swap the account with `agy-switch`.")
    d = ACCOUNTS / cli / name
    d.mkdir(parents=True, exist_ok=True)
    if cli == "agy":   # keep the operator's agy settings (permissions, MCP) in the new home
        src = USER_HOME / ".gemini" / "antigravity-cli" / "settings.json"
        dst = d / ".gemini" / "antigravity-cli" / "settings.json"
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return d


# ------------------------------------------------------------------ auth

def _probe(argv: List[str], env: Dict[str, str], timeout: int = 40) -> Tuple[int, str]:
    exe = shutil.which(argv[0])
    if not exe:
        return 127, "not installed"
    try:
        p = subprocess.run([exe] + argv[1:], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "status probe timed out"


def auth_status(cli: str, account: str, base_env: Dict[str, str]) -> Dict:
    """Is this CLI signed in for this account? Cheap local probes, no model call."""
    env = {**base_env, **account_env(cli, account)}
    d = account_dir(cli, account)
    out: Dict = {"cli": cli, "account": account, "logged_in": False, "detail": ""}
    if cli == "codex":
        code, text = _probe(["codex", "login", "status"], env)
        out.update(logged_in=code == 0 and "logged in" in text.lower(), detail=text.splitlines()[0] if text else "")
    elif cli == "claude":
        if env.get("CLAUDE_CODE_OAUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"):
            out.update(logged_in=True, detail="token/API key from env")
        else:
            code, text = _probe(["claude", "auth", "status"], env)
            try:
                s = json.loads(text[text.index("{"):])
                out.update(logged_in=bool(s.get("loggedIn")), detail=s.get("authMethod", ""))
            except ValueError:
                out["detail"] = text[:200]
    elif cli == "grok":   # `grok models` says "You are logged in with ..." (no model call)
        code, text = _probe(["grok", "models"], env)
        first = text.splitlines()[0] if text else ""
        out.update(logged_in=code == 0 and "logged in" in first.lower(), detail=first[:200])
    elif cli == "agy":    # listing models needs a valid login (token file with file auth, else the credential store)
        code, text = _probe(["agy-real", "models"], env, timeout=60)
        n = sum(1 for line in text.splitlines() if "\t" in line)
        out.update(logged_in=code == 0 and n > 0, detail=("%d models" % n) if n else text[-200:])
    elif cli == "opencode":   # usable without any sign-in: it ships free models; signed-in providers add more
        code, text = _probe(["opencode", "models"], env, timeout=60)
        models = [line for line in text.splitlines() if "/" in line and " " not in line.strip()]
        free = sum(1 for m in models if m.startswith("opencode/"))
        # The list sometimes comes back empty; that is a flaky probe, not a missing sign-in.
        out.update(logged_in=bool(shutil.which("opencode")),
                   detail=("%d models (%d free built-in)" % (len(models), free)) if models
                   else "free built-in models; no sign-in needed")
    if not out["logged_in"]:
        out["login"] = login_guide(cli, account)
    return out


def login_command(cli: str, account: str) -> str:
    """The command an operator types in a terminal to sign this account in (PowerShell on Windows)."""
    keep = set(ACCOUNT_ENV.get(cli, [])) | set(FILE_AUTH_ENV.get(cli, {}))   # never env.json's secrets
    env = {k: v for k, v in account_env(cli, account).items() if k in keep}
    argv = " ".join(LOGIN_ARGV[cli])
    if not env:
        return argv
    if IS_WIN:
        sets = "; ".join("$env:%s='%s'" % (k, v) for k, v in env.items())
        return "%s; %s" % (sets, argv)
    return "%s %s" % (" ".join("%s='%s'" % (k, v) for k, v in env.items()), argv)


def login_guide(cli: str, account: str) -> str:
    if cli not in LOGIN_ARGV:
        return "sign %s in with its own login command (account %s)" % (cli, account)
    return "%s  (%s)" % (login_command(cli, account), LOGIN_HINT[cli])


def open_login(cli: str, account: str, base_env: Dict[str, str]) -> Dict:
    """Start the CLI's interactive sign-in in a new console window with the account's environment. The operator
    completes it (browser / device code); nothing is typed for them."""
    if account not in ("", "default"):
        add_account(cli, account)
    argv = list(LOGIN_ARGV[cli])
    exe = shutil.which(argv[0])
    if not exe:
        raise RuntimeError("%s is not installed" % argv[0])
    env = {**base_env, **account_env(cli, account)}
    if IS_WIN:
        subprocess.Popen([exe] + argv[1:], env=env, creationflags=0x00000010)   # CREATE_NEW_CONSOLE
        opened = True
    else:
        opened = False
    return {"cli": cli, "account": account, "opened_window": opened, "command": login_command(cli, account),
            "hint": LOGIN_HINT[cli],
            "next": "finish the sign-in in the %s, then check with `delegate auth`" % ("new window" if opened else "terminal")}


# ------------------------------------------------------------------ chains and rest

def opencode_free_models(refresh: bool = False) -> List[str]:
    """opencode's free built-in models (opencode/*), best first by `opencode_prefer`. Cached for 6 h."""
    try:
        cached = json.loads(OPENCODE_MODELS.read_text(encoding="utf-8"))
        if not refresh and time.time() - cached.get("at", 0) < 6 * 3600 and cached.get("models"):
            return _rank_free(cached["models"])
    except (OSError, ValueError):
        cached = {}
    models: List[str] = []
    for _ in range(3):   # `opencode models` now and then prints nothing
        code, text = _probe(["opencode", "models"], dict(os.environ), timeout=60)
        models = [m.strip() for m in text.splitlines() if m.strip().startswith("opencode/") and " " not in m.strip()]
        if models:
            break
    if models:
        HOME.mkdir(parents=True, exist_ok=True)
        OPENCODE_MODELS.write_text(json.dumps({"at": time.time(), "models": models}), encoding="utf-8")
    else:
        models = cached.get("models") or []
    return _rank_free(models)


def _rank_free(models: List[str]) -> List[str]:
    prefer = [p.lower() for p in config().get("opencode_prefer") or []]

    def rank(m: str):
        hits = [i for i, p in enumerate(prefer) if p in m.lower()]
        return (hits[0] if hits else len(prefer), models.index(m))
    return sorted(models, key=rank)


def expand(brains: List[Dict]) -> List[Dict]:
    """A model-less opencode brain becomes its best free models, so one model's limit falls through to the next."""
    out: List[Dict] = []
    for b in brains:
        if b["cli"] == "opencode" and not b.get("model"):
            free = opencode_free_models()[:max(1, int(config().get("opencode_free_top") or 1))]
            if free:
                out += [{**b, "model": m} for m in free if {**b, "model": m} not in out]
                continue
        if b not in out:
            out.append(b)
    return out


def prompt_login(cli: str, account: str, base_env: Dict[str, str]) -> Optional[Dict]:
    """Open `cli`'s sign-in window for `account`, unless one was opened in the last `login_prompt_sec`.
    opencode needs no sign-in; a CLI without a known login flow is left to the operator."""
    every = int(config().get("login_prompt_sec") or 0)
    if cli == "opencode" or cli not in LOGIN_ARGV or every <= 0 or not IS_WIN:
        return None
    key = "%s@%s" % (cli, account or "default")
    try:
        state = json.loads(LOGIN_PROMPTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if time.time() - state.get(key, 0) < every:
        return None
    try:
        res = open_login(cli, account, base_env)
    except Exception as e:  # noqa: BLE001 - not installed etc.: the caller still reports the login command
        return {"cli": cli, "account": account, "opened_window": False, "error": str(e)}
    state[key] = time.time()
    HOME.mkdir(parents=True, exist_ok=True)
    LOGIN_PROMPTS.write_text(json.dumps(state), encoding="utf-8")
    return res


def wait_for_login(brains: List[Dict], base_env: Dict[str, str], seconds: int, every: float = 10.0,
                   stop=None) -> Optional[Dict]:
    """Poll the sign-in of `brains` until one is signed in (its auth rest is cleared and it is returned), the time
    is up, or `stop()` says so."""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(every)
        if stop and stop():
            return None
        for b in brains:
            if auth_status(b["cli"], b["account"], base_env).get("logged_in"):
                clear_limits(quota_group(b).split("#")[0] + "#")   # every model family of that account
                return b
    return None


def chain_for(cli: str, model: str = "", fallback: bool = True, installed=None) -> List[Dict]:
    """The default chain for a job given its first CLI: that CLI's models x accounts, then the fallback CLIs'."""
    cfg = config()
    off = set(cfg.get("disabled") or [])
    clis = [cli] + ([c for c in cfg["fallback"].get(cli, []) if c != cli and c not in off] if fallback else [])
    out: List[Dict] = []
    for i, c in enumerate(clis):
        if installed and not installed(c):
            continue
        models = [model] if (i == 0 and model) else (cfg["models"].get(c) or [""])
        accts = accounts(c)
        pairs = ([(m, a) for m in models for a in accts] if cfg.get("order") != "account-first"
                 else [(m, a) for a in accts for m in models])
        out += [{"cli": c, "model": m, "account": a} for m, a in pairs]
    return out


def tier_chain(difficulty: str, installed=None) -> List[Dict]:
    """The brains of a difficulty tier, each spread over that CLI's accounts, minus disabled / missing CLIs."""
    cfg = config()
    tiers = cfg.get("tiers") or {}
    if difficulty not in tiers:
        raise ValueError("difficulty must be one of %s" % ", ".join(tiers))
    off = set(cfg.get("disabled") or [])
    out: List[Dict] = []
    for spec in tiers[difficulty]:
        b = parse(spec)
        if b["cli"] in off or (installed and not installed(b["cli"])):
            continue
        accts = [b["account"]] if "@" in spec else accounts(b["cli"])
        out += [{"cli": b["cli"], "model": b["model"], "account": a} for a in accts]
    return out


def _load_limits() -> Dict[str, Dict]:
    try:
        raw = json.loads(LIMITS.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_limits(state: Dict[str, Dict]) -> None:
    try:
        LIMITS.parent.mkdir(parents=True, exist_ok=True)
        tmp = LIMITS.with_suffix(".tmp-%d" % os.getpid())
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, LIMITS)
    except OSError:
        pass


def rest_for(text: str) -> int:
    m = RESET_IN_RE.search(text or "")
    if m and any(m.groups()):
        dd, h, mi, s = (int(x or 0) for x in m.groups())
        return max(60, min(MAX_REST, dd * 86400 + h * 3600 + mi * 60 + s))
    return UNKNOWN_REST


def rest(b: Dict, kind: str, text: str, seconds: Optional[int] = None) -> float:
    """Remember that this brain's quota group cannot work until its reset. Returns the time it may work again."""
    now = time.time()
    until = now + (seconds if seconds is not None else rest_for(text))
    state = {k: v for k, v in _load_limits().items() if v.get("until", 0) > now}
    state[quota_group(b)] = {"until": until, "kind": kind, "why": (text or "").strip()[-300:], "brain": label(b)}
    _save_limits(state)
    return until


def resting(b: Dict) -> Optional[Dict]:
    v = _load_limits().get(quota_group(b))
    return v if v and v.get("until", 0) > time.time() else None


def limits() -> List[Dict]:
    now = time.time()
    return [{"group": k, "until": time.strftime("%m-%d %H:%M", time.localtime(v["until"])), "kind": v.get("kind"),
             "brain": v.get("brain"), "why": v.get("why", "")[-160:]}
            for k, v in sorted(_load_limits().items()) if v.get("until", 0) > now]


def clear_limits(group: str = "") -> int:
    state = _load_limits()
    keep = {k: v for k, v in state.items() if group and group not in k}
    _save_limits(keep)
    return len(state) - len(keep)


def classify(code: Optional[int], text: str, made_changes: bool) -> str:
    """ok | blocked | auth | unavailable | failed. Only the tail of the output is read, and a run that exited 0 counts
    as a quota hit only when it also produced nothing (a worker's own output may mention "rate limit")."""
    tail = (text or "")[-3000:]
    if not made_changes and BLOCKED_RE.search(tail):
        return "blocked"
    if code == 0 and (made_changes or not (AUTH_RE.search(tail) or UNAVAILABLE_RE.search(tail))):
        return "ok"
    if AUTH_RE.search(tail):
        return "auth"
    if UNAVAILABLE_RE.search(tail) or code is None:
        return "unavailable"
    return "failed"
