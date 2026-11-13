# Decision record: removing the Stop gate

**Decision:** the harness no longer makes the agent verify its own work before it may stop. The Stop gate (a Stop
hook that refused to end the agent's turn until its own `check()` had passed on its current files), the check
ledger behind it and the proof-of-check token in the agent's report are gone. The external gate, which runs after
every turn whatever the agent reports, is what decides.

**Why:** measured on full builds, the Stop gate cost more than it saved. It saved a fix round when the build model
could fix its own failures (the slider), and cost time when it could not (the form and the email app): it kept the
cheap model grinding at failures that the stronger fix model, reached sooner without the gate, cleared in one to
three minutes. The pipeline's model tiering already turns an early stop into a cheap escalation. Three designs, one
run per condition: a small sample, but the mechanism is clear in every run record.

## Setup

- **Designs:** three new design images drawn and reviewed by the pipeline (`megawob-harness design`), one each for
  `set-slider/horizontal`, `fill-form/invoice` and `use-email/archive`.
- **Conditions:** each design built twice from scratch, with the Stop gate and without it. Everything else equal:
  the brief tells the agent to call `check()` and to report its token in both conditions, and the harness requires a
  valid token in both. The only difference is whether the agent can end its turn before a check has passed on its
  final files.
- **Models (Codex, ChatGPT plan):** build `gpt-6-luna`, fix rounds `gpt-6.1-terra`, review `gpt-6.1-sol`, default
  reasoning effort. No polish pass.
- **Budget:** 20 minutes for the first turn (the default is 60), 10 minutes per fix round, at most 2 fix rounds,
  the full gate at 5 seeds and 5 viewports. The order of the two conditions alternated between designs.
- **Isolation:** every Codex process ran in a throwaway `CODEX_HOME` with only the login, so neither condition saw
  the user's Codex config, `AGENTS.md`, plugins or MCP servers. (This experiment found that leak; the fix stays in
  `runtime/codex.py`.)

The experiment script and the Stop gate were removed together after this run, so the records here are the
evidence; about 30 minutes of builds with 3 jobs produced them.

## Results

All six builds ended with a site that passes the full gate.

| Design | Condition | Fix rounds | Minutes | `check` calls | Stop-gate blocks | Tokens (output) | Review |
|---|---|---|---|---|---|---|---|
| Slider | **with Stop gate** | **0** | **3.8** | 1 | 0 | 0.48 M (7.2 k) | 2 |
| Slider | without | 1 | 7.5 | 12 | | 1.58 M (11.2 k) | 2 |
| Form | with Stop gate | 2 | 18.5 | 19 | 4 | 8.05 M (37.7 k) | 1 |
| Form | **without** | **1** | **7.8** | 9 | | 2.90 M (11.0 k) | 2 |
| Email app | with Stop gate | 1 | 21.9 | 23 | 2 | 7.59 M (44.7 k) | 1 |
| Email app | **without** | 2 | **7.2** | 9 | | 1.89 M (10.6 k) | 2 |
| **All three** | with Stop gate | 3 | 44.2 | 43 | 6 | 16.1 M (89.5 k) | |
| | without | 4 | 22.5 | 30 | | 6.4 M (32.9 k) | |

Minutes are wall-clock to an accepted site, including every gate run. Tokens are Codex's totals for the build thread
(mostly cached input). Review is the independent reviewer's fidelity score, 1 to 5.

## What happened

- **Slider, with the gate:** one check, which passed, and a report with its token. Accepted in one turn.
- **Slider, without:** nine checks in the first turn, but the agent stopped on files no check had passed. The gate
  found 17 failures; the fix model cleared them in 87 s.
- **Form, with the gate:** the cheap model could not make `check()` pass. The Stop gate blocked it three times (its
  limit), the agent ran 13 checks in 13 minutes, then reported honestly that its last check still failed. The fix
  model fixed the layout in one round, and a second round went to a report of `done: false`.
- **Form, without:** four checks, a stop with 17 failures after 4 minutes, one fix round of 3 minutes.
- **Email app, with the gate:** 22 checks and 27 renders, until the 20-minute limit interrupted the turn before it
  wrote a report. The site itself passed the full gate; the fix round was spent on the missing report and token.
- **Email app, without:** a stop after 4 minutes with 17 failures; the fix model cleared them in 45 s, and a second
  round went to a report of `done: false`.

## What it means for the pipeline

1. **The gate makes the agent honest, not capable.** With the gate, every agent finished with a valid proof, said
   plainly that it could not, or was still checking at the time limit. Without it, all three first turns stopped on unverified files.
2. **With model tiering, an early stop is cheap.** The harness answers a failed gate with a stronger model. Holding
   the cheap model in its turn delays that escalation; the 2.5x token cost comes from the cheap model's long turns.
3. **What changed:** the Stop gate, the ledger and the token are removed; the agent keeps a plain `check()` tool,
   and the brief still asks it to check before it reports. Two of the seven fix rounds were also spent only on
   `done: false` from a model whose site already passed; a fix round now only has to make the gate pass (the
   polish pass is where open visual differences get worked on).
4. **Fidelity is low in every run** (1 to 2 of 5): a 20-minute budget with no polish is far from the default. The
   shots below are what such a build looks like, not what the 2,942 published sites look like.

The run records are in `runs/`, the reviewer's verdicts inside them, and the design image beside each built page in
`shots/`:

![Design image (left) and the page built without the Stop gate (right), for the email app](shots/design-use-email-archive-06.no-stop-gate.webp)
