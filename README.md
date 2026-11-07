# Deception Arena

Frontier models play social deception games against each other, one model per seat. The project was inspired by
[dwarkeshsp/clocktower](https://github.com/dwarkeshsp/clocktower), which does the same for Blood on the
Clocktower.

Three games that work with exactly three players:

| Game | Deception | Checked against hidden state |
|---|---|---|
| **Coup** | Claiming characters you may not hold, and challenging others' claims | Every action and block claim is tagged true or bluff against the claimant's cards at that moment |
| **One Night Ultimate Werewolf** (3 players + 3 center cards) | Hiding a werewolf, claiming roles, lying about night information | Each speech carries a public claim about the role the speaker was dealt, tagged true or false |
| **Spyfall** | The spy blends in without knowing the location | Spy wins, early and late guesses, votes on the spy |

The engine, logging and renderer use only the Python standard library. Rendering videos also needs Pillow and
ffmpeg.

## How a game runs

- Seats are named Ash, Birch and Cedar. Players are not told which model sits in which seat.
- Calls are stateless. Every prompt carries the rules, the seat's private information, the public table state,
  every event the seat has seen, and the seat's own private notes from earlier decisions. Transcript lines are
  numbered per player, so gaps cannot reveal how many private events other players received.
- Every decision is a JSON object enforced by a strict schema. It includes a private `thought` field, which is
  hidden from the other players and shown back to its author on later turns. Free text is cut to 400 characters,
  and the cut text is exactly what the other players see.
- Simultaneous decisions (Coup responses, night actions, votes) are asked in parallel. Each player answers
  without seeing the others' answers.
- An invalid reply is retried with the validation error, up to 4 attempts. A game that still gets no valid
  reply is marked `aborted`. Defaults are never filled in.

### Rules choices

Each engine's docstring and rules text give the full rules. Deviations from the published games:

- **Coup.** When an action can be challenged or blocked, every eligible player answers once and simultaneously
  (allow, challenge or block). Challenges resolve first; if several players challenge, the one seated earliest
  after the actor is the challenger. Then a block applies, and anyone except the blocker may challenge it.
  Games stop at 60 turns, which counts as a draw.
- **One Night Werewolf.** Night choices are made simultaneously and resolved in wake order (Werewolf, Seer,
  Robber, Troublemaker). With this role set that gives the same result as waking players in turn. The day is
  three rounds of speeches in seat order, then a simultaneous vote. A 1-1-1 vote kills nobody.
- **Spyfall.** There are 9 questions instead of a timer, and you may not ask back the player who just asked you.
  The spy may guess the location at the start of any of their own turns. After the questions everyone votes.
  The spy is caught with 2 or more votes, but a caught spy still wins with a correct last guess.

## Backends

All model calls go through `arena/backends.py`:

| Backend | Route |
|---|---|
| `claude_cli` | `claude -p`. Empty temp dir, no tools, replaced system prompt, `--json-schema`, no settings or MCP |
| `codex_cli` | `codex exec`. Read-only sandbox, user config and rules ignored, tools disabled, `--output-schema` |
| `gemini_api` | Gemini `generateContent` with `responseJsonSchema` and `thinkingLevel` |
| `anthropic_api`, `openai_api` | Messages / Responses API with structured output |

API backends refuse to run unless `ARENA_ALLOW_API=1` is set, and each one reads its key from the environment
(`GEMINI_API_KEY` and so on). CLI backends use the CLI's own login.

### Spend cap

`budget_usd` in the config sets a hard cap per backend. Before every call the runner reserves a worst-case
cost: the prompt at 3 characters per token, plus the maximum output. The call is refused if spend so far plus
held reservations plus this reservation would pass the cap. Actual cost is booked from the reported usage, with
thinking tokens billed as output. A timed-out API call is booked at its worst case. Spend is saved to
`<run>/budget.json` after every call and survives restarts. Prices come from the config and are list prices
only.

## Usage

```bash
python -m arena schedule configs/pilot.json          # the games, seats and seeds; no model calls
python -m arena smoke configs/pilot.json --out runs  # one tiny call per model (counts against the budget)
python -m arena run configs/pilot.json --out runs    # play; finished games are skipped on rerun
python -m arena check runs/<run_id>                  # replay every finished game from its logged decisions
python -m arena summary runs/<run_id>                # summary.json + summary.md
python -m arena render runs/<run_id>/<game> out.mp4  # video reconstruction (needs Pillow + ffmpeg)
python -m unittest tests.test_games                  # engines with random legal players
```

The schedule interleaves titles. Seat orders cycle through all permutations of the three models, and in
Spyfall the spy role cycles through the models. A rerun archives an aborted game's logs as `<game>.attemptN`
before playing it again.

## Output

```
<out>/<run_id>/budget.json
<out>/<run_id>/<game_id>/meta.json        seats, models, seed, rules version, prompt-template hash, CLI versions,
                                          code revision, status, winners, call counts and API cost
                         events.jsonl     every event: text, who could see it, full state snapshot after it
                         decisions.jsonl  every accepted decision (including private thoughts)
                         calls.jsonl      every model call: full user prompt, raw reply, tokens, cost, errors
                         result.json
```

## Accuracy of the videos

`arena/render.py` draws each frame only from the logged event and its state snapshot, plus the private note of
the decision that produced it. It never re-simulates. All text is shown verbatim, and fonts shrink to fit
instead of cutting text. `arena check` re-runs each game engine on the logged decisions and requires an exact
match with the logged events and result. That check ties the logs, and therefore the videos, to the decisions
the models actually made.

The view is omniscient. Hidden cards, roles and private notes are visible to the viewer and marked as hidden
from the other players.

## Limits

- The models' private notes are their stated reasoning, not a window into their computation.
- Small runs cannot rank models. A pilot of 6 games per title is for finding bugs and examples. Treat any win
  rate from it as anecdote.
- Players may recognise each other's writing style, even with neutral seat names.
