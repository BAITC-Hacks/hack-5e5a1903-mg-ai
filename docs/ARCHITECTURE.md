# Архитектура

## Компоненты

```mermaid
flowchart LR
    Client["Браузер"]
    FE["frontend<br/>React, планируется"]
    BE["backend<br/>FastAPI, root_path=/api"]
    DB[("PostgreSQL 16")]
    PL["pipeline<br/>прогноз, разовый запуск"]
    VOL[("data/ ro<br/>outputs/ reports/ rw")]

    Client --> FE
    Client --> BE
    FE -.-> BE
    BE --> DB
    PL --> VOL

    subgraph compose["docker compose"]
        FE
        BE
        DB
        PL
    end

    style FE stroke-dasharray: 5 5
```

Пунктиром обозначен сервис, которого в репозитории еще нет. Сейчас реально работают
`backend` и `db`, оба описаны в `docker-compose.yml`.

`pipeline` — не сервис, а разовая команда в том же образе, что и `backend`. Он лежит
в профиле `pipeline`, поэтому `docker compose up` его не поднимает, а `docker compose
run --rm pipeline` запускает и удаляет контейнер. БД ему не нужна, поэтому связи
с `db` у него нет: расчет прогноза читает `data/` и пишет в `outputs/` и `reports/`.

## Внутреннее устройство backend

```mermaid
flowchart TD
    R["router<br/>принимает запрос"]
    S["service<br/>бизнес-логика"]
    M["models<br/>таблицы SQLAlchemy"]
    SC["schemas<br/>валидация Pydantic"]
    DBDEP["get_db<br/>сессия через Depends"]
    EX["BusinessError<br/>единый конверт ошибок"]

    R --> SC
    R --> S
    R --> DBDEP
    S --> M
    S --> EX
    DBDEP --> S
```

Правило слоев: роутер не содержит логики, сервис не знает про HTTP. Ошибки бросаются
только через `BusinessError`, иначе ответ не попадет в общий формат.

## Стек

| Слой | Технология | Где |
|------|-----------|-----|
| Frontend | React, планируется | `frontend/` |
| Backend | FastAPI, SQLAlchemy 2.0 async, Alembic | `backend/` |
| БД | PostgreSQL 16 | сервис `db` в compose |
| Пакеты Python | uv | `backend/pyproject.toml`, `backend/uv.lock` |
| Линтер и формат | Ruff | конфиг в `backend/pyproject.toml` |
| Тесты | pytest, pytest-asyncio, httpx | `backend/tests/` |
| Запуск | Docker Compose | `docker-compose.yml` |
| Пайплайн прогноза | тот же образ backend, разовый контейнер | сервис `pipeline` в compose |
| CI | GitHub Actions | `.github/workflows/ci.yml` |

## Структура репозитория

```
HACKALEM AI/
├── AGENTS.md                # инструкции для ИИ-агентов
├── CLAUDE.md                # ссылки на документацию для Claude Code
├── README.md                # описание проекта для судей, на английском
├── SECURITY.md              # принятые меры безопасности
├── Makefile                 # install / hooks / run / test / lint / audit / migrate
├── docker-compose.yml       # единственная точка запуска
├── docker-compose.dev.yml   # оверлей с автоперезагрузкой
├── .env.example             # все переменные окружения без значений
├── .pre-commit-config.yaml  # хуки, включая запрет коммита в main
├── .github/
│   ├── workflows/ci.yml     # линтер, формат, тесты, сборка образа
│   ├── workflows/audit.yml  # уязвимости в зависимостях, на каждый PR
│   └── pull_request_template.md
├── scripts/
│   ├── no-push-to-main.sh   # хук pre-push, запрещает push в main
│   ├── protect-main.sh      # включает защиту ветки main на GitHub
│   └── audit-deps.sh        # pip-audit и npm audit
├── rules/                   # PDF организаторов и выжимка из них
├── data/                    # исходные данные SCADA, монтируются только на чтение
├── outputs/                 # файлы прогноза, пишет сервис pipeline
├── reports/                 # метрики бэктеста, пишет сервис pipeline
├── docs/
│   ├── ARCHITECTURE.md      # этот файл
│   ├── architecture-guidelines.md  # best practices
│   ├── proposals.md         # инструменты на будущее, не внедрены
│   ├── backend.md           # разбор backend и его шаблона
│   ├── git-workflow.md      # ветки, коммиты, PR, запрет прямого push в main
│   ├── dependency-audit.md  # проверка зависимостей перед мерджем
│   ├── worktrees.md         # worktree на фичу и слоты портов
│   ├── ai-workflow.md       # как команда работала с ИИ
│   ├── pre-submit-checklist.md
│   ├── adr/                 # записи об архитектурных решениях
│   └── dev1/ dev2/ dev3/    # зоны ответственности разработчиков
├── .dev-notes/              # заметки команды, me.md определяет кто ты
└── backend/
    ├── src/core/            # config, database, security, exceptions, logger
    ├── src/modules/auth/    # образцовый модуль
    ├── migrations/          # Alembic
    └── tests/               # pytest
```

## Правила и то, чего здесь пока нет

Правила, по которым эта архитектура растет, лежат в
[architecture-guidelines.md](architecture-guidelines.md). Ключевое из них: backend
не хранит состояние в памяти процесса, поэтому реплики взаимозаменяемы и приложение
готово к балансировке нагрузки.

Инструменты, которые хорошо подошли бы дальше, собраны в [proposals.md](proposals.md)
вместе с оценкой, когда они понадобятся. Среди них обратный прокси и балансировка
на nginx, Redis, фоновый воркер, объектное хранилище и pgvector. **Ничего из этого
в проекте нет**, и добавляется оно только по решению команды.

## Решения и почему так

Каждое значимое решение зафиксировано отдельной записью в [adr/](adr/):

- [0001](adr/0001-fastapi-template-as-base.md) — взять готовый FastAPI-шаблон за основу.
- [0002](adr/0002-worktree-per-feature.md) — worktree на фичу со слотами портов.
- [0003](adr/0003-single-compose-entrypoint.md) — один compose-файл в корне как точка запуска.
- [0004](adr/0004-sqlite-for-tests.md) — тесты на SQLite в памяти, а приложение на PostgreSQL.

## Запуск

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec backend alembic upgrade head
curl http://localhost:8000/api/health
```

Порты берутся из `.env`. Если 8000 или 3000 на машине заняты, слот меняется,
см. [worktrees.md](worktrees.md).

Пайплайн прогноза запускается отдельно и веб-часть ему не нужна:

```bash
cp .env.example .env
make pipeline cmd="replay --start 2026-01-31 --end 2026-02-27"
```

Результаты появляются в `outputs/` и `reports/` на хосте. Контейнер работает
от пользователя `appuser` с uid 1000, поэтому оба каталога лежат в репозитории
с файлом `.gitkeep`: если бы их не было, Docker создал бы их от root, и запись
из контейнера упала бы. Для разработки тот же пайплайн запускается без Docker,
через `uv` из каталога `backend`.
