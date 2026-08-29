.DEFAULT_GOAL := help
SHELL := /bin/bash
CORPUS ?= full
WHO ?= raj

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install everything
	uv sync --all-extras
	cp -n .env.example .env || true

up: ## Start the core infrastructure (postgres, redis, minio)
	docker compose --profile core up -d --wait

down: ## Stop infrastructure, keep volumes
	docker compose --profile core down

nuke: ## Stop infrastructure and destroy all data
	docker compose --profile core down -v

migrate: ## Run database migrations (as the owner role)
	uv run alembic upgrade head

fetch: ## Clone the GitLab handbook corpus into corpus/sources/
	uv run gatekeeper corpus fetch

seed: ## Load the corpus into Postgres with derived ACLs (CORPUS=full|small)
	uv run gatekeeper corpus load --profile $(CORPUS)

index: ## Chunk and embed the loaded corpus (idempotent; --force to re-chunk)
	uv run gatekeeper index build

stats: ## Show indexed chunk counts per embedding space
	uv run gatekeeper index stats

ask: ## Ask a question as a principal, e.g. make ask Q="expense limit?" WHO=dana
	uv run gatekeeper ask "$(Q)" --as $(WHO)

reacl: ## Re-apply corpus/acl_rules.yaml to existing chunks (no re-embedding)
	uv run gatekeeper index reacl

eval: ## Run the retrieval ablation over the golden set; writes docs/ABLATION.md
	uv run gatekeeper eval

redteam: ## Run the adversarial corpus; exits non-zero on any leak
	uv run gatekeeper redteam

bench: ## Measure what RLS costs ANN search; writes docs/BENCHMARKS.md
	uv run gatekeeper bench

verify-audit: ## Verify the tamper-evidence of the audit chain
	uv run gatekeeper audit

ui: ## Serve the demo console on http://127.0.0.1:8077
	uv run gatekeeper serve

whoami: ## Show what a given principal can see, e.g. make whoami WHO=dana
	uv run gatekeeper principals show $(WHO)

bootstrap: up migrate fetch seed index ## One command from nothing to a queryable system

lint: ## ruff + mypy
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts
	uv run mypy src scripts

fmt: ## Autoformat
	uv run ruff check --fix src tests scripts
	uv run ruff format src tests scripts

test: ## Unit tests only (no docker required)
	uv run pytest tests/unit -q

test-all: ## Full suite including RLS integration tests (needs docker)
	uv run pytest -q

.PHONY: help install up down nuke migrate fetch seed index repair reacl stats eval redteam bench verify-audit ask ui whoami bootstrap lint fmt test test-all
