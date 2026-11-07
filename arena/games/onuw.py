"""One Night Ultimate Werewolf for three players (Bezier Games), with the recommended 3-player role set:
Werewolf x2, Seer, Robber, Troublemaker, Villager. Three cards go to the center.

Night actions are chosen simultaneously and resolved in the official wake order (Werewolf, Seer, Robber,
Troublemaker), which gives the same result as waking players one by one because no night choice depends on
another player's night result. The day is three rounds of speeches in seat order, then a simultaneous vote.
"""
from __future__ import annotations

from ..engine import THOUGHT, Game, check_choice, enum, norm_common, text, SAY_MAX

DECK = ["Werewolf", "Werewolf", "Seer", "Robber", "Troublemaker", "Villager"]
ROLES = ["Werewolf", "Seer", "Robber", "Troublemaker", "Villager"]
ROUNDS = 3
CENTER = ["center1", "center2", "center3"]

RULES = """ONE NIGHT ULTIMATE WEREWOLF (3 players)
Cards in play: Werewolf, Werewolf, Seer, Robber, Troublemaker, Villager. Each player gets one card; the other
three are face down in the center (center1, center2, center3). Everyone knows which six cards are in play.

Night (each role acts once, in this order; players act secretly):
1. Werewolf: werewolves learn who the other werewolf player is. A lone werewolf (the only werewolf among the
   players) may look at one center card.
2. Seer: may look at one other player's card, or at two center cards.
3. Robber: may swap their card with another player's card, then looks at their new card.
4. Troublemaker: may swap the cards of the two other players, without looking at them.
Cards can move during the night, so a player's card at the end of the night may differ from the card they were
dealt. Nobody looks at their own card again. You win or lose with the card in front of you at the END of the
night, not the one you were dealt.

Day: three rounds of discussion. In each round every player speaks once, in seat order. With each speech you
also state, publicly, which role you claim you were DEALT ("none" if you make no claim). Then everyone votes
at the same time for one other player.
- The player with the most votes dies. If every player gets exactly one vote, nobody dies.
- Village team (everyone whose final card is not Werewolf) wins if at least one player holding a Werewolf card
  dies. If no player holds a Werewolf card (both werewolves are in the center), the village wins only if nobody
  dies.
- Werewolf team (players whose final card is Werewolf) wins if at least one player holds a Werewolf card and
  none of them dies.

Lying is allowed and expected. Werewolves must hide; villagers may also bluff."""


class OneNightWerewolf(Game):
    title = "onuw"
    rules = RULES
    rules_version = "onuw-1"

    def __init__(self, seed, seats):
        super().__init__(seed, seats)
        deck = list(DECK)
        self.rng.shuffle(deck)
        self.dealt = {pid: deck[i] for i, pid in enumerate(seats)}
        self.cards = dict(self.dealt)            # current card of each player
        self.center = {c: deck[3 + i] for i, c in enumerate(CENTER)}
        self.known: dict[str, list[str]] = {p: [f"You were dealt {self.dealt[p]}."] for p in seats}
        self.claims: dict[str, list[str]] = {p: [] for p in seats}
        self.phase = "night"
        self.votes: dict[str, str] = {}
        self.dead: list[str] = []

    def snapshot(self):
        return {"phase": self.phase, "dealt": dict(self.dealt), "cards": dict(self.cards),
                "center": dict(self.center), "claims": {p: list(v) for p, v in self.claims.items()},
                "votes": dict(self.votes), "dead": list(self.dead)}

    def public_view(self):
        lines = [f"Phase: {self.phase}. Players in seat order: {', '.join(self.seats)}."]
        for p in self.seats:
            if self.claims[p]:
                lines.append(f"- {p} has claimed: {' -> '.join(self.claims[p])}")
        return "\n".join(lines)

    def private_view(self, pid):
        return " ".join(self.known[pid])

    def learn(self, pid, fact, **data):
        self.known[pid].append(fact)
        self.emit("night_info", fact, visible_to=[pid], actor=pid, **data)

    # ----- night -----
    def night(self):
        wolves = [p for p in self.seats if self.dealt[p] == "Werewolf"]
        for w in wolves:
            others = [o for o in wolves if o != w]
            self.learn(w, f"Werewolf: the other werewolf is {others[0]}." if others
                       else "Werewolf: you are the only werewolf among the players.", wolves=wolves)
        reqs = []
        for p in self.seats:
            role = self.dealt[p]
            others = self.others(p)
            if role == "Werewolf" and len(wolves) == 1:
                opts = CENTER + ["none"]
                instr = "Night, lone Werewolf: you may look at one center card. Choose center1, center2, center3 or none."
            elif role == "Seer":
                pairs = ["center1+center2", "center1+center3", "center2+center3"]
                opts = others + pairs + ["none"]
                instr = (f"Night, Seer: look at one other player's card ({', '.join(others)}), or two center cards "
                         f"({', '.join(pairs)}), or none.")
            elif role == "Robber":
                opts = others + ["none"]
                instr = f"Night, Robber: swap your card with {' or '.join(others)} and see your new card, or none."
            elif role == "Troublemaker":
                pair = "+".join(others)
                opts = [pair, "none"]
                instr = f"Night, Troublemaker: swap the cards of {others[0]} and {others[1]} ({pair}), or none."
            else:
                continue
            props = {"thought": THOUGHT, "choice": enum(opts)}
            reqs.append(self.request(p, "night", instr, props,
                                     lambda resp, o=opts: {**norm_common(resp),
                                                           "choice": check_choice(resp, "choice", set(o))},
                                     role=role, options=opts))
        choices = dict(zip([r.player for r in reqs], self.ask(reqs)))
        ids = {r.player: r.id for r in reqs}
        by_role = {self.dealt[p]: p for p in choices if self.dealt[p] != "Werewolf"}
        for p, d in choices.items():
            if self.dealt[p] == "Werewolf" and d["choice"] != "none":
                c = d["choice"]
                self.learn(p, f"Werewolf: you looked at {c}; it is {self.center[c]}.", looked=c,
                           saw=self.center[c], decision_id=ids[p])
        if "Seer" in by_role:
            p, c = by_role["Seer"], choices[by_role["Seer"]]["choice"]
            if c in self.seats:
                self.learn(p, f"Seer: you looked at {c}'s card; it is {self.cards[c]}.", looked=c,
                           saw=self.cards[c], decision_id=ids[p])
            elif c != "none":
                a, b = c.split("+")
                self.learn(p, f"Seer: you looked at {a} ({self.center[a]}) and {b} ({self.center[b]}).",
                           looked=c, saw=[self.center[a], self.center[b]], decision_id=ids[p])
        if "Robber" in by_role:
            p, c = by_role["Robber"], choices[by_role["Robber"]]["choice"]
            if c != "none":
                self.cards[p], self.cards[c] = self.cards[c], self.cards[p]
                self.learn(p, f"Robber: you swapped cards with {c}. Your card is now {self.cards[p]}.",
                           swapped=c, now=self.cards[p], decision_id=ids[p])
        if "Troublemaker" in by_role:
            p, c = by_role["Troublemaker"], choices[by_role["Troublemaker"]]["choice"]
            if c != "none":
                a, b = c.split("+")
                self.cards[a], self.cards[b] = self.cards[b], self.cards[a]
                self.learn(p, f"Troublemaker: you swapped the cards of {a} and {b}.", swapped=[a, b],
                           decision_id=ids[p])
        for p, d in choices.items():
            if d["choice"] == "none":
                self.emit("night_info", "You chose not to act during the night.", visible_to=[p], actor=p,
                          decision_id=ids[p])
        self.emit("dawn", "Night is over. Cards may have moved.")

    # ----- day -----
    def day(self):
        self.phase = "day"
        for rnd in range(1, ROUNDS + 1):
            for p in self.seats:
                instr = (f"Day, round {rnd} of {ROUNDS}: it is your turn to speak to the table. Also state which "
                         f"role you claim you were dealt (or none). This is public.")
                props = {"thought": THOUGHT,
                         "say": text(f"Your public statement. At most {SAY_MAX} characters."),
                         "claim_dealt": enum(ROLES + ["none"], "The role you publicly claim you were dealt.")}
                r = self.request(p, "speech", instr, props,
                                 lambda resp: {**norm_common(resp),
                                               "claim_dealt": check_choice(resp, "claim_dealt", set(ROLES + ["none"]))},
                                 round=rnd)
                d = self.ask_one(r)
                claim = d["claim_dealt"]
                if claim != "none":
                    self.claims[p].append(claim)
                self.emit("speech", f'{p} (round {rnd}, claims dealt: {claim}) says: "{d["say"]}"', actor=p,
                          say=d["say"], claim=claim, round=rnd, decision_id=r.id,
                          lie=(claim != "none" and claim != self.dealt[p]))
        self.phase = "vote"
        reqs = []
        for p in self.seats:
            opts = self.others(p)
            reqs.append(self.request(p, "vote", f"Vote: choose one other player to kill: {' or '.join(opts)}.",
                                     {"thought": THOUGHT, "vote": enum(opts)},
                                     lambda resp, o=opts: {**norm_common(resp), "vote": check_choice(resp, "vote", set(o))}))
        for r, d in zip(reqs, self.ask(reqs)):
            self.votes[r.player] = d["vote"]
        tally = {p: list(self.votes.values()).count(p) for p in self.seats}
        top = max(tally.values())
        self.dead = [] if top <= 1 else [p for p in self.seats if tally[p] == top]
        self.emit("votes", "Votes: " + ", ".join(f"{a} -> {b}" for a, b in self.votes.items()) + ". "
                  + (f"{', '.join(self.dead)} dies." if self.dead else "Nobody dies."),
                  votes=dict(self.votes), dead=list(self.dead), decision_ids=[r.id for r in reqs])

    def play(self):
        self.emit("setup", "The game begins. Seat order: " + ", ".join(self.seats) + ".")
        for p in self.seats:
            self.emit("deal", f"You were dealt {self.dealt[p]}.", visible_to=[p], actor=p, role=self.dealt[p])
        self.night()
        self.day()
        self.phase = "end"
        wolves = [p for p in self.seats if self.cards[p] == "Werewolf"]
        if wolves:
            village_wins = any(p in self.dead for p in wolves)
        else:
            village_wins = not self.dead
        winners = [p for p in self.seats if (self.cards[p] == "Werewolf") != village_wins]
        if not wolves and not village_wins:
            winners = []
        reveal = ", ".join(f"{p} ended as {self.cards[p]} (dealt {self.dealt[p]})" for p in self.seats)
        center = ", ".join(f"{c}: {v}" for c, v in self.center.items())
        self.emit("end", f"Reveal: {reveal}. Center: {center}. "
                  + ("Village wins." if village_wins else ("Werewolves win." if wolves else "Everyone loses.")),
                  winners=winners, village_wins=village_wins, final=dict(self.cards))
        self.result = {"winners": winners, "village_wins": village_wins, "wolves_final": wolves,
                       "dead": self.dead, "dealt": dict(self.dealt), "final": dict(self.cards)}
        return self.result
