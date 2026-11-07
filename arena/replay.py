"""Replay check: rebuild a game from its meta.json, feed it the logged decisions and confirm that it produces
exactly the logged events and result. Videos and stats are made from the event log, so this check is what
ties them to what the models actually did."""
from __future__ import annotations

import json
import os

from .players import ScriptedPlayer, make_asker
from .runner import build_game


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def replay_check(gdir) -> tuple[bool, str]:
    with open(os.path.join(gdir, "meta.json")) as f:
        meta = json.load(f)
    if meta.get("status") != "finished":
        return False, f"not finished ({meta.get('status')})"
    events = load_jsonl(os.path.join(gdir, "events.jsonl"))
    decisions = {d["id"]: d for d in load_jsonl(os.path.join(gdir, "decisions.jsonl"))}
    game = build_game(meta)
    player = ScriptedPlayer(decisions)
    game.asker = make_asker(game, {s: player for s in meta["seats"]}, parallel=False)
    result = game.play()
    with open(os.path.join(gdir, "result.json")) as f:
        logged_result = json.load(f)
    replayed = json.loads(json.dumps(game.events))
    if len(replayed) != len(events):
        return False, f"event count differs: replay {len(replayed)}, log {len(events)}"
    for a, b in zip(replayed, events):
        if a != b:
            return False, f"event {b['seq']} differs:\n  replay: {a['text']}\n  log:    {b['text']}"
    if json.loads(json.dumps(result)) != logged_result:
        return False, "result differs"
    if len(game.decisions) != len(decisions):
        return False, f"decision count differs: replay used {len(game.decisions)}, log has {len(decisions)}"
    return True, f"{len(events)} events and {len(decisions)} decisions reproduced"
