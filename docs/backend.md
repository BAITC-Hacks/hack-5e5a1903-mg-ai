# Backend: как устроен шаблон

Источник: `https://github.com/msspkz/fastapi-template.git`, коммит `cc2c58a` ("Initial template: FastAPI + SQLAlchemy + PostgreSQL + Docker"). Лежит в `backend/`. Вложенный `.git` удален, шаблон живет как наш код, апстрим не подтягиваем.

## Стек шаблона

FastAPI 0.129 + SQLAlchemy 2.0 async (asyncpg) + Alembic + PostgreSQL 16. Пакеты через **uv**, линтер Ruff (line-length 150). Python 3.12. JWT на PyJWT, пароли через passlib с argon2.

## Как он работает

Точка входа `src/main.py`. Приложение собирается в `get_application()` и живет с `root_path="/api"`, поэтому **все пути наружу начинаются с `/api`**, а в коде роутеров префикса `/api` нет. Swagger на `/api/docs`.

`lifespan` на старте делает `SELECT 1` в БД и **падает, если БД недоступна**. То есть backend не поднимется без живого postgres, это не «ленивое» подключение.

Три обработчика ошибок дают единый конверт ответа:

```json
{"error": {"code": "...", "message": "...", "details": {}}}
```

- `BusinessError` из `src/core/exceptions.py` — свой код и сообщение, это основной способ бросать ошибки из сервисов.
- `RequestValidationError` — 422, `details` превращается в `{"поле": "текст ошибки"}`.
- 500 — в `production` прячет текст исключения, в остальных окружениях отдает как есть.

Конфиг `src/core/config.py` на pydantic-settings, читает `.env`. Строка подключения не хранится в env, а **собирается** в `SQLALCHEMY_DATABASE_URI` из `POSTGRES_*`. Поэтому в `.env` нужны отдельные переменные, а не готовый `DATABASE_URL`.

`src/core/database.py` держит движок, `async_sessionmaker` и зависимость `get_db()`, которая отдает сессию и делает rollback на исключении. **Commit делает сервис, не зависимость.**

`src/core/base_model.py` — абстрактный `BaseModel` с `id` (BigInteger), `created_at`, `updated_at`. От него наследуются все модели. `src/core/base_schemas.py` — `BaseAppSchema` с `from_attributes=True` и готовый `PaginatedResponse[T]`.

## Структура и как добавлять свое

```
src/
├── core/                 # config, database, base_model, base_schemas,
│                         # security, exceptions, logger, dependencies
├── modules/
│   └── auth/             # образец модуля: models, schemas, service, router, dependencies
└── main.py
migrations/               # alembic, env.py импортирует модели
```

Модуль = папка в `src/modules/<name>/` с пятью файлами: `__init__.py`, `models.py`, `schemas.py`, `service.py`, `router.py`. Разделение слоев: **роутер только принимает запрос и зовет сервис, вся логика в сервисе**, БД-сессия прилетает через `Depends(get_db)`.

Два шага, про которые легко забыть, иначе модуль не заработает:

1. импортировать модели в `migrations/env.py`, иначе alembic их не увидит и миграция выйдет пустой;
2. подключить роутер в `src/main.py` через `app.include_router(...)`.

## Модуль auth как образец

`POST /api/auth/login` принимает email, password, remember_me. Сервис ищет юзера, проверяет пароль, отдает access и refresh токены. `GET /api/auth/me` берет Bearer-токен, `get_current_user` его декодирует и достает юзера.

Токены различаются полем `type`: `access` (30 минут) и `refresh` (1 день, либо 30 дней при `remember_me`). `get_current_user` пускает только `access`.

Модели: `User` (email, hashed_password, full_name, is_active, role_id) и `Role`. `User.role` грузится через `lazy="joined"`, поэтому `/me` отдает роль без отдельного запроса и без проблем с async lazy-load.

Роли захардкожены числами в `src/core/dependencies.py`: admin=1, tech=2, manager=3, плюс готовые зависимости `require_admin` и `require_tech`. Под наш кейс эти значения скорее всего нужно будет переписать.

## Запуск

Запускается не из этой папки, а из корня репозитория: там единственный
`docker-compose.yml`, который собирает `./backend`. Свои compose-файлы шаблона удалены,
причины в [adr/0003-single-compose-entrypoint.md](adr/0003-single-compose-entrypoint.md).

```bash
cp .env.example .env
docker compose up -d --build                                   # или make run
docker compose exec backend alembic upgrade head               # или make migrate
docker compose exec backend python -m src.scripts.seed         # или make seed
```

Режим разработки с автоперезагрузкой и монтированием кода — `make dev`, то есть
оверлей `docker-compose.dev.yml`. По умолчанию контейнер запускается командой из
Dockerfile: gunicorn с четырьмя воркерами uvicorn.

Миграции выполняются **внутри контейнера**. Цели шаблона запускали alembic на хосте
через `uv` с `POSTGRES_HOST=localhost`, это требовало uv на машине и опубликованного
порта БД. Теперь uv на хосте нужен только для тестов и линтера.

## Тесты

Лежат в `backend/tests/`, запускаются `make test`. Сейчас их 26. Покрыто: хеширование
паролей и выпуск токенов, сервис аутентификации, HTTP-слой вместе с конвертом ошибок,
health-эндпоинт и скрипт seed.

Тесты идут на SQLite в памяти, а не на PostgreSQL, поэтому не требуют поднятого стека
и проходят примерно за две секунды. Из-за этого в `src/core/base_model.py` тип
первичного ключа объявлен как `BigInteger().with_variant(Integer, "sqlite")`:
в SQLite `BIGINT` не является алиасом rowid и автоинкремент на нем не работает.
Подробнее: [adr/0004-sqlite-for-tests.md](adr/0004-sqlite-for-tests.md).

HTTP-тесты используют `httpx.ASGITransport`, который не выполняет lifespan, поэтому
приложение в тестах не обращается к настоящей БД.

## Что было починено при заливке

- **Не было `uv.lock`.** Dockerfile делает `COPY pyproject.toml uv.lock ./` и
  `uv sync --frozen`, поэтому сборка падала на `COPY`. Выполнен `uv lock`.
- **`.gitignore` шаблона игнорировал `migrations/versions/`.** Миграции не попадали бы
  в репозиторий, и у dev2 с dev3 просто не было бы таблиц. Строка убрана.
- **Не было начальной миграции.** Создана `migrations/versions/44afa53fd865_initial.py`
  на таблицы `roles` и `users`.
- **Цель `make seed` звала несуществующий модуль.** Написан `src/scripts/seed.py`,
  который создает первого администратора. Он идемпотентен и не хранит пароль в коде:
  пароль берется из `SEED_ADMIN_PASSWORD`, а без этой переменной генерируется случайный
  и печатается один раз. Без этого скрипта получить токен было невозможно, потому что
  регистрации в API нет.
- **Дубли настроек.** Свои `docker-compose.yaml`, `docker-compose.local.yaml`,
  `Makefile`, `.env.example` и `.gitignore` у backend удалены, их роль перешла корневым
  файлам. `backend/README.md` сокращен до ссылки на этот документ.

## Что проверено на живом стеке

Образ собирается, оба контейнера доходят до состояния healthy, миграции применяются
внутри контейнера. Пройден полный сценарий из README: seed, затем `login`, затем `me`
с Bearer-токеном отдает пользователя вместе с ролью. Повторный seed ничего не меняет.
Конверт ошибок работает: неверный пароль дает 401 `INVALID_CREDENTIALS`, запрос без
поля дает 422 с именем поля в `details`, Swagger отвечает на `/api/docs`.

## Что осталось решить

- **Баг в связи `Role.user`.** Объявлена как скаляр `Mapped["User"]`, хотя у роли много
  пользователей. Проверено: с двумя юзерами в одной роли SQLAlchemy выдает
  `SAWarning: Multiple rows returned with uselist=False` и молча отдает одного.
  Правится на `users: Mapped[list["User"]]` плюс переименование `back_populates`.
- **Refresh-токен выдается, но применить его нечем.** Есть `create_refresh_token`
  и схема `RefreshRequest`, а эндпоинта `/auth/refresh` нет. Значит и отзыва токенов нет.
- **Числовые роли в `src/core/dependencies.py`** захардкожены (admin=1, tech=2,
  manager=3) и нигде не используются. До сдачи их нужно приспособить под кейс или
  удалить, иначе это мертвый шаблонный код.
- **passlib 1.7.4 не поддерживается** и при чтении версии argon2 сыпет
  DeprecationWarning. Работает, но если начнет падать, схема хеширования это первое
  место для проверки.
