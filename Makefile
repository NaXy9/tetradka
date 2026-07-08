# Tetradka — dev shortcuts. Run `make help` for the list of targets.

COMPOSE = docker compose
MANAGE  = $(COMPOSE) run --rm web python manage.py

.PHONY: help up down logs ps bootstrap migrate superuser test lint fmt

help: ## list available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## start services (profiles come from COMPOSE_PROFILES in .env)
	$(COMPOSE) up -d --build

down: ## stop services
	$(COMPOSE) down

logs: ## tail logs of all services
	$(COMPOSE) logs -f --tail=100

ps: ## service status
	$(COMPOSE) ps

bootstrap: ## first run: migrations + superuser + web
	$(MANAGE) migrate
	$(MANAGE) createsuperuser --noinput || true
	$(COMPOSE) up -d web
	@echo "API: http://localhost:8000  Swagger: /api/schema/swagger-ui/  MinIO: http://localhost:9001"

migrate: ## apply database migrations
	$(MANAGE) migrate

superuser: ## create a superuser interactively
	$(MANAGE) createsuperuser

test: ## run tests inside the container
	$(COMPOSE) run --rm web pytest -x -q

lint: ## run linters inside the container
	$(COMPOSE) run --rm web sh -c "ruff check . && black --check ."

fmt: ## autoformat locally (outside the container)
	ruff check --fix . && black .
