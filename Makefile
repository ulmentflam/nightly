# Nightly — dev loop
#
# uv-managed Python project · Pyrefly for types · ruff for lint+format · pytest
# Run `make` or `make help` to see available targets.

SHELL := /bin/bash
UV    ?= uv

# Where the Python workspace lives once Phase 0 lands.
PKGS  := packages
BRAIN := .planning/brainstorm.html

.DEFAULT_GOAL := help
.PHONY: help install install-hooks uninstall-hooks pre-commit lock sync lint fmt format type test check brief planning clean nuke


# ────────────────────────────────────────────────────────────────────────────
# meta
# ────────────────────────────────────────────────────────────────────────────

help: ## show this help
	@printf "\n  \033[1mNightly · dev loop\033[0m\n\n"
	@awk 'BEGIN {FS = ":.*?## "} \
	     /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' \
	     $(MAKEFILE_LIST)
	@printf "\n  uv=%s\n\n" "$(UV)"


# ────────────────────────────────────────────────────────────────────────────
# project lifecycle — most targets become real once Phase 0 lands a pyproject.toml
# ────────────────────────────────────────────────────────────────────────────

install: sync ## alias for sync

sync: ## install / refresh deps with uv (no-op until pyproject.toml exists)
	@if [ -f pyproject.toml ]; then \
	  $(UV) sync --all-packages; \
	else \
	  echo "›› pyproject.toml not present yet (Phase 0 — repo contract + Python workspace)"; \
	fi

lock: ## regenerate uv.lock
	@if [ -f pyproject.toml ]; then \
	  $(UV) lock; \
	else \
	  echo "›› pyproject.toml not present yet"; \
	fi


# ────────────────────────────────────────────────────────────────────────────
# inner loop — lint · format · type · test
# ────────────────────────────────────────────────────────────────────────────

lint: ## ruff check (lint)
	@if [ -d $(PKGS) ]; then \
	  $(UV) run ruff check $(PKGS); \
	else \
	  echo "›› no $(PKGS)/ yet — nothing to lint"; \
	fi

fmt: format ## alias for format

format: ## ruff format (write)
	@if [ -d $(PKGS) ]; then \
	  $(UV) run ruff format $(PKGS); \
	else \
	  echo "›› no $(PKGS)/ yet — nothing to format"; \
	fi

type: ## Pyrefly type-check
	@if [ -d $(PKGS) ]; then \
	  $(UV) run pyrefly check $(PKGS); \
	else \
	  echo "›› no $(PKGS)/ yet — nothing to type-check"; \
	fi

test: ## pytest
	@if [ -d $(PKGS) ]; then \
	  $(UV) run pytest; \
	else \
	  echo "›› no $(PKGS)/ yet — nothing to test"; \
	fi

check: lint type test ## lint + type + test (the merge gate)


# ────────────────────────────────────────────────────────────────────────────
# git hooks — pre-commit framework wires ruff + pyrefly into `git commit`
# ────────────────────────────────────────────────────────────────────────────

install-hooks: ## arm the .git/hooks/pre-commit hook (idempotent)
	@$(UV) run --no-sync pre-commit install
	@echo "✓ pre-commit hook installed. Bypass with: git commit --no-verify"

uninstall-hooks: ## remove the pre-commit hook
	@$(UV) run --no-sync pre-commit uninstall

pre-commit: ## run every configured hook against every tracked file
	@$(UV) run --no-sync pre-commit run --all-files


# ────────────────────────────────────────────────────────────────────────────
# planning artifacts
# ────────────────────────────────────────────────────────────────────────────

brief: ## open the brainstorm in your browser
	@open $(BRAIN) 2>/dev/null || xdg-open $(BRAIN) 2>/dev/null || \
	  echo "open the file manually: $(BRAIN)"

planning: ## list every artifact under .planning/
	@find .planning -maxdepth 3 -type f 2>/dev/null | sort


# ────────────────────────────────────────────────────────────────────────────
# housekeeping
# ────────────────────────────────────────────────────────────────────────────

clean: ## remove caches (keeps .venv)
	rm -rf .ruff_cache .pyrefly_cache .pytest_cache .mypy_cache \
	       **/__pycache__ **/.pytest_cache .coverage htmlcov

nuke: clean ## clean + drop the venv
	rm -rf .venv
