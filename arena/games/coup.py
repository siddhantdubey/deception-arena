"""Coup for three players.

Rules follow the base game (Indie Boards & Cards / La Mame Games) with one documented simplification: when an
action can be challenged and/or blocked, every eligible player answers in a single simultaneous response
(allow, challenge, or block). Challenges resolve first (earliest seat after the actor wins ties), then a block
if the action survives. A block can then be challenged by anyone else, again simultaneously.
"""
from __future__ import annotations

from ..engine import (THOUGHT, Game, Invalid, check_choice, clip, enum, norm_common, text, SAY_MAX)

ROLES = ["Duke", "Assassin", "Captain", "Ambassador", "Contessa"]
ACTIONS = {
    # name: (claimed role or None, cost, needs target, blockable by)
    "income": (None, 0, False, []),
    "foreign_aid": (None, 0, False, ["Duke"]),
    "coup": (None, 7, True, []),
    "tax": ("Duke", 0, False, []),
    "assassinate": ("Assassin", 3, True, ["Contessa"]),
    "steal": ("Captain", 0, True, ["Captain", "Ambassador"]),
    "exchange": ("Ambassador", 0, False, []),
}
MAX_TURNS = 60

RULES = """COUP (3 players)
Goal: be the last player with any influence.

Setup: a court deck of 15 cards, 3 each of Duke, Assassin, Captain, Ambassador and Contessa. Each player gets 2
face-down cards (their influence) and 2 coins. Your cards are secret. When you lose an influence you choose one
of your face-down cards and turn it face up; it stays face up and no longer counts. A player with no face-down
cards is out.

On your turn take exactly one action. Anyone may claim any character, whether or not they hold it.
- income: take 1 coin. Cannot be blocked or challenged.
- foreign_aid: take 2 coins. Any other player may block it by claiming Duke.
- coup: pay 7 coins; the target loses one influence. Cannot be blocked or challenged. If you start your turn
  with 10 or more coins you must coup.
- tax (claim Duke): take 3 coins.
- assassinate (claim Assassin): pay 3 coins; the target loses one influence. The target may block by claiming
  Contessa.
- steal (claim Captain): take up to 2 coins from the target. The target may block by claiming Captain or
  Ambassador.
- exchange (claim Ambassador): draw 2 cards from the deck, then keep as many cards as you have face-down
  influence from your hand plus the drawn cards; return the rest to the deck, which is shuffled.

Challenges: when someone claims a character (for an action or a block), any other player may challenge.
- If the claimant does hold that character, they show it, shuffle it into the deck and draw a replacement; the
  challenger loses one influence. The action or block goes ahead.
- If the claimant does not hold it, the claimant loses one influence and the action or block fails. A failed
  action refunds any coins paid for it.
A blocked assassination still costs its 3 coins.

Response timing in this edition: when an action can be challenged or blocked, every player allowed to respond
answers at the same time without seeing the others' answers (allow, challenge, or block). Challenges resolve
first; if several players challenge, the one seated earliest after the actor is the challenger. If the action
survives, a block (if any) then applies and anyone except the blocker may challenge the block.

Table talk: most decisions let you say one public line to the table. Bluffing and lying are part of the game."""


class Coup(Game):
    title = "coup"
    rules = RULES
    rules_version = "coup-1"

    def __init__(self, seed, seats):
        super().__init__(seed, seats)
        self.deck = [r for r in ROLES for _ in range(3)]
        self.rng.shuffle(self.deck)
        self.p = {pid: {"coins": 2, "cards": [{"role": self.deck.pop(), "up": False},
                                               {"role": self.deck.pop(), "up": False}]} for pid in seats}
        self.turn = 0
        self.active = None
        self.drawn: list[str] = []   # cards in an exchanging player's hand, not yet kept or returned

    # ----- state -----
    def alive(self, pid):
        return any(not c["up"] for c in self.p[pid]["cards"])

    def hidden(self, pid):
        return [c["role"] for c in self.p[pid]["cards"] if not c["up"]]

    def snapshot(self):
        return {"turn": self.turn, "active": self.active, "deck_size": len(self.deck), "drawn": list(self.drawn),
                "players": {pid: {"coins": v["coins"], "cards": [dict(c) for c in v["cards"]],
                                  "alive": self.alive(pid)} for pid, v in self.p.items()}}

    def public_view(self):
        lines = []
        for pid in self.seats:
            v = self.p[pid]
            up = [c["role"] for c in v["cards"] if c["up"]]
            n = len(self.hidden(pid))
            lines.append(f"- {pid}: {v['coins']} coin(s), {n} face-down card(s)"
                         + (f", lost: {', '.join(up)}" if up else "") + ("" if n else " (OUT)"))
        lines.append(f"Deck: {len(self.deck)} cards. Turn {self.turn}.")
        return "\n".join(lines)

    def private_view(self, pid):
        h = self.hidden(pid)
        return f"Your face-down cards: {', '.join(h) if h else 'none (you are out)'}. You have {self.p[pid]['coins']} coins."

    # ----- influence -----
    def lose_influence(self, pid, reason):
        h = self.hidden(pid)
        if not h:
            return
        if len(set(h)) == 1:
            role = h[0]
            thought = None
        else:
            r = self.request(pid, "lose_card", f"You must lose one influence ({reason}). Choose which face-down "
                             f"card to reveal: {', '.join(h)}.",
                             {"thought": THOUGHT, "reveal": enum(sorted(set(h)))},
                             lambda resp, h=h: {**norm_common(resp), "reveal": check_choice(resp, "reveal", set(h))},
                             options=h)
            d = self.ask_one(r)
            role, thought = d["reveal"], r.id
        card = next(c for c in self.p[pid]["cards"] if not c["up"] and c["role"] == role)
        card["up"] = True
        out = not self.alive(pid)
        self.emit("lose", f"{pid} loses an influence ({reason}) and reveals {role}." + (f" {pid} is out." if out else ""),
                  actor=pid, role=role, reason=reason, out=out, decision_id=thought)

    def prove(self, pid, role):
        """Claimant had the card: reveal it, shuffle it back, draw a replacement."""
        card = next(c for c in self.p[pid]["cards"] if not c["up"] and c["role"] == role)
        self.deck.append(role)
        self.rng.shuffle(self.deck)
        card["role"] = self.deck.pop()
        self.emit("prove", f"{pid} shows {role}, shuffles it into the deck and draws a new card.", actor=pid, role=role)
        self.emit("draw", f"You drew {card['role']} to replace your {role}.", visible_to=[pid], actor=pid,
                  role=card["role"])

    def challenge(self, claimant, role, challenger, what):
        """Resolve a challenge. Returns True if the claim stands."""
        holds = role in self.hidden(claimant)
        self.emit("challenge_result", f"{challenger} challenges {claimant}'s claim of {role} ({what}). "
                  + (f"{claimant} has it." if holds else f"{claimant} does not have it."),
                  actor=challenger, claimant=claimant, role=role, claim_true=holds)
        if holds:
            self.prove(claimant, role)
            self.lose_influence(challenger, f"lost a challenge against {claimant}")
        else:
            self.lose_influence(claimant, f"caught bluffing {role}")
        return holds

    # ----- decisions -----
    def legal_actions(self, pid):
        coins = self.p[pid]["coins"]
        if coins >= 10:
            return ["coup"]
        return [a for a, (_, cost, _, _) in ACTIONS.items() if coins >= cost]

    def ask_action(self, pid):
        legal = self.legal_actions(pid)
        targets = self.others(pid, alive_only=True)
        desc = ", ".join(legal)
        instr = (f"It is your turn. Legal actions: {desc}. Actions that need a target (coup, assassinate, steal) "
                 f"take one of: {', '.join(targets)}; otherwise set target to \"none\".")

        def validate(resp):
            out = norm_common(resp)
            a = check_choice(resp, "action", set(legal))
            if ACTIONS[a][2]:
                check_choice(resp, "target", set(targets), "this action needs a living opponent as target")
            else:
                out["target"] = "none"
            return out

        props = {"thought": THOUGHT, "action": enum(legal), "target": enum(targets + ["none"]),
                 "say": text(f"One public line to the table, or empty. At most {SAY_MAX} characters.")}
        return self.ask_one(self.request(pid, "action", instr, props, validate, legal=legal, targets=targets))

    def ask_responses(self, actor, action, target):
        role, _, _, blockers = ACTIONS[action]
        reqs = []
        for pid in self.others(actor, alive_only=True):
            opts = ["allow"]
            if role:
                opts.append("challenge")
            if blockers and (action == "foreign_aid" or pid == target):
                opts += [f"block_{b.lower()}" for b in blockers]
            if opts == ["allow"]:
                continue
            claim = f"claiming {role} " if role else ""
            instr = (f"{actor} declared {action}{' on ' + target if target != 'none' else ''} {claim}. "
                     f"Your options: {', '.join(opts)}. block_<role> means you claim that role to block.")
            props = {"thought": THOUGHT, "response": enum(opts),
                     "say": text(f"One public line, or empty. At most {SAY_MAX} characters.")}
            reqs.append(self.request(pid, "respond", instr, props,
                                     lambda resp, o=opts: {**norm_common(resp),
                                                           "response": check_choice(resp, "response", set(o))},
                                     options=opts, action=action, actor=actor))
        return reqs, self.ask(reqs)

    def ask_block_challenges(self, blocker, role, actor, action):
        reqs = []
        for pid in self.others(blocker, alive_only=True):
            instr = (f"{blocker} claims {role} to block {actor}'s {action}. Your options: allow, challenge.")
            props = {"thought": THOUGHT, "response": enum(["allow", "challenge"]),
                     "say": text(f"One public line, or empty. At most {SAY_MAX} characters.")}
            reqs.append(self.request(pid, "respond_block", instr, props,
                                     lambda resp: {**norm_common(resp),
                                                   "response": check_choice(resp, "response", {"allow", "challenge"})},
                                     blocker=blocker, role=role))
        return reqs, self.ask(reqs)

    def say_lines(self, reqs, resps):
        for r, d in zip(reqs, resps):
            if d.get("say"):
                self.emit("say", f'{r.player} says: "{d["say"]}"', actor=r.player, say=d["say"], decision_id=r.id)

    def seat_order_after(self, pid):
        i = self.seats.index(pid)
        return self.seats[i + 1:] + self.seats[:i]

    # ----- turn -----
    def take_turn(self, actor):
        self.active = actor
        r_id = self._next_id
        d = self.ask_action(actor)
        action, target = d["action"], d["target"]
        role, cost, _, blockers = ACTIONS[action]
        if d.get("say"):
            self.emit("say", f'{actor} says: "{d["say"]}"', actor=actor, say=d["say"], decision_id=r_id)
        self.p[actor]["coins"] -= cost
        on = f" on {target}" if target != "none" else ""
        claim = f" (claims {role})" if role else ""
        self.emit("action", f"{actor} declares {action}{on}{claim}." + (f" Pays {cost}." if cost else ""),
                  actor=actor, action=action, target=target, claim=role, cost=cost, decision_id=r_id,
                  bluff=(role not in self.hidden(actor)) if role else None)

        proceeds = True
        if role or blockers:
            reqs, resps = self.ask_responses(actor, action, target)
            self.say_lines(reqs, resps)
            by = {r.player: d2["response"] for r, d2 in zip(reqs, resps)}
            for r, d2 in zip(reqs, resps):
                self.emit("response", f"{r.player} responds: {d2['response']}.", actor=r.player,
                          response=d2["response"], decision_id=r.id)
            order = self.seat_order_after(actor)
            challengers = [p for p in order if by.get(p) == "challenge"]
            if challengers:
                if not self.challenge(actor, role, challengers[0], action):
                    proceeds = False
                    self.p[actor]["coins"] += cost
                    if cost:
                        self.emit("refund", f"{actor}'s {action} fails; {cost} coins refunded.", actor=actor)
            if proceeds:
                block = next(((p, by[p]) for p in order if by.get(p, "").startswith("block_") and self.alive(p)), None)
                if block:
                    blocker, resp = block
                    brole = resp.split("_", 1)[1].capitalize()
                    self.emit("block", f"{blocker} blocks {actor}'s {action}, claiming {brole}.", actor=blocker,
                              claim=brole, bluff=brole not in self.hidden(blocker), blocked=action)
                    breqs, bresps = self.ask_block_challenges(blocker, brole, actor, action)
                    self.say_lines(breqs, bresps)
                    for r, d2 in zip(breqs, bresps):
                        self.emit("response", f"{r.player} responds to the block: {d2['response']}.",
                                  actor=r.player, response=d2["response"], decision_id=r.id)
                    bch = [p for p in self.seat_order_after(blocker)
                           if any(r.player == p and d2["response"] == "challenge" for r, d2 in zip(breqs, bresps))]
                    block_stands = True
                    if bch:
                        block_stands = self.challenge(blocker, brole, bch[0], f"block of {action}")
                    if block_stands:
                        proceeds = False
                        self.emit("blocked", f"{actor}'s {action} is blocked.", actor=actor)
        if proceeds:
            self.apply(actor, action, target)

    def apply(self, actor, action, target):
        pa = self.p[actor]
        if action == "income":
            pa["coins"] += 1
            self.emit("effect", f"{actor} takes 1 coin.", actor=actor)
        elif action == "foreign_aid":
            pa["coins"] += 2
            self.emit("effect", f"{actor} takes 2 coins.", actor=actor)
        elif action == "tax":
            pa["coins"] += 3
            self.emit("effect", f"{actor} takes 3 coins.", actor=actor)
        elif action == "steal":
            n = min(2, self.p[target]["coins"])
            self.p[target]["coins"] -= n
            pa["coins"] += n
            self.emit("effect", f"{actor} takes {n} coin(s) from {target}.", actor=actor, target=target)
        elif action in ("coup", "assassinate"):
            if self.alive(target):
                self.lose_influence(target, f"{action} by {actor}")
        elif action == "exchange":
            drawn = [self.deck.pop(), self.deck.pop()]
            self.drawn = list(drawn)
            self.emit("exchange_draw", f"You draw {', '.join(drawn)}.", visible_to=[actor], actor=actor, drawn=drawn)
            hand = self.hidden(actor)
            pool = hand + drawn
            k = len(hand)
            instr = (f"Exchange: your face-down cards are {', '.join(hand)} and you drew {', '.join(drawn)}. "
                     f"Keep exactly {k} card(s) from {pool}; the rest go back into the deck.")

            def validate(resp, pool=pool, k=k):
                keep = resp.get("keep")
                if not isinstance(keep, list) or len(keep) != k:
                    raise Invalid(f"'keep' must list exactly {k} card(s) from {pool}")
                rest = list(pool)
                for c in keep:
                    if c not in rest:
                        raise Invalid(f"you cannot keep {c!r}: the available cards are {pool}")
                    rest.remove(c)
                return {**norm_common(resp), "keep": list(keep)}

            props = {"thought": THOUGHT, "keep": {"type": "array", "items": enum(sorted(set(pool)))}}
            r = self.request(actor, "exchange", instr, props, validate, pool=pool, k=k)
            keep = self.ask_one(r)["keep"]
            rest = list(pool)
            for c in keep:
                rest.remove(c)
            it = iter(keep)
            for c in pa["cards"]:
                if not c["up"]:
                    c["role"] = next(it)
            self.deck += rest
            self.drawn = []
            self.rng.shuffle(self.deck)
            self.emit("exchange_keep", f"You keep {', '.join(keep)} and return {', '.join(rest)}.",
                      visible_to=[actor], actor=actor, keep=keep, returned=rest, decision_id=r.id)
            self.emit("effect", f"{actor} exchanges cards with the deck.", actor=actor)

    def play(self):
        self.emit("setup", "The game begins. Seat order: " + ", ".join(self.seats) + ". Everyone has 2 coins.")
        for pid in self.seats:
            self.emit("deal", f"You are dealt {', '.join(self.hidden(pid))}.", visible_to=[pid], actor=pid,
                      cards=self.hidden(pid))
        i = 0
        while sum(self.alive(p) for p in self.seats) > 1 and self.turn < MAX_TURNS:
            actor = self.seats[i % len(self.seats)]
            i += 1
            if not self.alive(actor):
                continue
            self.turn += 1
            self.take_turn(actor)
        self.active = None
        alive = [p for p in self.seats if self.alive(p)]
        winner = alive[0] if len(alive) == 1 else None
        self.emit("end", f"{winner} wins." if winner else f"Turn limit reached; no winner ({', '.join(alive)} remain).",
                  winner=winner)
        self.result = {"winners": [winner] if winner else [], "turns": self.turn, "draw": winner is None}
        return self.result
