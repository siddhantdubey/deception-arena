"""Shared game machinery: decision requests, the event log and the base Game class.

A game never talks to a model directly. It builds `Request`s and hands a batch of them to an `asker`
(`asker(requests) -> responses`, same order). The live runner's asker calls the models; the replay asker returns
logged decisions. The engine only uses its own seeded RNG and asks in a fixed order, so replaying the logged
decisions must reproduce the event log exactly.

Every event stores who could see it and a full snapshot of the game state after it. Renderers and analysis
read only these snapshots and never re-simulate.
"""
from __future__ import annotations

import copy
import dataclasses
import random
from typing import Callable

THOUGHT_MAX = 400   # characters kept from a private note
SAY_MAX = 400       # characters kept from anything said aloud; what is kept is what other players see


class Invalid(Exception):
    """A reply that breaks the rules of the decision. The message is shown to the player on retry."""


class GameAborted(Exception):
    pass


@dataclasses.dataclass
class Request:
    id: int
    player: str
    kind: str
    instructions: str               # what to decide, with the legal options spelled out
    schema: dict
    validate: Callable[[dict], dict]  # returns the normalised decision or raises Invalid
    context: dict = dataclasses.field(default_factory=dict)  # extra facts for the log (legal options etc.)


def clip(text, limit):
    text = " ".join(str(text or "").split())
    return (text[:limit - 1] + "…", True) if len(text) > limit else (text, False)


def obj_schema(props: dict) -> dict:
    """Strict object schema: every property required, nothing extra (works for all three structured-output APIs)."""
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def enum(values, desc=""):
    s = {"type": "string", "enum": list(values)}
    if desc:
        s["description"] = desc
    return s


def text(desc):
    return {"type": "string", "description": desc}


THOUGHT = text(f"Private note to yourself: your read of the table and your plan. Hidden from the other players "
               f"during the game. At most {THOUGHT_MAX} characters.")


class Game:
    title = "game"
    rules = ""
    rules_version = "1"

    def __init__(self, seed: int, seats: list[str]):
        self.seed = seed
        self.seats = list(seats)
        self.rng = random.Random(seed)
        self.events: list[dict] = []
        self.decisions: list[dict] = []
        self._next_id = 0
        self.asker: Callable[[list[Request]], list[dict]] | None = None
        self.result: dict | None = None

    # ----- to implement -----
    def snapshot(self) -> dict:
        raise NotImplementedError

    def private_view(self, pid: str) -> str:
        """Everything `pid` privately knows right now, as prompt text."""
        raise NotImplementedError

    def public_view(self) -> str:
        """Public table state as prompt text."""
        raise NotImplementedError

    def play(self) -> dict:
        raise NotImplementedError

    # ----- shared -----
    def others(self, pid, alive_only=False):
        return [p for p in self.seats if p != pid and (not alive_only or self.alive(p))]

    def alive(self, pid) -> bool:
        return True

    def emit(self, kind: str, text: str, visible_to: list[str] | None = None, actor: str | None = None,
             **data) -> dict:
        ev = {"seq": len(self.events), "kind": kind, "text": text, "actor": actor,
              "visible_to": visible_to, "data": data, "state": copy.deepcopy(self.snapshot())}
        self.events.append(ev)
        return ev

    def request(self, player, kind, instructions, props, validate, **context) -> Request:
        r = Request(self._next_id, player, kind, instructions, obj_schema(props), validate, context)
        self._next_id += 1
        return r

    def ask(self, requests: list[Request]) -> list[dict]:
        if not requests:
            return []
        responses = self.asker(requests)
        for r, resp in zip(requests, responses):
            self.decisions.append({"id": r.id, "player": r.player, "kind": r.kind, "context": r.context,
                                   "decision": resp})
        return responses

    def ask_one(self, request: Request) -> dict:
        return self.ask([request])[0]

    def transcript_for(self, pid: str) -> list[str]:
        # numbered per player: global event numbers would reveal how many private events others received
        seen = [e["text"] for e in self.events if e["visible_to"] is None or pid in e["visible_to"]]
        return [f"[{i + 1}] {t}" for i, t in enumerate(seen)]

    def notes_for(self, pid: str) -> list[str]:
        mine = [d for d in self.decisions if d["player"] == pid]
        return [f"({i + 1}. {d['kind']}) {d['decision']['thought']}" for i, d in enumerate(mine)
                if d["decision"].get("thought")]


def check_choice(resp, field, legal, what=None):
    v = resp.get(field)
    if v not in legal:
        raise Invalid(f"'{field}' must be one of {sorted(legal)} ({what or 'illegal choice'}), got {v!r}")
    return v


def norm_common(resp: dict) -> dict:
    """Clip the free-text fields every decision has. Returns a new dict."""
    out = dict(resp)
    out["thought"], _ = clip(resp.get("thought", ""), THOUGHT_MAX)
    if "say" in resp:
        out["say"], cut = clip(resp.get("say", ""), SAY_MAX)
        if cut:
            out["say_clipped"] = True
    return out
