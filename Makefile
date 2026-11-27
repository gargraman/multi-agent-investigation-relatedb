# Nexus developer commands (spec §16.3).
# Targets for slices not built yet print a notice and EXIT NON-ZERO (spec §19.1 rule 13:
# never fake success).

AGENT ?= sigint-agent
UV ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

.DEFAULT_GOAL := help
COMPOSE = docker compose -f infrastructure/docker/compose.yaml --env-file .env


.PHONY: help sync test lint fmt schemas schemas-check \
        secrets up down ready reset spike-linux up-obs demo demo-replay backup e2e e2e-replay \
        logs smoke-live chaos

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install all workspace members + dev tools into .venv
	$(UV) sync --all-packages

test: ## Run pytest across all members (includes schema drift check)
	$(UV) run pytest

lint: ## ruff check + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Auto-fix lint and format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

schemas: ## Regenerate contracts/schemas/*.schema.json
	$(UV) run python scripts/gen_schemas.py

schemas-check: ## Fail if committed schemas drift from the Pydantic models
	$(UV) run python scripts/gen_schemas.py --check

# --- Compose stack (S1) -----------------------------------------------------------------
secrets: ## Generate per-service tokens into .env (git-ignored; values never printed)
	$(UV) run python scripts/gen_secrets.py

.env:
	@echo "No .env — run: make secrets" >&2; exit 1

up: .env ## Core app, lite profile; waits until every /readyz is green (AC1)
	$(COMPOSE) up -d --build --wait
	@$(MAKE) --no-print-directory ready

down: ## Stop the stack (volumes kept)
	$(COMPOSE) down

ready: ## AC1 check: every core service healthy and /api/v1/system/health = OK
	@scripts/check_ready.sh

logs: ## Tail all services
	$(COMPOSE) logs -f --tail=100

reset: ## Stop and WIPE the Relate store + SQLite volumes (asks first)
	@read -p "Wipe all Nexus volumes (Relate store, SQLite)? [y/N] " ans; [ "$$ans" = "y" ] || { echo aborted; exit 1; }
	$(COMPOSE) down -v

# Host-side LLM tooling: loads .env into the process (values never echoed).
WITH_ENV = set -a && . ./.env && set +a &&

verify-openai: .env ## (S2) List models + strict-schema probe for OPENAI_MODEL_EXTRACT
	@$(WITH_ENV) uv run python scripts/verify_openai.py

record-replay: .env ## Oracle-gated live recording: make record-replay SCENARIO=contradiction [AGENT=geoint-agent]
	@test -n "$(SCENARIO)" || { echo "usage: make record-replay SCENARIO=<id>" >&2; exit 2; }
	@$(WITH_ENV) uv run python scripts/record_replay.py --scenario $(SCENARIO) --agent $(AGENT)

REPLAY_AGENTS = sigint-agent geoint-agent
E2E_BASE_URL ?= http://127.0.0.1:$(or $(NEXUS_UI_PORT),3000)

# Needs the stack up (`make up`). Restarts only the two agents with NEXUS_LLM_MODE=replay, runs
# scripts/e2e_replay.py over the 3 scenarios, prints an agent-log check (no OpenAI provider calls,
# no replay misses), and ALWAYS restores the agents to the default mode afterwards (trap on exit),
# so `make up` semantics are unchanged. Reads no secrets beyond what compose already uses.
e2e-replay: .env ## (S3a) agents in replay mode, 3-scenario E2E; restored in replay unless NEXUS_LLM_MODE=live
	@set -e; \
	restore() { mode="$${NEXUS_LLM_MODE:-replay}"; \
	  echo "--- restoring $(REPLAY_AGENTS) with NEXUS_LLM_MODE=$$mode"; \
	  NEXUS_LLM_MODE="$$mode" $(COMPOSE) up -d --no-deps --force-recreate --wait $(REPLAY_AGENTS) >/dev/null 2>&1 \
	    || echo "WARNING: could not restore $(REPLAY_AGENTS); run: make up" >&2; }; \
	trap restore EXIT; \
	echo "--- restarting $(REPLAY_AGENTS) with NEXUS_LLM_MODE=replay"; \
	NEXUS_LLM_MODE=replay $(COMPOSE) up -d --no-deps --force-recreate --wait $(REPLAY_AGENTS); \
	scripts/check_ready.sh; \
	since=$$(date -u +%Y-%m-%dT%H:%M:%SZ); \
	rc=0; $(UV) run python scripts/e2e_replay.py --base-url $(E2E_BASE_URL) || rc=$$?; \
	logs=$$($(COMPOSE) logs --no-color --since $$since $(REPLAY_AGENTS) 2>&1); \
	ready=$$($(COMPOSE) logs --no-color $(REPLAY_AGENTS) 2>&1 | grep 'agent ready' | tail -n 2); \
	live=$$(printf '%s\n' "$$logs" | grep -c 'llm call done' || true); \
	miss=$$(printf '%s\n' "$$logs" | grep -c 'replay miss' || true); \
	done_n=$$(printf '%s\n' "$$logs" | grep -c '"task finished"' || true); \
	echo "--- agent logs since $$since"; \
	printf '%s\n' "$$ready" | sed -E 's/.*"service": "([a-z-]+)".*"llm_mode": "([a-z]+)".*/  \1 ready, llm_mode=\2/'; \
	echo "  task finished: $$done_n   OpenAI calls (llm call done): $$live   replay misses: $$miss"; \
	if [ "$$live" != 0 ] || [ "$$miss" != 0 ]; then echo "FAIL: model calls or replay misses in replay E2E" >&2; rc=1; fi; \
	if [ "$$done_n" -lt 6 ]; then echo "FAIL: $$done_n task(s) finished in the agent logs, expected >= 6 (2 agents x 3 scenarios)" >&2; rc=1; fi; \
	exit $$rc

# Real containers, replay mode, compose.chaos.yaml (deadline 45 s, 20 s replay delay). Breaks one
# thing per scenario (stop/restart/kill) and asserts recovery. ~5-6 min. Restores the default
# config on exit, like e2e-replay. Subset: make chaos CHAOS_SCENARIOS="ac12a ac13c".
CHAOS_SCENARIOS ?=
chaos: .env ## (S3b) AC12-a, AC13-a/b/c against real containers in replay mode
	@scripts/chaos.sh $(CHAOS_SCENARIOS)

spike-linux: .env ## ADR-001 R5: Relate spike suite inside the Linux Context Service image
	scripts/spike_linux.sh

# --- Not implemented yet: each exits non-zero -------------------------------------------
define not_implemented
	@echo "make $@: not implemented yet — arrives in slice $(1)" >&2; exit 1
endef

up-obs: ## (S6) core + Grafana LGTM
	$(call not_implemented,S6)
demo: ## (S6) up-obs + open UI
	$(call not_implemented,S6)
demo-replay: ## (S2, Good-to-have) restart agents in replay mode
	$(call not_implemented,S2)
backup: ## (S1, Good-to-have) relate clone of the store to ./backups/
	$(call not_implemented,S1)
e2e: ## (S5) Playwright + chaos + telemetry tests
	$(call not_implemented,S5)
smoke-live: ## (S2, Good-to-have) live-LLM smoke test
	$(call not_implemented,S2)
