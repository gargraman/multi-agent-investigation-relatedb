# ADR-006: LLM substitute — labelled ReplayLLMProvider, replay mode only

- **Status:** Proposed
- **Date:** 2026-11-24
- **Spec refs:** §13.2, §19.1 rule 13

## Context
- The spec forbids mock success paths, yet E2E tests must be deterministic and must not depend on OpenAI availability or cost.
- A live demo also fails if OpenAI is unavailable or rate-limited.

## Decision
- `ReplayLLMProvider` serves responses recorded from **real** OpenAI runs, captured by `scripts/record_replay.py` into `data/replay/<scenario>/`.
- It is selected only by `NEXUS_LLM_MODE=replay`. It is never a silent fallback.
- When replay is active:
  - the UI shows a persistent "REPLAY MODE" badge;
  - every replayed call is marked in the timeline and in spans (`nexus.llm.mode=replay`);
  - findings carry `model_id` with a `replay:` prefix.
- Replay responses still pass the same Pydantic validation, persistence and policy paths. Only the model call is substituted.
- A missing recording raises an error; it never falls back to a fabricated response.
- Recordings are looked up by a hash of canonical key material (scenario, agent, task type, prompt and schema version, call ordinal, input with volatile IDs/timestamps stripped). Full definition in spec §13.2 (v1.2).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Recorded replay, labelled (chosen)** | Deterministic; honest; exercises the real downstream path | Recordings go stale when prompts change (re-record on prompt version bump) |
| Live LLM in all tests | Most realistic | Flaky, slow, costly; CI needs a key |
| Hand-written fake responses | Easy | Violates the "no fake success" rule; drifts from real model behaviour |

## Consequences
- (+) Deterministic CI and a resilient demo, without misrepresentation.
- (−) Recordings must be refreshed whenever `prompt_version` changes; a test checks that each recording's prompt version matches.
