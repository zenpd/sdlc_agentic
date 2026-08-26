SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

# Colors for output
ECHO := printf '%b\n'
GREEN := \033[32m
YELLOW := \033[33m
RED := \033[31m
CYAN := \033[36m
RESET := \033[0m
UNDERLINE := \033[4m

# Required uv version
REQUIRED_UV_VERSION := 0.8.13
PKGS ?= openhands-sdk openhands-tools

.PHONY: build format lint clean help check-uv-version run-backend run-frontend

# Default target
.DEFAULT_GOAL := help


check-uv-version:
	@$(ECHO) "$(YELLOW)Checking uv version...$(RESET)"
	@UV_VERSION=$$(uv --version | cut -d' ' -f2); \
	REQUIRED_VERSION=$(REQUIRED_UV_VERSION); \
	if [ "$$(printf '%s\n' "$$REQUIRED_VERSION" "$$UV_VERSION" | sort -V | head -n1)" != "$$REQUIRED_VERSION" ]; then \
		$(ECHO) "$(RED)Error: uv version $$UV_VERSION is less than required $$REQUIRED_VERSION$(RESET)"; \
		$(ECHO) "$(YELLOW)Please update uv with: uv self update$(RESET)"; \
		exit 1; \
	fi; \
	$(ECHO) "$(GREEN)uv version $$UV_VERSION meets requirements$(RESET)"

build: check-uv-version
	@$(ECHO) "$(CYAN)Setting up the Agent SDLC development environment...$(RESET)"
	@$(ECHO) "$(YELLOW)Installing dependencies with uv sync --dev...$(RESET)"
	@uv sync --dev
	@$(ECHO) "$(GREEN)Dependencies installed successfully.$(RESET)"
	@$(ECHO) "$(YELLOW)Setting up pre-commit hooks...$(RESET)"
	@uv run pre-commit install
	@$(ECHO) "$(GREEN)Pre-commit hooks installed successfully.$(RESET)"
	@$(ECHO) "$(GREEN)Build complete! Development environment is ready.$(RESET)"

run-backend:
	@$(ECHO) "$(CYAN)Starting FastAPI backend on :8040...$(RESET)"
	@uv run uvicorn backend.api_server:app --host 127.0.0.1 --port 8040

run-frontend:
	@$(ECHO) "$(CYAN)Starting Next.js dashboard...$(RESET)"
	@cd ui && npm run dev

format:
	@$(ECHO) "$(YELLOW)Formatting code with uv format...$(RESET)"
	@uv run ruff format
	@$(ECHO) "$(GREEN)Code formatted successfully.$(RESET)"

lint:
	@$(ECHO) "$(YELLOW)Linting code with ruff...$(RESET)"
	@uv run ruff check --fix
	@$(ECHO) "$(GREEN)Linting completed.$(RESET)"

pre-commit:
	@$(ECHO) "$(YELLOW)Run pre-commit...$(RESET)"
	uv run pre-commit run --all-files
	@$(ECHO) "$(GREEN)Pre-commit run successfully.$(RESET)"

clean:
	@$(ECHO) "$(YELLOW)Cleaning up cache files...$(RESET)"
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache .mypy_cache 2>/dev/null || true
	@$(ECHO) "$(GREEN)Cache files cleaned.$(RESET)"


# Show help
help:
	@$(ECHO) "$(CYAN)Agent SDLC Makefile$(RESET)"
	@$(ECHO) ""
	@$(ECHO) "$(UNDERLINE)Usage:$(RESET) make <COMMAND>"
	@$(ECHO) ""
	@$(ECHO) "$(UNDERLINE)Commands:$(RESET)"
	@$(ECHO) "  $(GREEN)build$(RESET)                Setup development environment (install deps + hooks)"
	@$(ECHO) "  $(GREEN)run-backend$(RESET)          Run the FastAPI backend on :8040"
	@$(ECHO) "  $(GREEN)run-frontend$(RESET)         Run the Next.js dashboard dev server"
	@$(ECHO) "  $(GREEN)format$(RESET)               Format code with uv format"
	@$(ECHO) "  $(GREEN)lint$(RESET)                 Lint code with ruff"
	@$(ECHO) "  $(GREEN)pre-commit$(RESET)           Run the pre-commit"
	@$(ECHO) "  $(GREEN)clean$(RESET)                Clean up cache files"
	@$(ECHO) "  $(GREEN)help$(RESET)                 Show this help message"


.PHONY: set-package-version
set-package-version: check-uv-version
	@if [ -z "$(version)" ]; then \
		$(ECHO) "$(RED)Error: missing version. Use: make set-package-version version=1.2.3$(RESET)"; \
		exit 1; \
	fi
	@$(ECHO) "$(CYAN)Setting version to $(version) for: $(PKGS)$(RESET)"
	@for PKG in $(PKGS); do \
		$(ECHO) "$(YELLOW)bumping $$PKG -> $(version)$(RESET)"; \
		uv version --package $$PKG $(version); \
	done
	@$(ECHO) "$(GREEN)Version updated in all selected packages.$(RESET)"
