# AGENTS.md -- rust-agent

Guide for AI coding assistants working in this project. Humans: see README.md /
README_ZH.md.

## What this is

A complete, multi-file Rust agent for the GOSIM survey26 hackathon's
telescope-survey challenge. It speaks `participant-agent-protocol-v4` (one
JSON object per line on stdin/stdout), runs a deterministic anchor-search
planner, and calls an LLM twice per night for forecast/bulletin advice. Read
`src/main.rs` first -- it is the entry point that drives the decision loop.

## Module map

| File | Responsibility |
|---|---|
| `src/main.rs` | Reads `initialize`, then runs the decision loop; logs to stderr. |
| `src/protocol.rs` | Serde types for the wire format; permissive JSON-Lines read/write. |
| `src/state.rs` | One-time config snapshot from `initialize`: catalogue, night calendar, visibility windows, spatial index. |
| `src/memory.rs` | What the agent has learned from its own feedback: per-target progress, learned sky scale, bulletins, timed observation requests, the clean-sky history behind fault detection, the JSONL trace, and the stderr logger. |
| `src/planner.rs` | Turns one `decision_request` into one `decision_response`: anchor search, band-coherent fibre fill, critical-duration ladder, required-target and timed-request bonuses, the instrument-fault report chain, pointing-offset probing, and the pace governor. |
| `src/llm.rs` | OpenAI-compatible chat client (default: Kimi Coding Plan), with retries and a run-wide budget. |
| `src/scoring.rs` | Public sky geometry + scoring formulas (no scenario data). |
| `src/validate.rs` | Protocol-rule validation (including the consecutive-report limit) and the deterministic safe fallback. |

## Changing the strategy

Target ranking, fibre filling and exposure sizing live in `src/planner.rs`.
The two LLM-advised calls (forecast avoidance + bulletin check-in) are issued
once per night from `planner.rs` (`night_advice`) through `llm.rs`
(`LlmClient::ask_json`); a third, rarer call confirms a suspected instrument
fault (`maybe_report`). The nightly advice is parsed, evidence-checked and
**logged only -- never applied** (`extra_avoid` is always emptied,
`duration_scale` is pinned to 1.0), so the decision sequence stays
deterministic; keep it that way unless you re-validate against the Python
behaviour source (`kit/python/agent_core/`, which this port tracks
feature-for-feature). You can change the ranking heuristic, add signals to
`state.rs`/`memory.rs`, change what (if anything) the LLM is asked, or replace
the LLM calls entirely -- the only hard requirement enforced by `validate.rs`
is that whatever `main.rs` writes to stdout is a protocol-legal response.

## Configuring the LLM (Kimi key / base URL / model)

Copy `.env.example` to `.env` and set:

- `OPENAI_API_KEY` (or `KIMI_API_KEY`) -- required to run the bundled planner
  as-is; with neither set, the agent logs `missing API key: set
  OPENAI_API_KEY` and exits before reading anything from stdin.
- `OPENAI_BASE_URL` -- defaults to the Kimi Coding Plan endpoint
  (`https://api.kimi.com/coding/v1`; use `https://api.kimi.ai/coding/v1`
  outside mainland China) when unset.
- `OPENAI_MODEL` -- defaults to `k3` when unset.

Any other OpenAI-compatible `/chat/completions` endpoint works too -- just
point `OPENAI_BASE_URL` / `OPENAI_MODEL` at it. On the platform,
`OPENAI_BASE_URL` / `OPENAI_API_KEY` are injected automatically for every run
(the platform's own model proxy and a temporary credential); `.env` must never
be uploaded in a submission.

## Packing and uploading as a complete project

```sh
cargo build --release --locked
```

`observer.project.json` at the root already declares the build and run steps
for the platform (`cargo build --release --locked`, then
`./target/release/rust-agent`), so the platform compiles it for you. Upload
this folder as a ZIP (excluding `target/` and `.env`), or push it to a GitHub
repository and submit the repo URL instead.

## Protocol & scoring

Full protocol reference and scoring formulas are not duplicated here -- see
`docs/` (bundled in the downloadable ZIP of this example) for the complete
participant guide in Chinese and English.
