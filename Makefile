.PHONY: install hooks run demo dev down logs ps migrate makemigrations seed test lint fmt audit check protect-main ml-install ml-test ml-lint ml-fmt ml-openapi

COMPOSE     := docker compose
# пайплайн прогноза выполняется в образе backend разовым контейнером,
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml
# pytest и ruff берут настройки из backend/pyproject.toml, поэтому запускаются из backend
BACKEND     := cd backend && uv
# ML-сервис — отдельный проект со своим uv.lock
ML          := cd ml && uv

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

## demo — поднять всё и подготовить к показу: стек, миграции, администратор
demo: run
	bash scripts/wait-healthy.sh db
	bash scripts/wait-healthy.sh backend
	$(MAKE) migrate
	$(MAKE) seed
	@echo "Интерфейс: http://localhost:$${FRONTEND_PORT:-3000}"

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

## ml-install — поставить зависимости ML-сервиса
ml-install:
	$(ML) sync --dev

## ml-test — тесты ML-сервиса
ml-test:
	$(ML) run pytest

ml-train:
	$(ML) run python -m training.train

## ml-lint — стиль и форматирование ML-сервиса
ml-lint:
	$(ML) run ruff check .
	$(ML) run ruff format --check .

## ml-fmt — отформатировать ML-сервис
ml-fmt:
	$(ML) run ruff format .
	$(ML) run ruff check --fix .

## ml-openapi — выгрузить контракт ML-сервиса в ml/openapi.json после изменения схем
ml-openapi:
	$(ML) run python scripts/export_openapi.py

## check — то, что должно проходить перед коммитом и перед мерджем
check: lint test ml-lint ml-test audit

## protect-main — один раз включить защиту ветки main на GitHub
protect-main:
	bash scripts/protect-main.sh
