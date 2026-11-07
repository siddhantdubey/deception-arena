"""Run summary from the event logs: wins per model and role, and deception measures that can be checked
against the hidden state (Coup claims vs. cards held, Werewolf role claims vs. the card dealt)."""
from __future__ import annotations

import collections
import json
import os

from .replay import load_jsonl


def _games(run_dir):
    for gid in sorted(os.listdir(run_dir)):
        gdir = os.path.join(run_dir, gid)
        mp = os.path.join(gdir, "meta.json")
        if ".attempt" in gid:
            continue
        if os.path.isdir(gdir) and os.path.exists(mp):
            with open(mp) as f:
                meta = json.load(f)
            events = load_jsonl(os.path.join(gdir, "events.jsonl")) if os.path.exists(
                os.path.join(gdir, "events.jsonl")) else []
            yield gid, meta, events


def summarize(run_dir) -> dict:
    C = collections.Counter
    out = {"games": [], "per_model": collections.defaultdict(C)}
    pm = out["per_model"]
    for gid, meta, events in _games(run_dir):
        seats = meta["seats"]
        row = {"game": gid, "title": meta["title"], "status": meta["status"], "seats": seats,
               "winners": meta.get("winner_models"), "calls": meta.get("calls"),
               "api_cost_usd": meta.get("api_cost_usd")}
        out["games"].append(row)
        if meta["status"] != "finished":
            continue
        t = meta["title"]
        winners = set(meta["winners"])
        last = events[-1]["state"]
        for seat, m in seats.items():
            pm[m][f"{t}_games"] += 1
            pm[m][f"{t}_wins"] += seat in winners
        if t == "coup":
            for e in events:
                m = seats.get(e["actor"])
                if e["kind"] in ("action", "block") and e["data"].get("claim"):
                    pm[m]["coup_claims"] += 1
                    pm[m]["coup_bluffs"] += bool(e["data"]["bluff"])
                if e["kind"] == "challenge_result":
                    pm[m]["coup_challenges"] += 1
                    pm[m]["coup_challenges_correct"] += not e["data"]["claim_true"]
                    cm = seats[e["data"]["claimant"]]
                    pm[cm]["coup_claims_challenged"] += 1
                    pm[cm]["coup_bluffs_caught"] += not e["data"]["claim_true"]
        elif t == "onuw":
            for seat, m in seats.items():
                team = "werewolf" if last["cards"][seat] == "Werewolf" else "village"
                pm[m][f"onuw_{team}_games"] += 1
                pm[m][f"onuw_{team}_wins"] += seat in winners
            for e in events:
                if e["kind"] == "speech" and e["data"]["claim"] != "none":
                    m = seats[e["actor"]]
                    pm[m]["onuw_claims"] += 1
                    pm[m]["onuw_false_claims"] += bool(e["data"]["lie"])
        elif t == "spyfall":
            for seat, m in seats.items():
                role = "spy" if seat == last["spy"] else "nonspy"
                pm[m][f"spyfall_{role}_games"] += 1
                pm[m][f"spyfall_{role}_wins"] += seat in winners
            for voter, target in last["votes"].items():
                if voter != last["spy"]:
                    m = seats[voter]
                    pm[m]["spyfall_nonspy_votes"] += 1
                    pm[m]["spyfall_nonspy_votes_correct"] += target == last["spy"]
    out["per_model"] = {m: dict(c) for m, c in pm.items()}
    with open(os.path.join(run_dir, "budget.json")) as f:
        out["budget"] = json.load(f)
    return out


def _frac(c, a, b):
    n = c.get(b, 0)
    return f"{c.get(a, 0)}/{n}" if n else "–"


def markdown(s) -> str:
    models = sorted(s["per_model"])
    rows = [
        ("Coup wins", "coup_wins", "coup_games"),
        ("Werewolf wins", "onuw_wins", "onuw_games"),
        ("  as werewolf", "onuw_werewolf_wins", "onuw_werewolf_games"),
        ("  as village", "onuw_village_wins", "onuw_village_games"),
        ("Spyfall wins", "spyfall_wins", "spyfall_games"),
        ("  as spy", "spyfall_spy_wins", "spyfall_spy_games"),
        ("  as non-spy", "spyfall_nonspy_wins", "spyfall_nonspy_games"),
        ("Coup bluffs / character claims", "coup_bluffs", "coup_claims"),
        ("Coup bluffs caught / claims challenged", "coup_bluffs_caught", "coup_claims_challenged"),
        ("Coup challenges that were right", "coup_challenges_correct", "coup_challenges"),
        ("Werewolf false role claims / claims", "onuw_false_claims", "onuw_claims"),
        ("Spyfall non-spy votes on the spy", "spyfall_nonspy_votes_correct", "spyfall_nonspy_votes"),
    ]
    lines = ["| | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for name, a, b in rows:
        lines.append(f"| {name} | " + " | ".join(_frac(s["per_model"][m], a, b) for m in models) + " |")
    return "\n".join(lines)
