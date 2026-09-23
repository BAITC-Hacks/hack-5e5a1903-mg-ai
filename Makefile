.PHONY: install hooks run dev down logs ps migrate makemigrations seed test lint fmt audit check protect-main

COMPOSE     := docker compose
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml
# pytest и ruff берут настройки из backend/pyproject.toml, поэтому запускаются из backend
BACKEND     := cd backend && uv

## install — поставить зависимости backend вместе с dev-группой
install:
	$(BACKEND) sync --dev

## hooks — поставить git-хуки: проверки перед коммитом и запрет push в main
hooks:
	uvx pre-commit install
	cp scripts/no-push-to-main.sh .git/hooks/pre-push
	chmod +x .git/hooks/pre-push

## run — поднять весь стек (БД + backend) в фоне
run:
	$(COMPOSE) up -d --build

## dev — то же, но с автоперезагрузкой кода и логами в терминале
dev:
	$(COMPOSE_DEV) up --build

## down — погасить стек
down:
	$(COMPOSE) down

## logs — смотреть логи
logs:
	$(COMPOSE) logs -f

## ps — статус контейнеров
ps:
	$(COMPOSE) ps

## migrate — применить миграции внутри контейнера backend
migrate:
	$(COMPOSE) exec backend alembic upgrade head

## makemigrations m="имя" — создать миграцию по изменениям моделей
makemigrations:
	$(COMPOSE) exec backend alembic revision --autogenerate -m "$(m)"

## seed — создать первого администратора (пароль из SEED_ADMIN_PASSWORD или случайный)
seed:
	$(COMPOSE) exec backend python -m src.scripts.seed

## test — прогнать тесты
test:
	$(BACKEND) run pytest

## lint — проверить стиль и форматирование, ничего не меняя
lint:
	$(BACKEND) run ruff check .
	$(BACKEND) run ruff format --check .

## fmt — отформатировать и починить то, что починится автоматически
fmt:
	$(BACKEND) run ruff format .
	$(BACKEND) run ruff check --fix .

## audit — проверить зависимости на известные уязвимости
audit:
	bash scripts/audit-deps.sh

## check — то, что должно проходить перед коммитом и перед мерджем
check: lint test audit

## protect-main — один раз включить защиту ветки main на GitHub
protect-main:
	bash scripts/protect-main.sh
