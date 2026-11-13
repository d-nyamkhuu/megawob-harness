# agent-harness

A harness for coding agents in which no agent's own verdict accepts anything. Deterministic checks decide whether
the work is done, a separate reviewer scores it, and a Stop hook makes the agent run the checks itself before it may
finish.

It runs the same job on **Codex** (through the app-server SDK) or **Claude** (through the Claude Agent SDK). It came
out of [MegaWoB](https://github.com/d-nyamkhuu/megawob), where it built over 2,900 web pages from design images, each
one gated by a browser-based checker.

```text
            ┌──────────── one thread, one workspace ────────────┐
 brief ───► │ agent ──► render / check (MCP, outside sandbox)    │
            │   │            │ ledger: hash of files → pass/fail │
            │   └─ stop? ──► Stop gate: last check passed on    ─┼─► no: "call check()", continue the turn
            │                these exact files?                │
            └───────────────────────────┬───────────────────────┘
                                        ▼ report (+ check_token)
                  gate (the domain's checks) ── fail ──► fix round, same thread, fix model
                                        │ pass
                                        ▼
                  reviewer (another thread, no tools): fidelity 1-5 + differences
                                        ▼
                  polisher (fresh thread) ── gate + review again ── score dropped? ──► roll back
```

## Why it is built this way

Agents say "done" too early. They claim work they never verified, test with something weaker than the real check,
or stop at the time limit with a half-built page. The harness never asks the agent whether it succeeded:

- **The gate decides.** After every turn the domain's own checks run, whatever the agent reported. A failure goes
  back to the same thread as a fix round (on a stronger model if you like) with the exact failures, at most
  `fix_rounds` times.
- **The agent must verify first, inside its turn.** Its `check` tool runs the domain's quick checks and records the
  result against a hash of the workspace in a ledger the agent cannot write. A Stop hook lets the turn end only if
  the last check passed on the files as they are now; otherwise the hook's reason becomes the agent's next
  instruction. Fixing a failure inside the turn is far cheaper than a fix round, which re-runs the whole gate and
  re-reads the context.
- **The report must prove it.** A passing check prints a one-time token. The report has to carry the token of a check
  that passed on the current files. An edit after the check voids it, and a report without a valid token is sent
  back like a gate failure. This works on any runtime, hooks or not.
- **Looks are judged by someone else.** A reviewer runtime with no tools scores the result from images. A polish pass
  on a stronger model is kept only if the gate still passes and the score did not drop; otherwise the workspace is
  restored.
- **Everything is bounded and recorded.** Every turn has a wall-clock limit (an interrupt, not a kill), the Stop gate
  blocks at most three times per turn and never after the time limit, and the run record has every turn's model,
  time, tokens, cost, tool calls, Stop-gate blocks and proof of check.

## The parts

| Part | File | What it does |
|---|---|---|
| Runtime interface | `agent_harness/runtime/base.py` | `Session.turn` (multi-turn, model switch per turn), `ask` (JSON from a read-only turn), `image` |
| Codex runtime | `runtime/codex.py` | app-server over JSON-RPC, workspace-write sandbox, ephemeral threads, Stop gate as a trusted command hook |
| Claude runtime | `runtime/claude.py` | Agent SDK with file tools only, a PreToolUse hook that denies writes outside the workspace, Stop gate as a callback |
| Replay runtime | `runtime/replay.py` | scripted agent moves, for tests without a model |
| Checks | `agent_harness/checks.py` | the workspace hash, the ledger, the `check` tool, the Stop gate and its hook command |
| Tools | `agent_harness/tools.py` | the domain's tools plus `check`, served over MCP (stdio) outside the agent's sandbox |
| Domain | `agent_harness/domain.py` | what a kind of work needs: workspace, brief, tools, quick check, gate, review |
| Pipeline | `agent_harness/pipeline.py` | build, gate and fix, review, polish with rollback, run record |

### Writing a domain

```python
from agent_harness import Domain

class MyDomain(Domain):
    def workspace(self, job): ...       # the folder the agent may write
    def prepare(self, job): ...         # create it (a scaffold that already passes the checks is ideal)
    def brief(self, job, minutes): ...  # the first instruction
    def tools(self, job): ...           # typed, documented functions served over MCP (run outside the sandbox)
    def quick_check(self, job): ...     # failures as strings; behind the agent's check()
    def gate(self, job): ...            # failures as strings; decides acceptance
    def review(self, job, reviewer): ...  # optional: reviewer.ask(prompt, REVIEW_SCHEMA, images=[...])
```

The domain is rebuilt in the tool server's process from its import path and `config`, so keep both JSON-friendly.

## Getting started

```bash
uv venv && uv pip install -e '.[dev]'
playwright install chromium
codex login            # for --runtime codex
claude login           # for --runtime claude (or ANTHROPIC_API_KEY)
pytest                 # 27 tests, no model, no API key
```

Run the example: rebuild a pricing page from its screenshot, HTML and CSS only.

```bash
agent-harness run --domain examples.static_page.domain:StaticPage \
    --job examples/static_page/jobs/pricing.json --runtime claude --review-runtime codex
agent-harness status
```

The example's gate also blocks the shortcuts a model might take. A page that pastes the screenshot as an image, uses
a data URI, a script or a network request is rejected, even though it would score perfectly on similarity.

## Results

Live runs on the example (one pricing page, 10-minute limit, no polish, each runtime reviewing its own build):

| Runtime | Gate | Turns | `render` / `check` calls | Reviewer fidelity | Time | Cost |
|---|---|---|---|---|---|---|
| Claude (Agent SDK) | passed | 1 | 1 / 1 | 4 | 0.5 min | $0.09 |
| Codex (app-server) | passed | 1 | 9 / 1 | 5 | 3.2 min | ChatGPT plan |

The Stop gate, live (`HARNESS_LIVE=1 pytest tests/test_live.py`): the brief tells the agent to write the answer and
report at once without checking. On both runtimes the hook blocked the stop, the agent called `check` inside the
same turn, and the work was accepted with a valid token and no fix round.

Two bugs the live runs found that the unit tests had not:

- Both SDKs start MCP servers in the agent's workspace. The tool server now runs from, and imports from, the
  harness's directory.
- Playwright's sync API allows one instance per thread. The example's tools now share one browser.

## Implementation notes

- **Codex hook trust.** Codex runs only hooks the user has trusted, keyed by a hash it reports in `hooks/list`, and
  reads that trust only from the user config; a session override cannot set it. A session with a Stop gate
  therefore runs in a throwaway `CODEX_HOME`: a copy of the user's `config.toml`, a link to their `auth.json`, and a
  trust entry for the harness's own hook. Nothing lands in the user's Codex home.
- **JSON answers.** Schemas go in the prompt and are validated locally with a retry. Strict output schemas made
  some reasoning models stall after their reasoning item.
- **No framework.** The pipeline is a plain loop over files. A source-code study of eleven coding harnesses
  ([arXiv 2609.00006](https://arxiv.org/abs/2609.00006)) found the same: none imports a general agent framework.
- **State is files.** A workspace and a run record per job, so any job can be rerun or inspected alone.

## Background reading

- OpenAI, [Harness engineering](https://openai.com/index/harness-engineering) (February 2026)
- Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents):
  agents test end to end only when made to
- [Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents](https://arxiv.org/abs/2609.00006)
- [Agentic Harness Engineering](https://arxiv.org/abs/2604.25850): most gains come from tools and middleware, not
  the system prompt

## License

MIT
