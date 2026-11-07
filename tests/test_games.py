"""Engine tests with random legal players: games finish, invariants hold, and replay reproduces every log."""
import json
import os
import random
import tempfile
import unittest

from arena.engine import Invalid
from arena.games.coup import Coup, ROLES
from arena.players import build_prompt, make_asker
from arena.replay import replay_check
from arena.runner import build_game, schedule


class RandomPlayer:
    def __init__(self, seed):
        self.rng = random.Random(seed)

    def value(self, s):
        if "enum" in s:
            return self.rng.choice(s["enum"])
        if s["type"] == "array":
            return [self.value(s["items"]) for _ in range(self.rng.randint(0, 3))]
        return self.rng.choice(["", "I am definitely the Duke.", "Hmm. " * 120, "You're lying!"])

    def decide(self, game, req):
        build_prompt(game, req)  # prompt building must work at every decision point
        for _ in range(500):
            try:
                return req.validate({k: self.value(v) for k, v in req.schema["properties"].items()})
            except Invalid:
                pass
        raise AssertionError(f"random player found no valid reply for {req.kind}")


def play_random(spec, seed):
    game = build_game(spec)
    p = RandomPlayer(seed)
    game.asker = make_asker(game, {s: p for s in spec["seats"]}, parallel=False)
    return game, game.play()


def save(game, spec, result, d):
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump({**spec, "status": "finished"}, f)
    for name, rows in (("events", game.events), ("decisions", game.decisions)):
        with open(os.path.join(d, f"{name}.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
    with open(os.path.join(d, "result.json"), "w") as f:
        json.dump(result, f)


CFG = {"run_id": "test", "models": {"A": {}, "B": {}, "C": {}}, "games": {"coup": 30, "onuw": 30, "spyfall": 30}}


class TestGames(unittest.TestCase):
    def test_schedule_balance(self):
        sched = schedule({**CFG, "games": {"coup": 6, "onuw": 6, "spyfall": 6}})
        self.assertEqual(len(sched), 18)
        coup_orders = {tuple(s["seats"].values()) for s in sched if s["title"] == "coup"}
        self.assertEqual(len(coup_orders), 6)
        spies = [s["seats"][s["spy"]] for s in sched if s["title"] == "spyfall"]
        self.assertEqual(sorted(spies), ["A", "A", "B", "B", "C", "C"])

    def test_all_games_finish_and_replay(self):
        for spec in schedule(CFG):
            game, result = play_random(spec, spec["seed"] ^ 7)
            self.assertEqual(game.events[-1]["kind"], "end", spec["game_id"])
            for w in result["winners"]:
                self.assertIn(w, spec["seats"])
            with tempfile.TemporaryDirectory() as d:
                save(game, spec, result, d)
                ok, msg = replay_check(d)
                self.assertTrue(ok, f"{spec['game_id']}: {msg}")

    def test_coup_card_conservation(self):
        for spec in [s for s in schedule(CFG) if s["title"] == "coup"]:
            game, _ = play_random(spec, 3)
            for e in game.events:
                st = e["state"]
                held = sum(len(p["cards"]) for p in st["players"].values())
                self.assertEqual(held + st["deck_size"] + len(st["drawn"]), 15)
                for p in st["players"].values():
                    self.assertGreaterEqual(p["coins"], 0)
            counts = {r: 0 for r in ROLES}
            for c in game.deck + [c["role"] for p in game.p.values() for c in p["cards"]]:
                counts[c] += 1
            self.assertEqual(set(counts.values()), {3})

    def test_coup_bluff_flag(self):
        g = Coup(1, ["Ash", "Birch", "Cedar"])
        g.p["Ash"]["cards"] = [{"role": "Contessa", "up": False}, {"role": "Captain", "up": False}]
        answers = iter([{"thought": "", "action": "tax", "target": "none", "say": "Duke here."},
                        {"thought": "", "response": "allow", "say": ""},
                        {"thought": "", "response": "allow", "say": ""}])
        g.asker = lambda reqs: [r.validate(next(answers)) for r in reqs]
        g.take_turn("Ash")
        ev = next(e for e in g.events if e["kind"] == "action")
        self.assertTrue(ev["data"]["bluff"])
        self.assertEqual(g.p["Ash"]["coins"], 5)

    def test_private_info_stays_private(self):
        for spec in schedule(CFG)[:30]:
            game, _ = play_random(spec, 11)
            for seat in spec["seats"]:
                seen = [line.split("] ", 1)[1] for line in game.transcript_for(seat)]
                mine = [e["text"] for e in game.events if e["visible_to"] is None or seat in e["visible_to"]]
                self.assertEqual(seen, mine)
                for e in game.events:
                    if e["visible_to"] and seat not in e["visible_to"] and e["text"] not in mine:
                        self.assertNotIn(e["text"], "\n".join(seen))
            if spec["title"] == "onuw":  # line count must not depend on what others learned at night
                counts = {s: len(game.transcript_for(s)) - sum(1 for e in game.events if e["visible_to"] == [s])
                          for s in spec["seats"]}
                self.assertEqual(len(set(counts.values())), 1)

if __name__ == "__main__":
    unittest.main()
