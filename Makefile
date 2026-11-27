.DEFAULT_GOAL := help

.PHONY: help sync lint docs-check docs-write content-check history-check \
        test-frida-agent test-platform test-e2e build check

help: ## Show available verification and development targets
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## Install dev, platform, and contract dependencies from lockfile
	uv sync --locked --group dev --group platform --group contract

lint: ## Run static lint checks with ruff
	uv run --only-group dev ruff check .

docs-check: ## Verify generated reference docs are up to date
	uv run --group platform python scripts/generate_public_docs.py --check

docs-write: ## Regenerate reference docs from source definitions
	uv run --group platform python scripts/generate_public_docs.py --write

content-check: ## Check public content hygiene and Markdown links
	python3 scripts/check_public_content.py

history-check: ## Audit git history for leaked secrets or private markers
	python3 scripts/check_public_content.py --check-git-history

test-frida-agent: ## Run Frida agent Node.js unit tests (no npm install required)
	node --test autophone/observation/frida/agent/src/*.test.mjs

test-platform: ## Run public platform pytest suite against disposable PostgreSQL
	scripts/test_public_platform.sh

test-e2e: ## Run dashboard Playwright E2E suite against mock API
	npm run test:dashboard-e2e

build: ## Build public core wheels/sdists and verify archives
	rm -rf dist/core && scripts/build_public_core.sh dist/core

check: lint docs-check content-check test-frida-agent ## Run fast, zero-DB local verification gate
