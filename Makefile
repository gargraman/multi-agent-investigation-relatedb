# Nexus developer commands (spec §16.3).
# Targets for slices not built yet print a notice and EXIT NON-ZERO (spec §19.1 rule 13:
# never fake success).

UV ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

.DEFAULT_GOAL := help
COMPOSE = docker compose -f infrastructure/docker/compose.yaml --env-file .env


.PHONY: help sync test lint fmt schemas schemas-check \
        secrets up down ready reset spike-linux up-obs demo demo-replay backup e2e logs smoke-live

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

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

record-replay: .env ## (S2) Oracle-gated live recording: make record-replay SCENARIO=prompt-injection
	@test -n "$(SCENARIO)" || { echo "usage: make record-replay SCENARIO=<id>" >&2; exit 2; }
	@$(WITH_ENV) uv run python scripts/record_replay.py --scenario $(SCENARIO) --agent sigint-agent

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
