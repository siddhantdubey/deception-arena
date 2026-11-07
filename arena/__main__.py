"""Command line:

    python -m arena run CONFIG --out DIR [--only GAME_ID ...] [--workers N]
    python -m arena check RUN_DIR             replay every finished game from its logged decisions
    python -m arena summary RUN_DIR           write summary.json and summary.md
    python -m arena schedule CONFIG           print the schedule without calling any model
    python -m arena smoke CONFIG --out DIR    one tiny call per model (counts against the budget)
    python -m arena render GAME_DIR OUT.mp4   (needs Pillow and ffmpeg)
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arena")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("config")
    r.add_argument("--out", required=True)
    r.add_argument("--only", nargs="*")
    r.add_argument("--workers", type=int, default=3)
    sub.add_parser("check").add_argument("run_dir")
    sub.add_parser("summary").add_argument("run_dir")
    sub.add_parser("schedule").add_argument("config")
    sm = sub.add_parser("smoke")
    sm.add_argument("config")
    sm.add_argument("--out", required=True)
    v = sub.add_parser("render")
    v.add_argument("game_dir")
    v.add_argument("out")
    a = ap.parse_args(argv)

    if a.cmd in ("run", "schedule", "smoke"):
        with open(a.config) as f:
            cfg = json.load(f)
    if a.cmd == "run":
        from .runner import Runner
        Runner(cfg, a.out).run(only=a.only, workers=a.workers)
    elif a.cmd == "smoke":
        from .runner import smoke
        smoke(cfg, a.out)
    elif a.cmd == "schedule":
        from .runner import schedule
        for s in schedule(cfg):
            print(json.dumps(s))
    elif a.cmd == "check":
        from .replay import replay_check
        bad = 0
        for gid in sorted(os.listdir(a.run_dir)):
            gdir = os.path.join(a.run_dir, gid)
            if not os.path.exists(os.path.join(gdir, "meta.json")):
                continue
            ok, msg = replay_check(gdir)
            bad += not ok and not msg.startswith("not finished")
            print(f"{gid}: {'OK' if ok else 'FAIL' if not msg.startswith('not finished') else 'SKIP'} {msg}")
        sys.exit(1 if bad else 0)
    elif a.cmd == "summary":
        from .stats import markdown, summarize
        s = summarize(a.run_dir)
        with open(os.path.join(a.run_dir, "summary.json"), "w") as f:
            json.dump(s, f, indent=1)
        md = markdown(s)
        with open(os.path.join(a.run_dir, "summary.md"), "w") as f:
            f.write(md + "\n")
        print(md)
    elif a.cmd == "render":
        from .render import render_game
        render_game(a.game_dir, a.out)


if __name__ == "__main__":
    main()
