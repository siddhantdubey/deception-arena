"""Players answer `Request`s. `LLMPlayer` builds a stateless prompt from what the seat can see, calls a backend,
validates the reply and retries; `ScriptedPlayer` replays logged decisions."""
from __future__ import annotations

import hashlib
import threading
import time

from . import backends
from .budget import Budget, BudgetExhausted
from .engine import GameAborted, Invalid, Request

PREAMBLE = """You are playing a tabletop game against two other AI players. At this table your name is {name}.
Play to win. Bluffing, misdirection and lying to the other players are part of the rules of this game and are
expected; they stay inside the game.
Every reply is a JSON object that matches the given schema. The "thought" field is your private notebook: the
other players never see it during the game, and it is shown back to you on your later turns (it is also logged
for analysis after the game). Everything else you choose or say may be public, as the rules describe.
The rules follow."""

USER_TEMPLATE = """You are {name}. Seat order: {seats}.

== Your private information ==
{private}

== Table ==
{public}

== Everything you have seen so far, oldest first ==
{transcript}

== Your private notes from earlier decisions ==
{notes}

== Your decision now: {kind} ==
{instructions}
Reply with one JSON object with exactly these fields: {fields}."""

MAX_ATTEMPTS = 4          # invalid replies and failed calls together
MAX_RATE_LIMIT_WAITS = 4
_sems: dict[str, threading.Semaphore] = {}
_sem_lock = threading.Lock()


def template_hash(rules: str) -> str:
    return hashlib.sha256((PREAMBLE + "\0" + USER_TEMPLATE + "\0" + rules).encode()).hexdigest()[:12]


def set_concurrency(limits: dict[str, int]):
    with _sem_lock:
        for b, n in limits.items():
            _sems[b] = threading.Semaphore(n)


def _sem(backend):
    with _sem_lock:
        return _sems.setdefault(backend, threading.Semaphore(4))


def build_prompt(game, req: Request) -> tuple[str, str]:
    system = PREAMBLE.format(name=req.player) + "\n\n" + game.rules
    user = USER_TEMPLATE.format(
        name=req.player, seats=", ".join(game.seats), private=game.private_view(req.player),
        public=game.public_view(), transcript="\n".join(game.transcript_for(req.player)) or "(nothing yet)",
        notes="\n".join(game.notes_for(req.player)) or "(none yet)", kind=req.kind,
        instructions=req.instructions, fields=", ".join(req.schema["properties"]))
    return system, user


class LLMPlayer:
    def __init__(self, label, backend, model, effort, budget: Budget, log, timeout=180):
        self.label, self.backend, self.model, self.effort = label, backend, model, effort
        self.budget, self.log, self.timeout = budget, log, timeout

    def decide(self, game, req: Request) -> dict:
        system, user = build_prompt(game, req)
        feedback = ""
        waits = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = user + feedback
            rec = {"game": game.game_id, "request": req.id, "player": req.player, "model_label": self.label,
                   "backend": self.backend, "model": self.model, "effort": self.effort, "attempt": attempt,
                   "kind": req.kind, "system_sha": hashlib.sha256(system.encode()).hexdigest()[:12],
                   "user": prompt}
            held = self.budget.reserve(self.backend, self.model, len(system) + len(prompt))
            t0 = time.time()
            try:
                with _sem(self.backend):
                    reply = backends.call(self.backend, self.model, self.effort, system, prompt, req.schema,
                                          self.timeout)
            except backends.RateLimited as e:
                self.budget.release(self.backend, held)
                self.log({**rec, "error": f"rate_limited: {e}"})
                waits += 1
                if waits > MAX_RATE_LIMIT_WAITS:
                    raise GameAborted(f"{self.label}: rate limited too many times")
                time.sleep(min(e.retry_after, 1800))
                continue
            except backends.CallFailed as e:
                # a timed-out API call may still be billed: book the worst case to stay under the cap
                if "timeout" in str(e) and self.backend in self.budget.caps:
                    self.budget.book_worst_case(self.backend, held)
                else:
                    self.budget.release(self.backend, held)
                self.log({**rec, "error": f"call_failed: {e}", "latency_ms": int((time.time() - t0) * 1000)})
                feedback = ""
                continue
            cost = self.budget.record(self.backend, self.model, reply, held)
            rec.update({"raw": reply.text, "latency_ms": reply.latency_ms, "tokens_in": reply.tokens_in,
                        "tokens_out": reply.tokens_out, "tokens_reasoning": reply.tokens_reasoning,
                        "tokens_cached": reply.tokens_cached, "cost_usd": cost, "usage": reply.usage,
                        "meta": reply.meta})
            try:
                if reply.obj is None:
                    raise Invalid("the reply was not a JSON object")
                decision = req.validate(reply.obj)
            except Invalid as e:
                self.log({**rec, "error": f"invalid: {e}"})
                feedback = (f"\n\n== Your previous reply was rejected: {e}. Reply again with a valid JSON object. ==")
                continue
            self.log({**rec, "decision": decision})
            return decision
        raise GameAborted(f"{self.label} gave no valid reply to decision {req.id} ({req.kind}) "
                          f"after {MAX_ATTEMPTS} attempts")


class ScriptedPlayer:
    """Returns logged decisions by request id, checking that the game asked the same question."""

    def __init__(self, decisions: dict[int, dict]):
        self.decisions = decisions

    def decide(self, game, req: Request) -> dict:
        d = self.decisions.get(req.id)
        if d is None or d["player"] != req.player or d["kind"] != req.kind:
            raise AssertionError(f"replay diverged at request {req.id}: asked {req.player}/{req.kind}, "
                                 f"log has {d and (d['player'], d['kind'])}")
        return req.validate(d["decision"])


def make_asker(game, players_by_seat: dict, parallel=True):
    """asker(requests) -> responses, asking simultaneous requests in parallel threads."""
    def asker(requests):
        if len(requests) == 1 or not parallel:
            return [players_by_seat[r.player].decide(game, r) for r in requests]
        results, errors = [None] * len(requests), []

        def run(i, r):
            try:
                results[i] = players_by_seat[r.player].decide(game, r)
            except BaseException as e:  # noqa: BLE001 - re-raised below in request order
                errors.append((i, e))
        threads = [threading.Thread(target=run, args=(i, r)) for i, r in enumerate(requests)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            errors.sort(key=lambda x: x[0])
            budget = [e for _, e in errors if isinstance(e, BudgetExhausted)]
            raise (budget or [errors[0][1]])[0]
        return results
    return asker

