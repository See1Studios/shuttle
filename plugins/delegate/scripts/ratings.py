"""Ratings: how well each brain (cli:model) does at each difficulty, and routing that learns from it.

Every finished piece of delegated work leaves events in $DELEGATE_HOME/ratings.jsonl:

  passed        the brain made the job's acceptance check pass (value 1.0, minus 0.15 per automatic fix round)
  escalated     it could not, and the job moved on to a stronger brain (0.0)
  failed_check  it could not and nobody stronger was left (0.0)
  failed        the worker exited with an error (0.0)
  accepted      the director landed its work (1.0)
  rework        the director sent it back with feedback (0.4)
  rejected      the director discarded its finished work (0.0)
  blocked       its permission mode stopped it: a setup problem, not skill -- not scored

score(brain, difficulty) = (sum of values + 1) / (events + 2), a Laplace-smoothed success rate in 0..1.

Routing keeps the tier's cheapest-first order but skips a brain whose score at that difficulty is below
`ratings.min_score` once it has `ratings.min_events` events; with probability `ratings.explore` a skipped brain is
tried anyway so its record can recover. The last brain of a chain is never skipped.
"""
from __future__ import annotations

import json
import random
import time
from collections import defaultdict
from typing import Dict, List, Optional

import brains as B

FILE = B.HOME / "ratings.jsonl"
VALUES = {"passed": 1.0, "accepted": 1.0, "rework": 0.4, "escalated": 0.0, "failed_check": 0.0, "failed": 0.0,
          "rejected": 0.0}
DEFAULTS = {"min_events": 4, "min_score": 0.35, "explore": 0.15}


def settings() -> Dict:
    out = dict(DEFAULTS)
    out.update(B.config().get("ratings") or {})
    return out


def key(b: Dict) -> str:
    return "%s:%s" % (b["cli"], b.get("model") or "default")


def record(brain: str, difficulty: str, event: str, job: str = "", **extra) -> None:
    """Append one event. `brain` is 'cli:model'. A write failure only loses the event, never the job."""
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "brain": brain, "difficulty": difficulty or "unrated",
           "event": event, "job": job}
    row.update({k: v for k, v in extra.items() if v not in (None, "")})
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _rows() -> List[Dict]:
    try:
        lines = FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def table() -> List[Dict]:
    """Per brain and difficulty: events, score, counts per event, mean worker seconds."""
    agg: Dict[tuple, Dict] = defaultdict(lambda: {"sum": 0.0, "n": 0, "counts": defaultdict(int), "sec": []})
    for r in _rows():
        a = agg[(r["brain"], r.get("difficulty", "unrated"))]
        a["counts"][r["event"]] += 1
        if r["event"] in VALUES:
            v = VALUES[r["event"]]
            if r["event"] == "passed":
                v = max(0.3, 1.0 - 0.15 * int(r.get("auto_rounds") or 0))
            a["sum"] += v
            a["n"] += 1
        if r.get("agent_sec"):
            a["sec"].append(float(r["agent_sec"]))
    out = []
    for (brain, diff), a in sorted(agg.items()):
        out.append({"brain": brain, "difficulty": diff, "events": a["n"],
                    "score": round((a["sum"] + 1) / (a["n"] + 2), 2), "counts": dict(a["counts"]),
                    "avg_sec": round(sum(a["sec"]) / len(a["sec"]), 1) if a["sec"] else None})
    return out


def score(brain: str, difficulty: str) -> Optional[Dict]:
    for row in table():
        if row["brain"] == brain and row["difficulty"] == difficulty:
            return row
    return None


def route(chain: List[Dict], difficulty: str) -> (List[Dict], List[str]):
    """The chain with proven-weak brains for this difficulty removed (never the last one). Returns (chain, notes)."""
    cfg = settings()
    keep, notes = [], []
    for i, b in enumerate(chain):
        s = score(key(b), difficulty)
        weak = s and s["events"] >= cfg["min_events"] and s["score"] < cfg["min_score"]
        last = i == len(chain) - 1
        if weak and not last and random.random() >= cfg["explore"]:
            notes.append("skipped %s at %s (score %.2f over %d)" % (key(b), difficulty, s["score"], s["events"]))
            continue
        keep.append(b)
    return keep, notes
