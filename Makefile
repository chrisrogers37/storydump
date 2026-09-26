.PHONY: help install install-dev test test-unit test-integration test-quick test-failed test-watch clean create-db drop-db reset-db init-db setup-db check-db check-health run dev logs db-shell db-backup db-restore env-example quickstart validate-env

# Load environment variables from .env file
ifneq (,$(wildcard ./.env))
    include .env
    export
endif

# Default environment file
ENV_FILE ?= .env

# Where the virtualenv lives. This checkout uses `.venv/`; older ones and some
# contributors use `venv/`, and the test targets hardcoded the latter — so six
# targets named a `pytest` that is not there (TD-O6). Probe rather than choose:
# `.venv/` wins when it exists, `venv/` is the fallback, and `make VENV=...`
# overrides both.
VENV ?= $(if $(wildcard .venv/bin/pytest),.venv,venv)

# Database connection variables
DB_HOST ?= localhost
DB_PORT ?= 5432
DB_NAME ?= storydump
DB_USER ?= $(USER)
DB_PASSWORD ?=

# PostgreSQL connection options (respects all connection variables)
PG_OPTS = -h $(DB_HOST) -p $(DB_PORT) -U $(DB_USER)

# init-db's migration runner gets its URL from `scripts.app_db_url`, which
# percent-encodes every part: pasted into the URL raw, a password carrying `@`,
# `/` or `%` was misread by libpq after psql had connected with it. The recipe
# hands the helper each field quoted exactly as psql gets it — the password as
# in `PGPASSWORD="$(DB_PASSWORD)"`, the other four bare, as in `PG_OPTS` and
# `-d $(DB_NAME)` — so both steps connect with the same values. Like every psql
# recipe here, a password carrying `"` or a backtick still breaks the shell
# line, a `$` that starts a name is expanded in it, and a single-quoted `.env`
# password keeps its quotes.

# Colors for output
GREEN  := \033[0;32m
YELLOW := \033[0;33m
RED    := \033[0;31m
NC     := \033[0m # No Color

help: ## Show this help message
	@echo "$(GREEN)Storydump - Makefile Commands$(NC)"
	@echo ""
	@echo "$(YELLOW)Usage:$(NC)"
	@echo "  make <target>"
	@echo ""
	@echo "$(YELLOW)Available targets:$(NC)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-15s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)Environment:$(NC)"
	@echo "  ENV_FILE:    $(ENV_FILE)"
	@echo "  DB_HOST:     $(DB_HOST)"
	@echo "  DB_PORT:     $(DB_PORT)"
	@echo "  DB_NAME:     $(DB_NAME)"
	@echo "  DB_USER:     $(DB_USER)"

install: ## Install the dependencies, the package and the `storydump` CLI (the `cli` extra)
	@echo "$(GREEN)Installing dependencies...$(NC)"
	pip install -r requirements.txt
	pip install -e '.[cli]'
	@echo "$(GREEN)✓ Installation complete$(NC)"

install-dev: install ## `install`, plus the lint and scan tools CI runs (ruff, bandit, pip-audit)
	@echo "$(GREEN)Installing the lint and scan tools...$(NC)"
	pip install ruff bandit pip-audit
	@echo "$(GREEN)✓ Development installation complete$(NC)"

test: ## Run tests with pytest (auto-creates test database)
	@echo "$(GREEN)Running tests...$(NC)"
	@echo "$(YELLOW)Note: Test database will be auto-created and cleaned up$(NC)"
	$(VENV)/bin/pytest -v --cov=src --cov-report=term-missing

test-unit: ## Run unit tests only
	@echo "$(GREEN)Running unit tests...$(NC)"
	$(VENV)/bin/pytest -v -m unit

test-integration: ## Run integration tests only
	@echo "$(GREEN)Running integration tests...$(NC)"
	$(VENV)/bin/pytest -v -m integration

test-quick: ## Run tests without coverage (faster)
	@echo "$(GREEN)Running tests (no coverage)...$(NC)"
	$(VENV)/bin/pytest -v --no-cov

test-failed: ## Re-run only failed tests
	@echo "$(GREEN)Re-running failed tests...$(NC)"
	$(VENV)/bin/pytest -v --lf

test-watch: ## Run tests in watch mode (requires pytest-watch)
	@echo "$(GREEN)Running tests in watch mode...$(NC)"
	$(VENV)/bin/ptw -- -v

clean: ## Clean up temporary files and caches
	@echo "$(GREEN)Cleaning up...$(NC)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".coverage" -exec rm -f {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "$(GREEN)✓ Cleanup complete$(NC)"

create-db: ## Create the database
	@echo "$(GREEN)Creating database: $(DB_NAME)...$(NC)"
	@PGPASSWORD="$(DB_PASSWORD)" createdb $(PG_OPTS) $(DB_NAME) 2>/dev/null || \
		(echo "$(YELLOW)⚠ Database $(DB_NAME) may already exist$(NC)" && exit 0)
	@echo "$(GREEN)✓ Database created$(NC)"

drop-db: ## Drop the database (WARNING: destructive)
	@echo "$(RED)WARNING: This will permanently delete database: $(DB_NAME)$(NC)"
	@echo "Press Ctrl+C to cancel, or Enter to continue..." && read confirm
	@echo "$(RED)Dropping database: $(DB_NAME)...$(NC)"
	@PGPASSWORD="$(DB_PASSWORD)" dropdb $(PG_OPTS) --if-exists $(DB_NAME) 2>/dev/null || true
	@echo "$(GREEN)✓ Database dropped$(NC)"

init-db: ## Build the schema on a FRESH database, the way the lineage lane proves it: step 0 (the service roles and the DDL door), the by-hand base with the one table production made by hand, then every runner file. DB_USER needs CREATEROLE
	@echo "$(GREEN)Initializing database schema...$(NC)"
	@PGPASSWORD="$(DB_PASSWORD)" psql $(PG_OPTS) -d $(DB_NAME) -q -v ON_ERROR_STOP=1 \
		-f scripts/window/step0_bootstrap.sql -f scripts/window/step0_legacy_ddl_door.sql \
		-f scripts/setup_database.sql -f tests/scripts/fixtures/legacy_by_hand.sql 2>&1 || \
		(echo "$(RED)✗ Failed to build the by-hand base. Check database connection and permissions (step 0 creates the svc_* roles: DB_USER needs CREATEROLE).$(NC)" && exit 1)
	@DATABASE_URL="$$(DB_USER=$(DB_USER) DB_PASSWORD="$(DB_PASSWORD)" DB_HOST=$(DB_HOST) DB_PORT=$(DB_PORT) DB_NAME=$(DB_NAME) python -m scripts.app_db_url)" python -m scripts.migration_runner apply || \
		(echo "$(RED)✗ The migration runner failed; see its output above.$(NC)" && exit 1)
	@echo "$(GREEN)✓ Schema initialized$(NC)"

setup-db: create-db init-db ## Create database and initialize schema
	@echo "$(GREEN)✓ Database setup complete$(NC)"

reset-db: ## Drop and recreate database (WARNING: destructive)
	@echo "$(RED)WARNING: This will permanently delete database: $(DB_NAME)$(NC)"
	@echo "Press Ctrl+C to cancel, or Enter to continue..." && read confirm
	@PGPASSWORD="$(DB_PASSWORD)" dropdb $(PG_OPTS) --if-exists $(DB_NAME) 2>/dev/null || true
	@PGPASSWORD="$(DB_PASSWORD)" createdb $(PG_OPTS) $(DB_NAME)
	@$(MAKE) init-db
	@echo "$(GREEN)✓ Database reset complete$(NC)"

check-db: ## Check if database exists and is accessible
	@echo "$(GREEN)Checking database connection...$(NC)"
	@PGPASSWORD="$(DB_PASSWORD)" psql $(PG_OPTS) -d $(DB_NAME) -c "SELECT version();" >/dev/null 2>&1 && \
		echo "$(GREEN)✓ Database is accessible$(NC)" || \
		echo "$(RED)✗ Cannot connect to database$(NC)"

check-health: ## Health of the deployed API (storydump health)
	@echo "$(GREEN)Running health checks...$(NC)"
	storydump health

run: ## Run the worker (python -m src.main) — it posts to Instagram; see the safety rules
	@echo "$(GREEN)Starting the worker...$(NC)"
	python -m src.main

dev: ## Run the worker against .env (no gate on production's health)
	@echo "$(GREEN)Starting the worker in development mode...$(NC)"
	@if [ ! -f .env ]; then \
		echo "$(RED)✗ .env file not found. Copy .env.example to .env and configure it.$(NC)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Environment file found$(NC)"
	python -m src.main

logs: ## View application logs (tail -f)
	@echo "$(GREEN)Tailing logs...$(NC)"
	tail -f logs/app.log

db-shell: ## Open PostgreSQL shell for application database
	@echo "$(GREEN)Opening database shell...$(NC)"
	PGPASSWORD="$(DB_PASSWORD)" psql $(PG_OPTS) -d $(DB_NAME)

db-backup: ## Backup database to file
	@echo "$(GREEN)Backing up database...$(NC)"
	@mkdir -p backups
	@BACKUP_FILE="backups/$(DB_NAME)_$$(date +%Y%m%d_%H%M%S).sql"; \
	PGPASSWORD="$(DB_PASSWORD)" pg_dump $(PG_OPTS) $(DB_NAME) > $$BACKUP_FILE && \
	echo "$(GREEN)✓ Backup saved to: $$BACKUP_FILE$(NC)"

db-restore: ## Restore database from backup (Usage: make db-restore FILE=path/to/backup.sql)
	@if [ -z "$(FILE)" ]; then \
		echo "$(RED)✗ Usage: make db-restore FILE=path/to/backup.sql$(NC)"; \
		exit 1; \
	fi
	@echo "$(YELLOW)WARNING: This will restore database from: $(FILE)$(NC)"
	@echo "Press Ctrl+C to cancel, or Enter to continue..." && read confirm
	@echo "$(GREEN)Restoring database...$(NC)"
	@PGPASSWORD="$(DB_PASSWORD)" psql $(PG_OPTS) -d $(DB_NAME) < $(FILE)
	@echo "$(GREEN)✓ Database restored$(NC)"

env-example: ## Copy .env.example to .env
	@if [ -f .env ]; then \
		echo "$(YELLOW)⚠ .env file already exists. Skipping...$(NC)"; \
	else \
		cp .env.example .env; \
		echo "$(GREEN)✓ Created .env from .env.example$(NC)"; \
		echo "$(YELLOW)→ Please edit .env and configure your settings$(NC)"; \
	fi

quickstart: env-example install setup-db ## Quick start: setup everything for first-time use
	@echo ""
	@echo "$(GREEN)========================================$(NC)"
	@echo "$(GREEN)✓ Quickstart Complete!$(NC)"
	@echo "$(GREEN)========================================$(NC)"
	@echo ""
	@echo "$(YELLOW)Next steps:$(NC)"
	@echo "  1. Edit .env and configure your settings"
	@echo "  2. Connect Google Drive and a Telegram group on the web (Settings › Integrations)"
	@echo "  3. Set the schedule on the web (Settings › General, the schedule card)"
	@echo "  4. Run: make run"
	@echo ""

validate-env: ## Load the settings, and build the key ring both services refuse to boot without
	@echo "$(GREEN)Validating environment configuration...$(NC)"
	@python -c "from src.config.settings import settings" && \
		python -c "from src.services.target.oauth_states import ring; ring()" && \
		echo "$(GREEN)✓ Configuration is valid$(NC)" || \
		(echo "$(RED)✗ Configuration validation failed$(NC)" && exit 1)

.DEFAULT_GOAL := help
