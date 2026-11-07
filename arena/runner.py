"""Runs a schedule of games and writes one directory per game:

    <out>/<run_id>/<game_id>/meta.json        seats, models, seed, versions, cost, status
                              events.jsonl     the event log with full state snapshots
                              decisions.jsonl  every accepted decision
                              calls.jsonl      every model call, including rejected and failed attempts
                              result.json      outcome (finished games only)

Finished games are skipped on a rerun, so an interrupted run can be resumed with the same command.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import itertools
import json
import os
import threading
import traceback

from .backends import cli_version
from .budget import Budget, BudgetExhausted
from .engine import GameAborted
from .games import GAMES
from .players import LLMPlayer, make_asker, set_concurrency, template_hash

SEAT_NAMES = ["Ash", "Birch", "Cedar"]


def seed_for(run_id, title, i):
    return int(hashlib.sha256(f"{run_id}:{title}:{i}".encode()).hexdigest()[:12], 16)


def schedule(cfg) -> list[dict]:
    """Interleaved games. Seat orders cycle through all permutations of the models; in Spyfall the spy cycles
    through the models."""
    labels = list(cfg["models"])
    perms = list(itertools.permutations(labels))
    per = {t: n for t, n in cfg["games"].items()}
    out = []
    for i in range(max(per.values())):
        for title, n in per.items():
            if i >= n:
                continue
            order = perms[i % len(perms)]
            seats = {SEAT_NAMES[k]: order[k] for k in range(len(order))}
            g = {"game_id": f"{title}-{i + 1:02d}", "title": title, "index": i, "seed": seed_for(cfg["run_id"], title, i),
                 "seats": seats}
            if title == "spyfall":
                spy_model = labels[i % len(labels)]
                g["spy"] = next(s for s, m in seats.items() if m == spy_model)
            out.append(g)
    return out


def build_game(spec):
    cls = GAMES[spec["title"]]
    seats = list(spec["seats"])
    game = cls(spec["seed"], seats, spec["spy"]) if spec["title"] == "spyfall" else cls(spec["seed"], seats)
    game.game_id = spec["game_id"]
    return game


def code_revision():
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=here, capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=here, capture_output=True,
                               text=True, timeout=10).stdout.strip()
        return rev + ("-dirty" if dirty else "") if rev else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _dump_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class Runner:
    def __init__(self, cfg, out_root):
        self.cfg = cfg
        self.dir = os.path.join(out_root, cfg["run_id"])
        os.makedirs(self.dir, exist_ok=True)
        self.budget = Budget(os.path.join(self.dir, "budget.json"), cfg.get("budget_usd", {}),
                             cfg.get("prices", {}))
        set_concurrency(cfg.get("concurrency", {}))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.consecutive_aborts = 0

    def status(self, gid):
        p = os.path.join(self.dir, gid, "meta.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f).get("status")

    def run_game(self, spec):
        gid = spec["game_id"]
        gdir = os.path.join(self.dir, gid)
        if os.path.exists(gdir):  # an earlier aborted attempt: keep its logs (and its cost record)
            n = 1
            while os.path.exists(f"{gdir}.attempt{n}"):
                n += 1
            os.rename(gdir, f"{gdir}.attempt{n}")
        os.makedirs(gdir)
        game = build_game(spec)
        calls_path = os.path.join(gdir, "calls.jsonl")
        open(calls_path, "w").close()
        call_lock = threading.Lock()

        def log(rec):
            rec = {"ts": _now(), **rec}
            with call_lock, open(calls_path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        models = self.cfg["models"]
        players = {seat: LLMPlayer(label, models[label]["backend"], models[label]["model"],
                                   models[label].get("effort", "low"), self.budget, log,
                                   self.cfg.get("timeout", 180))
                   for seat, label in spec["seats"].items()}
        game.asker = make_asker(game, players)
        meta = {**spec, "status": "running", "started": _now(), "rules_version": game.rules_version,
                "template_hash": template_hash(game.rules),
                "models": {seat: {"label": label, **models[label]} for seat, label in spec["seats"].items()},
                "cli_versions": {b: cli_version(b) for b in ("claude_cli", "codex_cli")},
                "code_revision": code_revision()}
        self._write_meta(gdir, meta)
        try:
            result = game.play()
            meta["status"] = "finished"
        except BudgetExhausted as e:
            result, meta["status"], meta["error"] = None, "aborted_budget", str(e)
            self.stop.set()
        except GameAborted as e:
            result, meta["status"], meta["error"] = None, "aborted", str(e)
        except Exception as e:  # engine bug: keep the logs, stop the run
            result, meta["status"], meta["error"] = None, "crashed", traceback.format_exc()
            self.stop.set()
        meta["ended"] = _now()
        _dump_jsonl(os.path.join(gdir, "events.jsonl"), game.events)
        _dump_jsonl(os.path.join(gdir, "decisions.jsonl"), game.decisions)
        meta.update(self._call_totals(calls_path))
        if result is not None:
            meta["winners"] = result["winners"]
            meta["winner_models"] = [spec["seats"][w] for w in result["winners"]]
            with open(os.path.join(gdir, "result.json"), "w") as f:
                json.dump(result, f, indent=1)
        self._write_meta(gdir, meta)
        with self.lock:
            self.consecutive_aborts = 0 if meta["status"] == "finished" else self.consecutive_aborts + 1
            if self.consecutive_aborts >= self.cfg.get("max_consecutive_aborts", 3):
                self.stop.set()
        spent = {b: round(self.budget.spent(b), 4) for b in self.budget.caps}
        print(f"[{_now()}] {gid}: {meta['status']} winners={meta.get('winner_models')} "
              f"calls={meta['calls']} spent={spent}" + (f" error={meta.get('error', '')[:200]}" if
                                                       meta["status"] != "finished" else ""), flush=True)
        return meta

    @staticmethod
    def _call_totals(calls_path):
        n = failed = 0
        cost = {}
        with open(calls_path) as f:
            for line in f:
                r = json.loads(line)
                n += 1
                failed += "error" in r
                if r.get("cost_usd") is not None:
                    cost[r["model_label"]] = cost.get(r["model_label"], 0.0) + r["cost_usd"]
        return {"calls": n, "calls_rejected_or_failed": failed, "api_cost_usd": {k: round(v, 5) for k, v in cost.items()}}

    @staticmethod
    def _write_meta(gdir, meta):
        with open(os.path.join(gdir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=1)

    def run(self, only=None, workers=3):
        specs = [s for s in schedule(self.cfg) if (not only or s["game_id"] in only)
                 and self.status(s["game_id"]) != "finished"]
        print(f"{len(specs)} game(s) to play in {self.dir}", flush=True)
        with cf.ThreadPoolExecutor(workers) as ex:
            pending = list(specs)
            running = set()
            while pending or running:
                while pending and len(running) < workers and not self.stop.is_set():
                    running.add(ex.submit(self.run_game, pending.pop(0)))
                if not running:
                    break
                done, running = cf.wait(running, return_when=cf.FIRST_COMPLETED)
                running = set(running)
        if self.stop.is_set():
            print("run stopped early (budget cap, repeated aborts, or an engine crash)", flush=True)


def smoke(cfg, out_root):
    """One tiny structured call per model, through the same budget as the run."""
    from . import backends
    d = os.path.join(out_root, cfg["run_id"])
    os.makedirs(d, exist_ok=True)
    budget = Budget(os.path.join(d, "budget.json"), cfg.get("budget_usd", {}), cfg.get("prices", {}))
    schema = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
              "required": ["answer"], "additionalProperties": False}
    for label, m in cfg["models"].items():
        held = budget.reserve(m["backend"], m["model"], 200)
        try:
            r = backends.call(m["backend"], m["model"], m.get("effort", "low"), "Answer with JSON.",
                              "Is 7 a prime number?", schema, cfg.get("timeout", 180))
        except Exception as e:
            budget.release(m["backend"], held)
            print(f"{label}: FAILED {e}")
            continue
        cost = budget.record(m["backend"], m["model"], r, held)
        print(f"{label}: {r.obj} in={r.tokens_in} out={r.tokens_out} reasoning={r.tokens_reasoning} "
              f"{r.latency_ms} ms" + (f" ${cost:.5f}" if cost is not None else ""))
