"""Spend accounting with a hard cap per backend.

Before every call `reserve()` checks that spend so far plus a worst-case cost for the call stays under the cap.
After the call `record()` books the actual list-price cost from the reported token usage. Spend is persisted to
a JSON file after every call, so a restarted run keeps counting from where it stopped. Subscription CLI calls
are counted but cost nothing here.
"""
from __future__ import annotations

import json
import os
import threading

from .backends import MAX_OUTPUT_TOKENS, Reply


class BudgetExhausted(Exception):
    pass


def call_cost(prices: dict | None, reply: Reply) -> float | None:
    """List-price USD for one call. Thinking tokens are billed as output. None if no price is known."""
    if not prices:
        return None
    tin = reply.tokens_in or 0
    cached = min(reply.tokens_cached or 0, tin)
    out = (reply.tokens_out or 0) + (reply.tokens_reasoning or 0)
    return ((tin - cached) * prices["input"] + cached * prices.get("cached_input", prices["input"])
            + out * prices["output"]) / 1e6


class Budget:
    def __init__(self, path: str, caps_usd: dict[str, float], prices: dict[str, dict]):
        self.path, self.caps, self.prices = path, caps_usd, prices
        self.lock = threading.Lock()
        self.state = {"usd": {}, "calls": {}, "reserved": {}}
        if os.path.exists(path):
            with open(path) as f:
                self.state.update(json.load(f))
        self.state["reserved"] = {}  # reservations never survive a restart

    def _worst_case(self, model: str, prompt_chars: int) -> float:
        p = self.prices.get(model)
        if not p:
            return 0.0
        # ~3 chars per token is pessimistic for English prompts; output capped by MAX_OUTPUT_TOKENS
        return (prompt_chars / 3 * p["input"] + MAX_OUTPUT_TOKENS * p["output"]) / 1e6

    def reserve(self, backend: str, model: str, prompt_chars: int) -> float:
        cap = self.caps.get(backend)
        if cap is None:
            return 0.0
        if model not in self.prices:
            raise BudgetExhausted(f"no price for {model}; refusing to call a capped backend blind")
        need = self._worst_case(model, prompt_chars)
        with self.lock:
            spent = self.state["usd"].get(backend, 0.0)
            held = self.state["reserved"].get(backend, 0.0)
            if spent + held + need > cap:
                raise BudgetExhausted(f"{backend}: spent ${spent:.4f} + held ${held:.4f} + next call up to "
                                      f"${need:.4f} would exceed the ${cap:.2f} cap")
            self.state["reserved"][backend] = held + need
        return need

    def record(self, backend: str, model: str, reply: Reply | None, held: float) -> float | None:
        cost = call_cost(self.prices.get(model), reply) if reply else None
        with self.lock:
            self.state["reserved"][backend] = max(0.0, self.state["reserved"].get(backend, 0.0) - held)
            self.state["calls"][backend] = self.state["calls"].get(backend, 0) + 1
            if cost is not None and backend in self.caps:
                self.state["usd"][backend] = self.state["usd"].get(backend, 0.0) + cost
            self._save()
        return cost

    def release(self, backend: str, held: float) -> None:
        """A call that failed before any tokens were billed (network error before a response)."""
        with self.lock:
            self.state["reserved"][backend] = max(0.0, self.state["reserved"].get(backend, 0.0) - held)

    def book_worst_case(self, backend: str, held: float) -> None:
        """A capped call that timed out may still be billed: count its full reservation as spent."""
        with self.lock:
            self.state["reserved"][backend] = max(0.0, self.state["reserved"].get(backend, 0.0) - held)
            self.state["usd"][backend] = self.state["usd"].get(backend, 0.0) + held
            self.state["calls"][backend] = self.state["calls"].get(backend, 0) + 1
            self._save()

    def spent(self, backend: str) -> float:
        with self.lock:
            return self.state["usd"].get(backend, 0.0)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: v for k, v in self.state.items() if k != "reserved"}, f, indent=1)
        os.replace(tmp, self.path)
