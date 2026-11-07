"""Video reconstruction of one game, drawn only from its logs (meta.json, events.jsonl, decisions.jsonl).

Each event becomes one still frame showing the table state stored with that event, the event itself (text
verbatim) and, when the event came from a decision, that player's private note. Frames are held long enough to
read and joined with ffmpeg. Nothing is re-simulated or paraphrased. The view is omniscient: hidden cards,
roles and private notes are shown to the viewer, each marked as hidden from the other players.

Needs Pillow and ffmpeg. Fonts default to DejaVu; set ARENA_FONT_DIR to use another directory with the same
file names.
"""
from __future__ import annotations

import json
import os
import subprocess

from PIL import Image, ImageDraw, ImageFont

from .replay import load_jsonl

W, H = 1920, 1080
FONT_DIR = os.environ.get("ARENA_FONT_DIR", "/usr/share/fonts/truetype/dejavu")
BG, PANEL, PANEL2, LINE = "#101219", "#1a1d28", "#222636", "#2e3346"
TEXT, MUTED, DIM = "#eceef4", "#9aa1b6", "#5d6378"
RED, GREEN, AMBER, NOTE_BG = "#ff5d62", "#3fd08f", "#f2b84b", "#211d16"
DEFAULT_COLORS = ["#ff7a1a", "#3fa7ff", "#b46cff"]
ROLE_COLORS = {"Duke": "#7b4fd1", "Assassin": "#3b3f4a", "Captain": "#2f6fd6", "Ambassador": "#2e9a62",
               "Contessa": "#c43c4c", "Werewolf": "#b8323a", "Seer": "#6a55d8", "Robber": "#8a6a3a",
               "Troublemaker": "#c06a2a", "Villager": "#3c8a5a"}
TITLES = {"coup": "Coup", "onuw": "One Night Ultimate Werewolf", "spyfall": "Spyfall"}

_fonts: dict = {}


def font(size, kind="sans"):
    key = (size, kind)
    if key not in _fonts:
        name = {"sans": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf", "serif": "DejaVuSerif.ttf",
                "mono": "DejaVuSansMono.ttf"}[kind]
        _fonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    return _fonts[key]


def wrap(text, f, width):
    lines = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}".strip()
            if f.getlength(trial) <= width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def fit(text, width, height, sizes=(44, 40, 36, 32, 29, 26, 23, 21, 19, 17), kind="sans", spacing=1.3):
    """Largest font size at which the text wraps into the box. The text is never cut."""
    for s in sizes:
        f = font(s, kind)
        lines = wrap(text, f, width)
        if len(lines) * s * spacing <= height:
            return f, lines, int(s * spacing)
    f = font(sizes[-1], kind)
    return f, wrap(text, f, width), int(sizes[-1] * spacing)


def draw_lines(d, xy, lines, f, lh, fill):
    x, y = xy
    for ln in lines:
        d.text((x, y), ln, font=f, fill=fill)
        y += lh
    return y


def badge(d, x, y, label, color, size=22):
    f = font(size, "bold")
    w = f.getlength(label) + 24
    d.rounded_rectangle([x, y, x + w, y + size + 14], radius=8, fill=color)
    d.text((x + 12, y + 6), label, font=f, fill="#0d0f14")
    return x + w + 10


def card(d, x, y, w, h, role, hidden=False, lost=False, label=None):
    col = ROLE_COLORS.get(role, "#555b70")
    if lost:
        d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill="#2a2d38", outline="#3a3f50", width=2)
        f = font(max(16, int(w / 7.5)), "bold")
        d.text((x + w / 2, y + h / 2 - 12), role, font=f, fill=DIM, anchor="mm")
        d.text((x + w / 2, y + h / 2 + 22), "LOST", font=font(18, "bold"), fill=RED, anchor="mm")
        return
    d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=col, outline="#ffffff30", width=2)
    f = font(max(15, int(w / 7.5)), "bold")
    d.text((x + w / 2, y + h / 2), role, font=f, fill="#ffffff", anchor="mm")
    if hidden or label:
        d.text((x + w / 2, y + 16), label or "hidden", font=font(15), fill="#ffffffb0", anchor="mm")


class Renderer:
    def __init__(self, gdir):
        with open(os.path.join(gdir, "meta.json")) as f:
            self.meta = json.load(f)
        self.events = load_jsonl(os.path.join(gdir, "events.jsonl"))
        self.decisions = {d["id"]: d for d in load_jsonl(os.path.join(gdir, "decisions.jsonl"))}
        self.title = self.meta["title"]
        self.seats = list(self.meta["seats"])
        models = self.meta["models"]
        self.display = {s: models[s].get("display", models[s]["label"]) for s in self.seats}
        self.color = {s: models[s].get("color", DEFAULT_COLORS[i]) for i, s in enumerate(self.seats)}

    # ----- frame parts -----
    def header(self, d, ev):
        d.rectangle([0, 0, W, 84], fill=PANEL)
        d.text((36, 20), TITLES[self.title], font=font(38, "bold"), fill=TEXT)
        x = 36 + font(38, "bold").getlength(TITLES[self.title]) + 24
        d.text((x, 34), f"{self.meta['game_id']}  ·  event {ev['seq'] + 1} of {len(self.events)}",
               font=font(22), fill=MUTED)
        st = ev["state"]
        if self.title == "coup":
            right = f"Turn {st['turn']}  ·  deck {st['deck_size']}"
        elif self.title == "onuw":
            right = "Center (face down): " + "  ·  ".join(st["center"].values()) + f"   |   {st['phase']}"
        else:
            right = f"Location (secret): {st['location']}   |   question {st['asked']} of 9"
        f = font(22)
        d.text((W - 36 - f.getlength(right), 32), right, font=f, fill=MUTED)

    def panel(self, d, seat, ev, x, y, w, h):
        st = ev["state"]
        active = ev.get("actor") == seat
        d.rounded_rectangle([x, y, x + w, y + h], radius=16, fill=PANEL,
                            outline=self.color[seat] if active else LINE, width=4 if active else 2)
        d.rounded_rectangle([x, y, x + w, y + 70], radius=16, fill=PANEL2)
        d.rectangle([x, y + 54, x + w, y + 70], fill=PANEL2)
        d.ellipse([x + 22, y + 22, x + 46, y + 46], fill=self.color[seat])
        d.text((x + 60, y + 14), seat, font=font(32, "bold"), fill=TEXT)
        f = font(22)
        lbl = self.display[seat]
        d.text((x + w - 22 - f.getlength(lbl), y + 24), lbl, font=f, fill=self.color[seat])
        by = y + 86
        getattr(self, f"panel_{self.title}")(d, seat, st, x + 22, by, w - 44, y + h - by - 16)

    def panel_coup(self, d, seat, st, x, y, w, h):
        p = st["players"][seat]
        d.text((x, y), f"{p['coins']}", font=font(54, "bold"), fill=AMBER)
        d.text((x + font(54, "bold").getlength(f"{p['coins']}") + 10, y + 26), "coins", font=font(22), fill=MUTED)
        cw, ch = 150, 196
        cx = x + w - 2 * cw - 18
        for i, c in enumerate(p["cards"]):
            card(d, cx + i * (cw + 18), y, cw, ch, c["role"], hidden=not c["up"], lost=c["up"])
        if st.get("drawn") and st.get("active") == seat:
            d.text((x, y + 92), "drawing:", font=font(18), fill=MUTED)
            for i, r in enumerate(st["drawn"]):
                card(d, x + i * 88, y + 118, 80, 78, r)
        if not p["alive"]:
            d.text((x, y + 92), "OUT", font=font(44, "bold"), fill=RED)

    def panel_onuw(self, d, seat, st, x, y, w, h):
        dealt, now = st["dealt"][seat], st["cards"][seat]
        cw, ch = 150, 150
        card(d, x, y, cw, ch, dealt, label="dealt")
        card(d, x + cw + 50, y, cw, ch, now, label="now")
        d.text((x + cw + 25, y + ch / 2), "→", font=font(30, "bold"), fill=MUTED if dealt == now else AMBER, anchor="mm")
        tx = x + 2 * cw + 76
        team = "Werewolf team" if now == "Werewolf" else "Village team"
        d.text((tx, y), team, font=font(22, "bold"), fill=RED if now == "Werewolf" else GREEN)
        claims = st["claims"][seat]
        f, lines, lh = fit("Claims dealt: " + (" → ".join(claims) if claims else "none yet"), w - (tx - x),
                           70, sizes=(20, 18, 16))
        draw_lines(d, (tx, y + 36), lines, f, lh, TEXT)
        if seat in st["votes"]:
            d.text((tx, y + 110), f"votes {st['votes'][seat]}", font=font(20), fill=MUTED)
        if seat in st["dead"]:
            d.text((x, y + ch + 8), "DIED", font=font(30, "bold"), fill=RED)

    def panel_spyfall(self, d, seat, st, x, y, w, h):
        if seat == st["spy"]:
            d.rounded_rectangle([x, y, x + 220, y + 110], radius=14, fill=RED)
            d.text((x + 110, y + 55), "SPY", font=font(52, "bold"), fill="#0d0f14", anchor="mm")
            d.text((x + 240, y + 20), "does not know", font=font(22), fill=MUTED)
            d.text((x + 240, y + 52), "the location", font=font(22), fill=MUTED)
        else:
            d.text((x, y), st["location"], font=font(30, "bold"), fill=TEXT)
            d.text((x, y + 44), st["roles"][seat], font=font(24), fill=MUTED)
            d.text((x, y + 80), "(hidden from the spy)", font=font(18), fill=DIM)
        if seat in st["votes"]:
            d.text((x, y + 130), f"votes {st['votes'][seat]}", font=font(22), fill=TEXT)

    def source_decision(self, ev):
        """Coup challenge and block outcomes are logged without a decision id; the decision behind them is the
        actor's latest 'challenge' / 'block_*' response event before this one."""
        if ev["kind"] not in ("challenge_result", "block"):
            return None
        want = "challenge" if ev["kind"] == "challenge_result" else "block_"
        for e in reversed(self.events[:ev["seq"]]):
            if e["kind"] == "response" and e["actor"] == ev["actor"] and e["data"]["response"].startswith(want):
                return e["data"]["decision_id"]
        return None

    def notes_for(self, ev):
        ids = ev["data"].get("decision_ids") or ([ev["data"]["decision_id"]] if ev["data"].get("decision_id") is not None else [])
        if not ids and self.source_decision(ev) is not None:
            ids = [self.source_decision(ev)]
        out = []
        for i in ids:
            dec = self.decisions.get(i)
            if dec and dec["decision"].get("thought"):
                out.append((dec["player"], dec["decision"]["thought"]))
        return out

    def stage(self, d, ev, x, y, w, h):
        actor = ev.get("actor")
        col = self.color.get(actor, MUTED)
        d.rounded_rectangle([x, y, x + w, y + h], radius=16, fill=PANEL2)
        d.rectangle([x, y + 16, x + 8, y + h - 16], fill=col)
        k, data = ev["kind"], ev["data"]
        ix, iy, iw = x + 40, y + 28, w - 80
        private = ev.get("visible_to")
        if private:
            tag = "PRIVATE · only " + ", ".join(private) + " sees this"
            badge(d, ix, iy, tag, AMBER, 20)
            iy += 54
        quote = data.get("question") if k == "question" else data.get("say")
        if quote is not None and k in ("say", "speech", "answer", "question"):
            head = {"say": f"{actor} says", "answer": f"{actor} answers",
                    "question": f"{actor} asks {data.get('target')}",
                    "speech": f"{actor} · round {data.get('round')}"}[k]
            d.text((ix, iy), head, font=font(30, "bold"), fill=col)
            if k == "speech":
                bx = ix + font(30, "bold").getlength(head) + 24
                bx = badge(d, bx, iy, f"claims dealt: {data['claim']}", MUTED, 20)
                if data["claim"] != "none":
                    badge(d, bx, iy, "FALSE CLAIM" if data["lie"] else "TRUE CLAIM", RED if data["lie"] else GREEN, 20)
            f, lines, lh = fit(f"“{quote}”" if quote else "(says nothing)", iw, h - (iy - y) - 70)
            draw_lines(d, (ix, iy + 56), lines, f, lh, TEXT)
            return
        f, lines, lh = fit(ev["text"], iw, h - (iy - y) - 90, sizes=(40, 36, 32, 28, 25, 22, 20))
        yy = draw_lines(d, (ix, iy), lines, f, lh, TEXT)
        bx = ix
        if k in ("action", "block") and data.get("claim"):
            bx = badge(d, bx, yy + 14, f"claims {data['claim']}", MUTED)
            badge(d, bx, yy + 14, "BLUFF" if data["bluff"] else "TRUE CLAIM", RED if data["bluff"] else GREEN)
        elif k == "challenge_result":
            badge(d, bx, yy + 14, "CLAIM WAS TRUE — CHALLENGE FAILS" if data["claim_true"] else "BLUFF CAUGHT",
                  GREEN if data["claim_true"] else RED)

    def note_panel(self, d, notes, x, y, w, h):
        d.rounded_rectangle([x, y, x + w, y + h], radius=16, fill=NOTE_BG, outline="#4a3f25", width=2)
        d.text((x + 24, y + 18), "PRIVATE NOTE", font=font(20, "bold"), fill=AMBER)
        d.text((x + 24 + font(20, "bold").getlength("PRIVATE NOTE") + 14, y + 21), "hidden from the other players",
               font=font(17), fill="#a08a5c")
        if not notes:
            d.text((x + 24, y + 90), "—", font=font(26, "serif"), fill=DIM)
            return
        top, avail = y + 58, h - 70
        per = avail / len(notes)
        for i, (who, thought) in enumerate(notes):
            yy = top + i * per
            d.text((x + 24, yy), who, font=font(20, "bold"), fill=self.color.get(who, TEXT))
            f, lines, lh = fit(thought, w - 48, per - 34, sizes=(26, 24, 22, 20, 18, 16, 15, 14), kind="serif",
                               spacing=1.28)
            draw_lines(d, (x + 24, yy + 30), lines, f, lh, "#efe4cc")

    def log_strip(self, d, ev, x, y, w, h):
        d.text((x, y), "Earlier", font=font(18, "bold"), fill=DIM)
        prev = self.events[max(0, ev["seq"] - 4):ev["seq"]]
        yy = y + 28
        for e in prev:
            t = e["text"] if not e.get("visible_to") else f"[private · {', '.join(e['visible_to'])}] {e['text']}"
            f = font(19)
            line = wrap(t, f, w)[0]
            if len(wrap(t, f, w)) > 1:
                line = line.rstrip() + " …"
            d.text((x, yy), line, font=f, fill=MUTED if not e.get("visible_to") else "#8f7d55")
            yy += 30

    def frame(self, ev) -> Image.Image:
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im, "RGBA")
        self.header(d, ev)
        pw = (W - 72 - 2 * 24) / 3
        for i, s in enumerate(self.seats):
            self.panel(d, s, ev, 36 + i * (pw + 24), 104, pw, 330)
        notes = self.notes_for(ev)
        if len(notes) > 1:  # simultaneous decisions: short event box, notes side by side underneath
            self.stage(d, ev, 36, 456, W - 72, 150)
            nw = (W - 72 - 24 * (len(notes) - 1)) / len(notes)
            for i, n in enumerate(notes):
                self.note_panel(d, [n], 36 + i * (nw + 24), 626, nw, 276)
        else:
            self.stage(d, ev, 36, 456, 1150, 446)
            self.note_panel(d, notes, 1210, 456, W - 36 - 1210, 446)
        self.log_strip(d, ev, 40, 918, W - 80, 150)
        return im

    def title_card(self) -> Image.Image:
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im, "RGBA")
        m = self.meta
        d.text((120, 120), "Deception Arena", font=font(34), fill=MUTED)
        d.text((120, 170), TITLES[self.title], font=font(84, "bold"), fill=TEXT)
        d.text((120, 290), f"{m['game_id']}  ·  seed {m['seed']}  ·  rules {m.get('rules_version')}  ·  "
               f"started {m.get('started', '')[:16].replace('T', ' ')} UTC", font=font(24), fill=MUTED)
        y = 390
        for s in self.seats:
            mm = m["models"][s]
            d.ellipse([120, y + 8, 148, y + 36], fill=self.color[s])
            d.text((170, y), s, font=font(40, "bold"), fill=TEXT)
            d.text((340, y + 6), f"{self.display[s]}", font=font(34), fill=self.color[s])
            d.text((340, y + 50), f"{mm['model']} via {mm['backend']}, effort {mm.get('effort')}",
                   font=font(20), fill=DIM)
            y += 110
        f, lines, lh = fit("Omniscient replay. Hidden cards, roles and private notes are shown to you; each player "
                           "saw only their own. Every frame is drawn from the logged game events, and all speech "
                           "and notes are shown verbatim.", W - 240, 120, sizes=(26, 24, 22))
        draw_lines(d, (120, 760), lines, f, lh, MUTED)
        return im

    def end_card(self) -> Image.Image:
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im, "RGBA")
        m = self.meta
        last = self.events[-1]
        winners = last["data"].get("winners") or []
        d.text((120, 120), TITLES[self.title] + " · result", font=font(34), fill=MUTED)
        if winners:
            d.text((120, 180), " & ".join(f"{w} ({self.display[w]})" for w in winners) + " win" +
                   ("s" if len(winners) == 1 else ""), font=font(56, "bold"), fill=TEXT)
        else:
            d.text((120, 180), "No winner", font=font(56, "bold"), fill=TEXT)
        f, lines, lh = fit(last["text"], W - 240, 200, sizes=(30, 28, 26, 24))
        y = draw_lines(d, (120, 280), lines, f, lh, MUTED) + 30
        for line in self.game_facts():
            d.text((120, y), line, font=font(26), fill=TEXT)
            y += 44
        cost = m.get("api_cost_usd") or {}
        foot = (f"{m.get('calls')} model calls ({m.get('calls_rejected_or_failed', 0)} rejected or failed, retried)."
                + (" API list-price cost: " + ", ".join(f"{k} ${v:.3f}" for k, v in cost.items()) + "." if cost else ""))
        d.text((120, H - 120), foot, font=font(22), fill=DIM)
        return im

    def game_facts(self):
        facts = []
        if self.title == "coup":
            for s in self.seats:
                cl = [e for e in self.events if e["actor"] == s and e["kind"] in ("action", "block") and e["data"].get("claim")]
                bl = sum(bool(e["data"]["bluff"]) for e in cl)
                caught = sum(1 for e in self.events if e["kind"] == "challenge_result"
                             and e["data"]["claimant"] == s and not e["data"]["claim_true"])
                facts.append(f"{s} ({self.display[s]}): {len(cl)} character claims, {bl} bluffs, {caught} caught")
        elif self.title == "onuw":
            for s in self.seats:
                sp = [e for e in self.events if e["actor"] == s and e["kind"] == "speech"]
                lies = sum(bool(e["data"]["lie"]) for e in sp)
                st = self.events[-1]["state"]
                facts.append(f"{s} ({self.display[s]}): dealt {st['dealt'][s]}, ended {st['cards'][s]}; "
                             f"false role claims {lies} of {sum(e['data']['claim'] != 'none' for e in sp)}")
        else:
            st = self.events[-1]["state"]
            facts.append(f"Spy: {st['spy']} ({self.display[st['spy']]}). Location: {st['location']}.")
            if st["votes"]:
                facts.append("Votes: " + ", ".join(f"{a} → {b}" for a, b in st["votes"].items()))
        return facts

    def duration(self, ev, notes):
        n = len(ev["text"]) + 0.6 * sum(len(t) for _, t in notes)
        base = 1.2 if ev["kind"] in ("response", "effect", "draw", "dawn") else 2.0
        return round(min(14.0, max(base, base + n / 24)), 2)


def render_game(gdir, out_path, fps=30):
    """Each frame is written to ffmpeg as raw RGB, repeated for exactly round(hold * fps) video frames."""
    r = Renderer(gdir)
    if r.meta.get("status") != "finished":
        raise SystemExit(f"{gdir}: game not finished ({r.meta.get('status')})")
    plan = [("title", r.title_card, 6.0)]
    for ev in r.events:
        plan.append((f"e{ev['seq']:04d}", (lambda ev=ev: r.frame(ev)), r.duration(ev, r.notes_for(ev))))
    plan.append(("end", r.end_card, 8.0))
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "medium",
                           "-crf", "20", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                           out_path], stdin=subprocess.PIPE)
    frames_log, n_total = [], 0
    try:
        for name, make, hold in plan:
            n = max(1, round(hold * fps))
            buf = make().tobytes()
            for _ in range(n):
                ff.stdin.write(buf)
            frames_log.append({"name": name, "start_s": round(n_total / fps, 3), "frames": n})
            n_total += n
    finally:
        ff.stdin.close()
        if ff.wait() != 0:
            raise RuntimeError("ffmpeg failed")
    got = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                              "stream=nb_read_frames", "-of", "csv=p=0", out_path],
                             capture_output=True, text=True, check=True).stdout.strip())
    if got != n_total:
        raise RuntimeError(f"{out_path}: encoded {got} frames, planned {n_total}")
    with open(os.path.splitext(out_path)[0] + ".frames.json", "w") as f:
        json.dump({"game": r.meta["game_id"], "fps": fps, "frames": n_total, "seconds": round(n_total / fps, 2),
                   "items": frames_log}, f, indent=0)
    return n_total / fps


def still(gdir, seq, out_png):
    """One frame as a PNG, for checking the layout."""
    r = Renderer(gdir)
    im = r.title_card() if seq == "title" else r.end_card() if seq == "end" else r.frame(r.events[int(seq)])
    im.save(out_png)
