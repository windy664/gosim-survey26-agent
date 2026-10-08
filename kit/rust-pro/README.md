# rust-pro -- python-pro, ported to Rust

Current robustness changes: logbook batches are acknowledged only after a valid decode, so slow or
rejected model calls cannot discard later notes. Corrections under an existing request ID are retained.
Coarse search derives central fibres from the supplied instrument geometry, and observation/wait
actions respect its exposure limits. These changes preserve L1-L4 scores; they are not evidence of
higher hidden-card scores. Reproduction tests live at `../../tools/check_intel_delivery.py` (local fake
model) and `../../tools/check_instrument_contract.py` (generated instrument variations).

`PRO_SCALE_INDEPENDENT=1` enables an experimental fault detector using fresh absolute-quality evidence,
even when no healthy program-band baseline exists. It remains off by default. Controlled tests and a
second public geometry found earlier repair without additional false reports; L1-L4 scores were unchanged.
Run `../../tools/check_fault_generalization.py` to reproduce; full figures are in
`../../docs/validation/robust2-local.json`. Authentication/balance/access errors (401/402/403) now disable
new model calls for the current process instead of consuming the run in repeated failures.

[中文说明见 README.zh.md](README.zh.md)

A line-by-line Rust port of the strong reference agent [`../python-pro`](../python-pro/README.md) for the
GOSIM survey26 telescope-survey challenge (`participant-agent-protocol-v4`). Same strategy, same constants,
same order of operations, same three model stages; the strategy itself is explained in
[python-pro's README](../python-pro/README.md). It only uses what the protocol gives an agent at run time
(catalogue, public score configuration, bulletins, forecasts and its own results) and never reads card files.

What the port changes is speed: a decision costs about 1/30 of the CPU time of the Python version, so on the
platform's fair clock the planner always runs its full search (search level 0) even on long cards.

## Results (local engine, deterministic stub model)

| Card | python-pro | **rust-pro** | CPU charged (python-pro / rust-pro) |
|---|---:|---:|---:|
| local L1 | 6,121.4 | **6,121.4** | 101-130 s / 5 s |
| local L2 | 6,392.7 | **6,392.7** | 101-118 s / 5 s |
| local L3 | 6,796.4 | **6,796.4** | 166-202 s / 6-7 s |
| local L4 | 6,799.3 ± 0.1 | **6,789.3** | 170-201 s / 6-7 s |
| starter-kit demo | 1,717.8 | **1,717.8** | 22-31 s / 1 s |

Three runs per card with `run_local.py` and the normal 900 s fair clock; the model is a local stub that
answers deterministically from the request (same answers for both agents), so the runs differ only through
pacing. With the search level pinned (`PRO_FIXED_LEVEL=0`) the two agents send identical decision sequences
on all five cards. On L4 python-pro briefly dropped to the cheaper search level 1 to stay inside its CPU
budget, which happened to gain 10 points; rust-pro never needs to. With a real model (Kimi `k3`) scores
vary from run to run by much more than this (see python-pro's README).

## Layout (one file per python-pro module)

```
src/main.rs        agent.py       entry point: protocol loop, pacing, instrument-fault reporting, model stages wiring
src/planner.rs     planner.py     one search for pointing + fibres + duration + program; learning from results
src/skymath.rs     skymath.py     public sky maths: sidereal time, alt/az, gnomonic projection, fibre grid, Moon
src/advisor.rs     advisor.py     the model stages: night plan, fault review, paid-report confirmation
src/llm_client.rs  llm_client.py  OpenAI-compatible chat client running calls on background threads
observer.project.json   platform manifest (cargo build --release --locked; ./target/release/rust-pro)
pack_agent.py      zip this folder for upload (target/ and .env are never packed)
.env.example       copy to .env and set an API key for local runs
```

In short (details in python-pro's README): one search picks pointing, fibres, duration and program to
maximise `gain - lambda * T` from value anchors plus density anchors with a refinement step; the band level is
fitted to saturated hits; instrument faults are detected from `E = quality level / band level` with free and
paid probe rules and special handling after earthquake notices (no probe for 12 h, then only on a new step
down); a hidden pointing offset is estimated from fibre hits and misses on a grid scaled to the fibre pitch;
required targets are attempted when their sky is near its best and the night is not forecast bad;
observation requests get all-or-nothing value. Pacing uses the fair clock
(`wallclock.remaining_real_cpu_seconds` against the process's own CPU time from `getrusage`, plus the real-time
cap). The model (default Kimi `k3`) is asked twice at every night start (night plan; fault review) and once
before a paid report, always on background threads, so it never blocks a decision.

## Configuration (.env)

```
OPENAI_API_KEY=sk-...                            # required (KIMI_API_KEY also accepted)
OPENAI_BASE_URL=https://api.kimi.com/coding/v1   # default; outside mainland China: https://api.kimi.ai/coding/v1
OPENAI_MODEL=k3                                  # default
```

Without a key the agent exits at start-up with `missing API key: set OPENAI_API_KEY`, except under `OBSERVER_MODEL_DISABLED=1` (set by the platform for an evaluation started with “This evaluation without a model” / `survey26 eval start --no-model`): then it needs no key and runs on its rules only, so you can compare with and without an LLM. On the platform the
team's variables are the program's environment. No temperature is sent (`k3` accepts only its default).
HTTP 429/5xx answers and network errors are retried with backoff.

## Building and running locally

```bash
cargo build --release --locked
python3 ../_local/runner/run_local.py --inherit-env --card ../_local/cards/L1 --agent "./target/release/rust-pro" --agent-cwd .
python3 pack_agent.py --out ../rust-pro-agent.zip
```

Every constant can be overridden with the same `PRO_<NAME>` environment variables as python-pro (for example
`PRO_LAMBDA_FRAC=0.5`; `PRO_FIXED_LEVEL=0` pins the search level). The platform builds inside a read-only
container, so `observer.project.json` points `CARGO_HOME` at `/tmp`.

## License

Task cards, simulated data, evaluation code and the example projects are licensed under
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/); please cite the GOSIM 2026 Agentic
Observer Hackathon (https://create.gosim.org/survey26/). See `../LICENSE.md`.
