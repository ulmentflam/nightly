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
.PHONY: help install install-hooks uninstall-hooks pre-commit lock sync unhide-pth venv-path lint fmt format type test check brief planning clean nuke


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

# ── venv placement · keep it off iCloud ─────────────────────────────────────
#
# Under iCloud Drive, every freshly written file inherits the parent's
# UF_HIDDEN flag. `site.py` skips hidden `.pth` files, so the editable
# install of the workspace packages silently stops being applied — and the
# failure looks nothing like its cause: `uv sync` reports success, and every
# test then dies with `ModuleNotFoundError: No module named 'nightly_core'`
# on a checkout that is perfectly healthy. (Same root cause, and the same
# `chflags` remedy below, as ulmentflam/corpus-forge.)
#
# Relocating is the wider fix. A venv is the worst possible payload to hand
# fileproviderd — thousands of small files, absolute-path shebangs, and
# binaries it will happily evict to dataless `.icloud` placeholders — so
# when the repo is under iCloud the venv is built in the local cache and
# `.venv` becomes a symlink to it. Tooling that hardcodes `./.venv` (uv, the
# pre-commit hooks, editors) keeps working, and nothing fileproviderd
# touches is ever on the import path.
#
# `unhide-pth` stays as the narrower belt-and-braces: anyone who runs
# `uv sync` directly, bypassing make, still gets an in-repo venv with hidden
# `.pth` files, and that target repairs it without a full rebuild.
#
# The path test mirrors `nightly_core.worktree.is_icloud_path`. Off macOS —
# and anywhere outside iCloud — VENV_DIR is empty and everything below
# behaves exactly as it did before, so CI is unaffected.
# NB: no `case` here. make matches parentheses when it scans `$(shell …)`, so
# the unbalanced `)` closing a case arm terminates the function early and the
# expansion silently comes back wrong — a hashed empty string. `grep` keeps
# every paren balanced.
VENV_DIR := $(shell real="$$(pwd -P)"; \
  echo "$$real" | grep -q 'Mobile Documents\|com~apple~CloudDocs' && \
  printf '%s/nightly/venvs/%s-%s' "$${XDG_CACHE_HOME:-$$HOME/.cache}" \
    "$$(basename "$$real")" "$$(printf '%s' "$$real" | shasum | cut -c1-8)")

install: sync ## alias for sync

sync: ## install / refresh deps with uv (venv goes outside iCloud when detected)
	@if [ ! -f pyproject.toml ]; then \
	  echo "›› pyproject.toml not present yet (Phase 0 — repo contract + Python workspace)"; \
	elif [ -z "$(VENV_DIR)" ]; then \
	  $(UV) sync --all-packages; \
	else \
	  mkdir -p "$$(dirname "$(VENV_DIR)")"; \
	  if [ -e .venv ] && [ ! -L .venv ]; then \
	    echo "›› .venv is a real directory inside iCloud — replacing it with a link"; \
	    rm -rf .venv; \
	  fi; \
	  if [ "$$(readlink .venv 2>/dev/null)" != "$(VENV_DIR)" ]; then \
	    rm -f .venv && ln -s "$(VENV_DIR)" .venv; \
	  fi; \
	  echo "›› iCloud detected — venv lives at $(VENV_DIR)"; \
	  UV_PROJECT_ENVIRONMENT="$(VENV_DIR)" $(UV) sync --all-packages; \
	fi
	@$(MAKE) --no-print-directory unhide-pth

unhide-pth: ## clear the iCloud UF_HIDDEN flag that makes site.py skip .pth files
	@if [ "$$(uname -s)" = "Darwin" ] && [ -d .venv/ ]; then \
	  find .venv/ -name "*.pth" -exec chflags nohidden {} + 2>/dev/null || true; \
	fi

venv-path: ## print where the venv lives (and why)
	@if [ -z "$(VENV_DIR)" ]; then \
	  echo "$$(pwd -P)/.venv (repo is not under iCloud)"; \
	else \
	  echo "$(VENV_DIR)"; \
	  echo "  linked from ./.venv — repo is under iCloud, see the Makefile comment"; \
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

nuke: clean ## clean + drop the venv (including the out-of-iCloud one it links to)
	@if [ -n "$(VENV_DIR)" ]; then rm -rf "$(VENV_DIR)"; fi
	rm -rf .venv
