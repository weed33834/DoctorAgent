# DoctorAgent development Makefile.
# Targets are phony unless they produce a file with the same name.

.PHONY: help install test lint format security docker-build docker-up docker-up-llm docker-down clean build check-version release-tag release

PYTHON ?= python
DOCKER ?= docker
DOCKER_COMPOSE ?= docker compose

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make <target>\n\nTargets:\n"} \
	  /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Install the project with dev extras (editable).
	$(PYTHON) -m pip install -e ".[dev]"

test: ## Run the test suite via pytest.
	$(PYTHON) -m pytest

lint: ## Lint with ruff (checks only).
	ruff check doctoragent/ && ruff format --check doctoragent/

format: ## Auto-format source with ruff.
	ruff format doctoragent/

security: ## Run bandit security scan.
	bandit -r doctoragent/ -q

docker-build: ## Build the doctoragent Docker image.
	$(DOCKER) build -t doctoragent:latest .

docker-up: ## Start the stack WITHOUT the local LLM (default profile).
	$(DOCKER_COMPOSE) up --build

docker-up-llm: ## Start the stack WITH the local Ollama LLM.
	$(DOCKER_COMPOSE) --profile with-llm up --build

docker-down: ## Stop and remove containers from the stack.
	$(DOCKER_COMPOSE) down

clean: ## Remove build artefacts and caches.
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# ── 发布相关 ──

build: ## Build wheel and sdist for PyPI.
	$(PYTHON) -m pip install --upgrade build
	$(PYTHON) -m build

check-version: ## Verify version consistency across pyproject, __init__, Dockerfile.
	@PY_VER=$$(grep '^version' pyproject.toml | sed 's/.*"\(.*\)".*/\1/'); \
	INIT_VER=$$(grep '__version__' doctoragent/__init__.py | sed 's/.*"\(.*\)".*/\1/'); \
	DOCKER_VER=$$(grep 'DOCTORAGENT_VERSION' Dockerfile | head -1 | sed 's/.*=\(.*\)/\1/'); \
	echo "pyproject.toml:    $$PY_VER"; \
	echo "__init__.py:       $$INIT_VER"; \
	echo "Dockerfile:        $$DOCKER_VER"; \
	if [ "$$PY_VER" != "$$INIT_VER" ] || [ "$$PY_VER" != "$$DOCKER_VER" ]; then \
		echo "❌ 版本号不一致！请统一后再发布。"; exit 1; \
	else \
		echo "✅ 版本号一致: $$PY_VER"; \
	fi

release-tag: ## Create and push a version tag (usage: make release-tag VERSION=0.3.1).
	@if [ -z "$(VERSION)" ]; then echo "用法: make release-tag VERSION=0.3.1"; exit 1; fi
	@git tag v$(VERSION) && git push origin v$(VERSION)
	@echo "✅ 已推送 tag v$(VERSION)，GitHub Actions 将自动发布到 PyPI + GHCR + GitHub Release"

release: check-version build ## Full release: verify version, build, tag and push.
	@if [ -z "$(VERSION)" ]; then echo "用法: make release VERSION=0.3.1"; exit 1; fi
	@git tag v$(VERSION) && git push origin v$(VERSION)
	@echo "🚀 发布已触发！查看进度: https://github.com/weed33834/DoctorAgent/actions"
