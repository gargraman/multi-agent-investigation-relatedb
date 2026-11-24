# Nexus developer commands (spec §16.3).
# Targets for slices not built yet print a notice and EXIT NON-ZERO (spec §19.1 rule 13:
# never fake success).

UV ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

.DEFAULT_GOAL := help

.PHONY: help sync test lint fmt schemas schemas-check \
        secrets up up-obs demo demo-replay reset backup e2e logs smoke-live

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

# --- Not implemented yet: each exits non-zero -------------------------------------------
define not_implemented
	@echo "make $@: not implemented yet — arrives in slice $(1)" >&2; exit 1
endef

secrets: ## (S1) generate per-service tokens into .env
	$(call not_implemented,S1)
up: ## (S1) core app, lite profile
	$(call not_implemented,S1)
up-obs: ## (S6) core + Grafana LGTM
	$(call not_implemented,S6)
demo: ## (S6) up-obs + open UI
	$(call not_implemented,S6)
demo-replay: ## (S2) restart agents in replay mode
	$(call not_implemented,S2)
reset: ## (S1) wipe Relate store + SQLite volumes
	$(call not_implemented,S1)
backup: ## (S1) relate clone of the store to ./backups/
	$(call not_implemented,S1)
e2e: ## (S5) Playwright + chaos + telemetry tests
	$(call not_implemented,S5)
logs: ## (S1) tail all services
	$(call not_implemented,S1)
smoke-live: ## (S2) live-LLM smoke test
	$(call not_implemented,S2)
