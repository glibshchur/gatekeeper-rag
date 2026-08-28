.DEFAULT_GOAL := help
SHELL := /bin/bash
CORPUS ?= full

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

whoami: ## Show what a given principal can see, e.g. make whoami WHO=dana@acme
	uv run gatekeeper principals show $(WHO)

bootstrap: up migrate fetch seed ## One command from nothing to a queryable system

lint: ## ruff + mypy
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy src

fmt: ## Autoformat
	uv run ruff check --fix src tests
	uv run ruff format src tests

test: ## Unit tests only (no docker required)
	uv run pytest tests/unit -q

test-all: ## Full suite including RLS integration tests (needs docker)
	uv run pytest -q

.PHONY: help install up down nuke migrate fetch seed whoami bootstrap lint fmt test test-all
