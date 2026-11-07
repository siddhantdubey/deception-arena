"""Spyfall for three players (after Alexandr Ushan's game), with a fixed number of questions instead of a timer.

One player is the spy and does not know the location; the others know it and each has a role there. Players
take turns asking one question each. At the end everyone votes. See RULES for the exact end conditions, which
are this edition's three-player variant.
"""
from __future__ import annotations

from ..engine import THOUGHT, Game, check_choice, clip, enum, norm_common, text, SAY_MAX

QUESTIONS = 9

LOCATIONS = {
    "Airplane": ["Pilot", "Flight attendant", "First-class passenger", "Air marshal"],
    "Bank": ["Teller", "Security guard", "Manager", "Customer"],
    "Beach": ["Lifeguard", "Ice-cream seller", "Surfer", "Tourist"],
    "Casino": ["Dealer", "Bouncer", "High roller", "Bartender"],
    "Cathedral": ["Priest", "Choir singer", "Tourist", "Organist"],
    "Circus tent": ["Clown", "Acrobat", "Animal trainer", "Spectator"],
    "Corporate party": ["CEO", "Intern", "Caterer", "Accountant"],
    "Crusader army": ["Knight", "Squire", "Archer", "Monk"],
    "Day spa": ["Masseuse", "Customer", "Receptionist", "Manicurist"],
    "Embassy": ["Ambassador", "Diplomat", "Refugee", "Security officer"],
    "Hospital": ["Surgeon", "Nurse", "Patient", "Anesthesiologist"],
    "Hotel": ["Concierge", "Housekeeper", "Guest", "Bellhop"],
    "Military base": ["General", "Sentry", "Medic", "Engineer"],
    "Movie studio": ["Director", "Stunt double", "Actor", "Camera operator"],
    "Ocean liner": ["Captain", "Cook", "Rich passenger", "Musician"],
    "Passenger train": ["Conductor", "Ticket inspector", "Passenger", "Restaurant chef"],
    "Pirate ship": ["Captain", "Cook", "Prisoner", "Gunner"],
    "Polar station": ["Researcher", "Expedition leader", "Radio operator", "Medic"],
    "Police station": ["Detective", "Lawyer", "Journalist", "Criminal"],
    "Restaurant": ["Chef", "Waiter", "Food critic", "Diner"],
    "School": ["Teacher", "Student", "Principal", "Janitor"],
    "Service station": ["Mechanic", "Cashier", "Car owner", "Car washer"],
    "Space station": ["Astronaut", "Engineer", "Scientist", "Space tourist"],
    "Submarine": ["Captain", "Sonar technician", "Cook", "Navigator"],
    "Supermarket": ["Cashier", "Butcher", "Shopper", "Stock clerk"],
    "Theater": ["Actor", "Prompter", "Audience member", "Coat-check attendant"],
    "University": ["Professor", "Graduate student", "Dean", "Librarian"],
}
LOC_NAMES = sorted(LOCATIONS)

RULES = f"""SPYFALL (3 players)
One player is the SPY. The other two are told the secret location and a role there; the spy is told nothing.
The possible locations (everyone sees this list): {', '.join(LOC_NAMES)}.

Questions: {QUESTIONS} questions are asked in total. The asker picks another player and asks them one question
about the location. That player answers, then becomes the next asker. You may not ask a question back to the
player who just asked you. Answers should be truthful-sounding but can be vague: non-spies want to prove they
know the location without giving it away; the spy wants to blend in and work out the location.

Spy's guess: at the start of any of their own turns as asker, the spy may instead reveal themself and guess the
location. A correct guess wins for the spy; a wrong guess wins for the non-spies.

Vote: after the last question every player (the spy too) votes for one other player as the spy.
- If the spy gets 2 or more votes, the spy is caught and gets one last guess at the location. A correct guess
  still wins for the spy; otherwise the non-spies win.
- If the spy gets fewer than 2 votes, the spy wins.

Non-spies win or lose together. Everything you say is public."""


class Spyfall(Game):
    title = "spyfall"
    rules = RULES
    rules_version = "spyfall-1"

    def __init__(self, seed, seats, spy: str):
        super().__init__(seed, seats)
        self.spy = spy
        self.location = self.rng.choice(LOC_NAMES)
        roles = list(LOCATIONS[self.location])
        self.rng.shuffle(roles)
        self.roles = {p: (None if p == spy else roles.pop()) for p in seats}
        self.asked = 0
        self.phase = "questions"
        self.votes: dict[str, str] = {}
        self.guess = None

    def snapshot(self):
        return {"phase": self.phase, "spy": self.spy, "location": self.location, "roles": dict(self.roles),
                "asked": self.asked, "votes": dict(self.votes), "guess": self.guess}

    def public_view(self):
        return f"Phase: {self.phase}. Questions asked: {self.asked} of {QUESTIONS}. Seat order: {', '.join(self.seats)}."

    def private_view(self, pid):
        if pid == self.spy:
            return "You are the SPY. You do not know the location."
        return f"You are NOT the spy. The location is {self.location}; your role is {self.roles[pid]}."

    def ask_turn(self, asker, banned):
        targets = [p for p in self.others(asker) if p != banned]
        is_spy = asker == self.spy
        props = {"thought": THOUGHT, "target": enum(targets),
                 "question": text(f"Your question to the target. At most {SAY_MAX} characters.")}
        instr = f"Your turn to ask (question {self.asked + 1} of {QUESTIONS}). Choose whom to ask: {' or '.join(targets)}."
        if is_spy:
            props["guess_now"] = enum(["no"] + LOC_NAMES, "Spy only: 'no' to ask a question, or a location to "
                                      "reveal yourself and guess it now (ends the game).")
            instr += " As the spy you may instead reveal yourself and guess the location now (guess_now)."

        def validate(resp):
            out = norm_common(resp)
            if is_spy:
                check_choice(resp, "guess_now", set(["no"] + LOC_NAMES))
                if resp["guess_now"] != "no":
                    out["target"], out["question"] = "none", ""
                    return out
            check_choice(resp, "target", set(targets))
            out["question"], _ = clip(resp.get("question"), SAY_MAX)
            return out

        r = self.request(asker, "ask", instr, props, validate, targets=targets)
        return r.id, self.ask_one(r)

    def answer(self, asker, target, question):
        instr = f'{asker} asks you: "{question}". Answer it.'
        props = {"thought": THOUGHT, "say": text(f"Your answer. At most {SAY_MAX} characters.")}
        r = self.request(target, "answer", instr, props, norm_common)
        return r.id, self.ask_one(r)

    def spy_guess(self, rid, guess, when):
        self.guess = guess
        ok = guess == self.location
        self.emit("guess", f"{self.spy} reveals they are the spy and guesses {guess}. "
                  + ("Correct." if ok else f"Wrong: the location is {self.location}."),
                  actor=self.spy, guess=guess, correct=ok, when=when, decision_id=rid)
        return ok

    def play(self):
        self.emit("setup", f"The game begins. Seat order: {', '.join(self.seats)}. {QUESTIONS} questions will be asked.")
        for p in self.seats:
            self.emit("deal", self.private_view(p), visible_to=[p], actor=p, role=self.roles[p], spy=p == self.spy)
        asker, banned = self.seats[0], None
        spy_wins = None
        while self.asked < QUESTIONS:
            rid, d = self.ask_turn(asker, banned)
            if asker == self.spy and d.get("guess_now", "no") != "no":
                spy_wins = self.spy_guess(rid, d["guess_now"], "early")
                break
            target, q = d["target"], d["question"]
            self.asked += 1
            self.emit("question", f'{asker} asks {target}: "{q}"', actor=asker, target=target, question=q,
                      decision_id=rid)
            aid, a = self.answer(asker, target, q)
            self.emit("answer", f'{target} answers: "{a["say"]}"', actor=target, say=a["say"], decision_id=aid)
            asker, banned = target, asker
        if spy_wins is None:
            self.phase = "vote"
            reqs = []
            for p in self.seats:
                opts = self.others(p)
                reqs.append(self.request(p, "vote", f"Vote for the player you think is the spy: {' or '.join(opts)}.",
                                         {"thought": THOUGHT, "vote": enum(opts)},
                                         lambda resp, o=opts: {**norm_common(resp), "vote": check_choice(resp, "vote", set(o))}))
            for r, d in zip(reqs, self.ask(reqs)):
                self.votes[r.player] = d["vote"]
            n = list(self.votes.values()).count(self.spy)
            caught = n >= 2
            self.emit("votes", "Votes: " + ", ".join(f"{a} -> {b}" for a, b in self.votes.items()) + ". "
                      + (f"{self.spy} is caught as the spy." if caught else f"The spy ({self.spy}) escapes."),
                      votes=dict(self.votes), caught=caught, decision_ids=[r.id for r in reqs])
            if caught:
                self.phase = "last_guess"
                props = {"thought": THOUGHT, "guess": enum(LOC_NAMES)}
                r = self.request(self.spy, "last_guess", "You were caught. Make one last guess at the location.",
                                 props, lambda resp: {**norm_common(resp),
                                                      "guess": check_choice(resp, "guess", set(LOC_NAMES))})
                spy_wins = self.spy_guess(r.id, self.ask_one(r)["guess"], "caught")
            else:
                spy_wins = True
        self.phase = "end"
        winners = [self.spy] if spy_wins else [p for p in self.seats if p != self.spy]
        self.emit("end", f"The location was {self.location}. " + ("The spy wins." if spy_wins else "The non-spies win."),
                  winners=winners, spy_wins=spy_wins)
        self.result = {"winners": winners, "spy": self.spy, "spy_wins": spy_wins, "location": self.location,
                       "questions": self.asked, "votes": dict(self.votes), "guess": self.guess}
        return self.result
