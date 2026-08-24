# ArenaBench — the commands worth having a short name for.
#
# `make help` lists them. Everything here is a thin front door onto a script
# in `scripts/` or a verb of the CLI itself; nothing has logic of its own, so
# a target and the thing it calls cannot drift into disagreeing.
#
# The two jobs this file exists for:
#
#   MONEY   `aws-scan` says what is running and what it costs per hour;
#           `aws-pause` stops it. The substrate scales to zero, which is
#           exactly why a stuck job is expensive — nothing looks wrong.
#
#   MATCHES `h2h` and `frontierbench` seat Stella against Claude Code on the
#           same worker model at the same effort, so the architecture is the
#           variable rather than one seat's budget.

SHELL := /bin/bash
PY ?= python3
REGION ?= us-east-1

# `make aws-pause` is a dry run unless APPLY=1. See scripts/aws-pause.sh for
# why that is the default and not politeness.
APPLY ?=
PAUSE_FLAGS := $(if $(APPLY),--apply,)

# Passed through to the quick starts. TASKS and SEED are the two a person
# actually retypes; everything else is available via ARGS.
TASKS ?= 10
SEED ?=
ARGS ?=
QUICK_FLAGS := --tasks $(TASKS) $(if $(SEED),--seed $(SEED),) $(ARGS)

.DEFAULT_GOAL := help

.PHONY: help
help: ## List every target
	@printf '\033[1mArenaBench\033[0m\n\n'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@printf '\n  \033[2mVariables: TASKS=%s SEED= APPLY=1 REGION=%s ARGS="--dry-run"\033[0m\n' \
	  '$(TASKS)' '$(REGION)'

# ── Money ────────────────────────────────────────────────────────────────────

.PHONY: aws-scan
aws-scan: ## What is running in AWS right now, and what it costs per hour (read-only)
	@./scripts/aws-scan.sh --region $(REGION)

.PHONY: aws-pause
aws-pause: ## Stop running Batch jobs and builds (DRY RUN; APPLY=1 to act)
	@./scripts/aws-pause.sh --region $(REGION) $(PAUSE_FLAGS)

.PHONY: aws-pause-all
aws-pause-all: ## ...also stop non-Batch EC2 and disable the compute environments
	@./scripts/aws-pause.sh --region $(REGION) --jobs --builds --ec2 --hard $(PAUSE_FLAGS)

# ── Matches ──────────────────────────────────────────────────────────────────
#
# Credentials are never arguments to make: a make variable lands in the same
# `ps` output and shell history a command-line flag does, and make also echoes
# its own recipes. Both starters read $ANTHROPIC_API_KEY and
# $CLAUDE_CODE_OAUTH_TOKEN from the environment, and take --anthropic-key-file
# / --oauth-token-file via ARGS for the file path. The literal --anthropic-key
# and --oauth-token flags exist and warn about exactly this when used.

.PHONY: h2h
h2h: ## Stella vs Claude Code, sonnet 5 low, sampled from the whole dataset
	@$(PY) scripts/quickstart.py h2h $(QUICK_FLAGS)

.PHONY: frontierbench
frontierbench: ## Same pairing over EASY+MEDIUM tasks only (ARGS=--include-hard widens)
	@$(PY) scripts/quickstart.py frontierbench $(QUICK_FLAGS)

.PHONY: match-preview
match-preview: ## Write a frontierbench toml and stop, so you can read it first
	@$(PY) scripts/quickstart.py frontierbench $(QUICK_FLAGS) --dry-run

# ── Running matches ──────────────────────────────────────────────────────────

.PHONY: status
status: ## Follow a cloud run (RUN=<run-id>)
	@[ -n "$(RUN)" ] || { echo 'usage: make status RUN=r20260824-...'; exit 2; }
	@$(PY) -m arenabench cloud status $(RUN) --follow

.PHONY: fetch
fetch: ## Pull a finished run's artifacts (RUN=<run-id> [OUT=dir])
	@[ -n "$(RUN)" ] || { echo 'usage: make fetch RUN=r20260824-... [OUT=dir]'; exit 2; }
	@$(PY) -m arenabench cloud fetch $(RUN) --artifacts $(if $(OUT),--out $(OUT),)

.PHONY: cancel
cancel: ## Cancel a cloud run's remaining jobs (RUN=<run-id>)
	@[ -n "$(RUN)" ] || { echo 'usage: make cancel RUN=r20260824-...'; exit 2; }
	@$(PY) -m arenabench cloud cancel $(RUN)

.PHONY: serve
serve: ## Open the arena UI on this machine
	@$(PY) -m arenabench serve

.PHONY: tasks
tasks: ## List the dataset's tasks with difficulty and category
	@$(PY) -m arenabench tasks terminal-bench-2.1

# ── Checks ───────────────────────────────────────────────────────────────────

.PHONY: test
test: ## The Python suite CI runs
	@uv run --with pytest --no-project pytest -q

.PHONY: lint
lint: ## ruff, plus shellcheck over scripts/ when it is installed
	@uv run --with ruff --no-project ruff check .
	@# `A && B || C` would report a shellcheck FAILURE as "not installed",
	@# because the `||` fires on B's exit code as readily as on A's. An if
	@# statement is the only form that can tell the two apart.
	@if command -v shellcheck >/dev/null 2>&1; then \
	  shellcheck scripts/*.sh; \
	else \
	  echo "shellcheck not installed — skipped"; \
	fi

.PHONY: guards
guards: ## The role-name guard CI runs
	@./scripts/check-role-names.sh
