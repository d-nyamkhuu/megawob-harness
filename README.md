# megawob-harness

Coding agents that build web pages for [MegaWoB](https://github.com/d-nyamkhuu/megawob), a set of seeded web tasks
for training computer-use agents. One model draws a design image for a task scenario. A coding agent (Codex or
Claude) rebuilds it as a MegaWoB site until MegaWoB's checker passes. Over 2,900 `design-*` sites were built this way.

No agent's own verdict accepts anything. The checker decides whether a site is done, a separate reviewer scores
how close it looks, and a Stop hook makes the agent run the checks itself before it may finish its turn.

```text
            ┌──────────── one thread, the site folder ──────────────┐
 brief ───► │ agent ──► render / check (MCP, outside the sandbox)    │
 + design   │   │            │ ledger: hash of the files → pass/fail │
   image    │   └─ stop? ──► Stop gate: last check passed on these  ─┼─► no: "call check()", continue the turn
            │                exact files?                            │
            └────────────────────────────┬───────────────────────────┘
                                         ▼ JSON report (+ check_token)
              gate: files, node --check, megawob.lint, megawob.check at 5 viewports ── fail ──► fix round
                                         │ pass                                   (same thread, fix model)
                                         ▼
              reviewer (another thread, no tools): fidelity 1-5, differences, frame issues
                                         ▼
              polisher (fresh thread, stronger model) ── gate + review again ── score dropped? ──► roll back
```

## Why it is built this way

Agents say "done" too early. They claim work they never verified, test with something weaker than the real check,
or stop at the time limit with a half-built page. The harness never asks the agent whether it succeeded.

- **The gate decides.** After every turn the full gate runs, whatever the agent reported: the site's files, `node
  --check`, `megawob.lint`, and `megawob.check` (the task's oracle plays every seed and randomization parameter) at
  all five training viewports. A failure goes back to the same thread as a fix round on a stronger model, with the
  exact failures, at most `--fix-rounds` times.
- **The agent must verify first, inside its turn.** Its `check` tool runs the quick check (lint, then the oracle at
  three viewports) and records the result against a hash of the site's files, in a ledger the agent cannot write.
  A Stop hook lets the turn end only if the last check passed on the files as they are now. Otherwise the hook's
  reason becomes the agent's next instruction. Fixing a failure inside the turn is far cheaper than a fix round,
  which re-runs the whole gate and re-reads the context.
- **The report must prove it.** A passing check prints a one-time token, and the report has to carry the token of
  a check that passed on the current files. An edit after the check voids it, and a report without a valid token
  is sent back like a gate failure.
- **Looks are judged by someone else.** The reviewer has no tools and never built the page. It scores the design
  image, the page beside it and the four instruction modes. A polish on a stronger model is kept only if the gate
  still passes and the score did not drop; otherwise the site's files are restored.
- **Everything is bounded and recorded.** Every turn has a wall-clock limit (an interrupt, not a kill). The Stop
  gate blocks at most three times per turn and never after the time limit. The run record has every turn's model,
  time, tokens, cost, tool calls, Stop-gate blocks and proof of check.

## Install

It builds into your MegaWoB checkout, so install that checkout editable first (its README has the details):

```bash
pip install -e path/to/megawob'[check]' && playwright install chromium
pip install -e .            # this repo
codex login                 # Codex: builds, reviews, design images
claude login                # Claude builds (--runtime claude), or set ANTHROPIC_API_KEY
```

You also need Node 22 or later (the gate runs `node --check`). Run every command from the root of the megawob
checkout.

## Stages

```bash
megawob-harness new use-email archive               # one new design image, then its site
megawob-harness design use-email archive --count 3  # only design images (reviewed)
megawob-harness build data/sitegen/designs/use-email/archive/07/design.json
megawob-harness card use-email                      # rebuild a task's capability card
megawob-harness status
```

| Stage | Output |
|---|---|
| `card <task>...` | `megawob_harness/cards/<task>.json`: what the task renders at the start of each scenario, what it never renders, 2-4 ways to lay out the task area |
| `design <task> <scenario>` | `<out>/designs/<task>/<scenario>/<NN>/`: `design.json`, `prompt.txt`, `attempt-*.png`, `design.png` |
| `build <design.json or site>...` | `html/sites/design-<task>-<scenario>-<NN>/`, `<out>/runs/<site>.json` |
| `new <task> <scenario>` | design, then build every accepted design |

`<out>` is `data/sitegen/` unless `--out` says otherwise. `--jobs N` runs designs or builds in parallel.

**Design images.** A design is one base screen at 1536x864, the largest training viewport, with no task note and no
end control, so it fits every episode setting. Designs of one task differ in structure, not only in colour:

- an era and visual language from `styles.json` (74 profiles, 1984 to 2026), the one the task has used least;
- a window arrangement from `arrangements.json` that the task has not used yet;
- a task-area layout from the card;
- a real goal sampled from the env.

Codex's image tool draws the design, and a separate turn reviews it against a checklist: accept, revise or redraw.

**Building.**

1. The scaffold copies the base site, with designs on its templates, so the page passes the checker before the
   agent changes anything.
2. The agent works in the site folder only:
   - Codex runs in its workspace-write sandbox with approvals denied.
   - Claude gets the file tools and no shell, and a hook denies writes outside the folder.
3. Its tools come from an MCP server outside the sandbox (`tools.py`):
   - `render` shows the design beside the page, crops, the small viewports and the four instruction modes. It can
     use any seed, parameter or popup, and replay the oracle's first N actions.
   - `check` runs the quick check.

The brief is `prompts/build.md`. The report schema is `prompts/result.schema.json` plus `check_token`.

## Models and cost

| Option | Default | Used for |
|---|---|---|
| `--runtime` | codex | builds, fix rounds and polish (`codex` or `claude`; set the models to match) |
| `--review-runtime` | codex | design and site reviews, capability cards |
| `--model` | gpt-6-luna | the build turn |
| `--fix-model` | gpt-6.1-terra | fix rounds (same thread, model switched) |
| `--polish-model` | gpt-6.1-sol | the polish pass (`--no-polish` skips it) |
| `--review-model` | gpt-6.1-sol | reviews and cards |
| `--image-model` | gpt-6-luna | relays the prompt to Codex's image tool |

A cheap model does the long build, a mid-size one fixes what the checker finds, and a strong one polishes and
reviews. `--no-stop-gate` lets the agent end a turn without a passing check (the gate still runs).

## Tests

```bash
pytest -m "not megawob"            # checks, Stop gate, runtime config: seconds, no model, no MegaWoB build
pytest -m megawob                  # the pipeline on a real stand-in site with a scripted agent: ~3 minutes
HARNESS_LIVE=1 pytest tests/test_live.py   # the Stop gate on real Codex and Claude agents
```

The `megawob` tests scaffold `design-set-slider-horizontal-99` in your checkout and run the real gate. Afterwards
they delete the site and restore `html/scenarios.json`. They cover:

- a checked build passing in one turn;
- the Stop gate catching an unchecked claim inside the turn;
- the same claim costing a fix round when the gate is off;
- a broken `site.js` going back to the fix model;
- a polish that the reviewer scores lower being rolled back.

## Results

The Stop gate, live on a real site (`tests/test_live.py`). The agent got a scaffolded `set-slider` site and was told
to report "done" at once without checking:

| Runtime | Stop-gate blocks | `check` calls | Result | Turn | Cost |
|---|---|---|---|---|---|
| Claude (Agent SDK) | 1 | 1 | gate passed, valid token, no fix round | 21 s | $0.07 |
| Codex (app-server) | 1 | 1 | gate passed, valid token, no fix round | 31 s | ChatGPT plan |

The over 2,900 existing `design-*` sites were built by this pipeline before the Stop gate and the check token
existed. Their failure modes motivated the gate: agents that stopped early and rated their own work 4 of 5 failed
the independent review 4 times in 6. How much the gate saves on full builds (fix rounds, time, tokens) is not
measured yet; each run record has what is needed to measure it.

The first live runs found two bugs that the scripted tests had missed:

- Both SDKs start MCP servers in the agent's working directory, which broke relative paths.
- Playwright's sync API allows only one instance per thread.

## Implementation notes

- **Runtimes** (`runtime/`):
  - Codex goes through its app-server SDK over JSON-RPC: typed items, ephemeral threads, an interrupt at the time
    limit.
  - Claude goes through the Claude Agent SDK, on an event loop thread of its own, with no user settings loaded.
  - A replay runtime scripts the agent's moves for tests.
- **Codex hook trust.** Codex runs only hooks the user has trusted, keyed by a hash it reports in `hooks/list`, and
  reads that trust only from the user config; a session override cannot set it. A build with the Stop gate
  therefore runs in a throwaway `CODEX_HOME`: a copy of your `config.toml`, a link to your `auth.json`, and a trust
  entry for the harness's own hook. Nothing lands in your Codex home.
- **Tool servers start in the site folder.** Both SDKs start MCP servers in the agent's working directory, so the
  server takes absolute paths from its spec.
- **JSON answers.** Schemas go in the prompt and are validated locally with a retry. Strict output schemas made
  some reasoning models stall after their reasoning item.
- **No agent framework.** The pipeline is a plain loop over files: a site folder and a run record per build.

## Changing the workflow

- **Guidance for one task or scenario:** the scenario's `hint` or `never` list in `cards/<task>.json`.
- **A new task:** `megawob-harness card <task>`. The task needs a widget template for the scenario in megawob's
  `html/templates/<task>/`.
- **More styles or arrangements:** `styles.json`, `arrangements.json`.
- **Prompts:**
  - the agent's brief: `prompts/build.md`;
  - the image prompt and its review: `prompts/image.txt`, `prompts/review.txt`;
  - the site review: `prompts/site_review.txt`.

## Background reading

- OpenAI, [Harness engineering](https://openai.com/index/harness-engineering) (February 2026)
- Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents):
  agents test end to end only when made to
- [Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents](https://arxiv.org/abs/2609.00006)
- [Agentic Harness Engineering](https://arxiv.org/abs/2604.25850)

## License

MIT
