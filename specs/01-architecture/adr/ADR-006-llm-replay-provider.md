# ADR-006: LLM substitute — labelled ReplayLLMProvider, replay mode only

- **Status:** Accepted (2026-11-24, with amendments E1–E8)
- **Date:** 2026-11-24
- **Spec refs:** §6.1, §6.4, §12.2, §13.2, §15.2, §16.3, §19.1 rule 13

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
- Recordings are looked up by a hash of canonical key material: scenario, agent, task type, prompt version, **prompt content hash**, schema version, call ordinal, and input with volatile fields stripped. Full definition in spec §13.2.

### Amendments accepted at sign-off
- **E1: Finding aliases in every prompt (live and replay).** Finding IDs are generated per run, so a replayed Analysis output would cite IDs from the recording run and fail the anti-hallucination validator every time. Agents therefore show the model `F1…Fn` aliases, numbered in content-hash order, and map them back to real IDs after validation. An alias outside the range counts as `unknown_finding_reference`. Side benefits: one code path for live and replay, fewer tokens, and invented IDs are trivial to catch. Manifest IDs (entities, evidence) are stable and not aliased.
- **E2: Prompt content hash in the key.** `prompt_sha256` joins the key material, so editing a prompt without re-recording is a loud miss, not a silent pass against a stale recording. `prompt_version` stays for humans.
- **E3: Mode reported per agent.** `llm_mode` appears on the Agent Card, on `/readyz` and in every TaskResult. The UI badge is derived from all agents: `REPLAY` or `MIXED: <agent>=replay`. A mixed setup is allowed and always labelled.
- **E4: Objective locked in replay mode.** The UI locks the objective to the scenario template; the API rejects other values with `422 replay_objective_locked`.
- **E5: Oracle-gated recording.** `record_replay.py` validates the live run against the scenario oracle and refuses to save a failing recording.
- **E6: No runtime mode switch.** No endpoint or button changes mode; the only way is restarting agents with a new `NEXUS_LLM_MODE` (`make demo-replay`). A runtime toggle is the silent-fallback path this ADR forbids.
- **E7: Metrics and budget in replay.** LLM metrics and spans carry `nexus.llm.mode`. Replay reports the recorded usage (`usage.replayed: true`) so the budget path still runs. Latency is never simulated.
- **E8: Recording contents.** Key material, response body, usage and model ID only. No headers or auth. A test scans `data/replay/` for API-key patterns.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Recorded replay, labelled (chosen)** | Deterministic; honest; exercises the real downstream path | Recordings go stale when prompts or detectors change (caught loudly by E2 and the input hash; fixed by re-recording) |
| Live LLM in all tests | Most realistic | Flaky, slow, costly; CI needs a key |
| Hand-written fake responses | Easy | Violates the "no fake success" rule; drifts from real model behaviour |

## Consequences
- (+) Deterministic CI and a resilient demo, without misrepresentation.
- (+) Aliasing (E1) improves live mode too: shorter prompts, and invented IDs are easy to detect.
- (−) Recordings must be refreshed whenever a prompt, schema or detector output changes. A miss names the expected key, so the cause can be found by diffing the key material.
- (−) Interface additions: `llm_mode` on the Agent Card, `/readyz` and TaskResult; `usage.replayed`.

## Tests implied
- Unit: alias mapping round-trip; an out-of-range alias → `unknown_finding_reference`; key material excludes volatile fields and includes `prompt_sha256`.
- Integration: a replayed Analysis run passes the ID validator against fresh finding IDs; editing a prompt file → `ReplayMissError`; replay objective mismatch → `422`.
- Repo check: no API-key patterns in `data/replay/`; every recording's stored key material matches the current prompt hash (stale-recording report).
