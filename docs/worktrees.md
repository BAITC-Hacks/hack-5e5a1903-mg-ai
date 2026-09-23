# Worktree на каждую фичу

Каждая фича живет не только в своей ветке, но и в **своем git worktree**. Это позволяет
пилить несколько фич параллельно на одной машине: каждый worktree — отдельная папка
со своим `.env`, своими контейнерами и своими портами.

Почему так, а не переключение ветками: [adr/0002-worktree-per-feature.md](adr/0002-worktree-per-feature.md).
Ветки и PR: [git-workflow.md](git-workflow.md).

## Где лежат worktree

Рядом с репозиторием, не внутри него:

```
C:\meirzhan\dev\
├── HACKALEM AI\                        # main
└── hackalem-worktrees\
    ├── features-dev1-auth\
    └── features-dev1-chat-ui\
```

## Создание

```bash
# из основного репозитория
git worktree add ../hackalem-worktrees/features-dev1-auth -b features-dev1-auth
cd ../hackalem-worktrees/features-dev1-auth
cp ../../HACKALEM\ AI/.env .env
```

Дальше в `.env` этого worktree выставляем **свой свободный слот портов** и свое имя
compose-проекта:

```env
COMPOSE_PROJECT_NAME=hackalem-dev1-auth
FRONTEND_PORT=3001
BACKEND_PORT=8001
DB_PORT=5433
```

`COMPOSE_PROJECT_NAME` обязателен и уникален. Без него контейнеры и volume разных
worktree столкнутся именами, и один compose будет гасить другой.

Важно: меняются только `FRONTEND_PORT`, `BACKEND_PORT` и `DB_PORT` — это порты,
публикуемые на хост. Переменные `POSTGRES_HOST=db` и `POSTGRES_PORT=5432` трогать не надо,
это адрес БД внутри сети compose, и он одинаковый во всех worktree.

Запуск как обычно, из папки worktree:

```bash
docker compose up -d --build
docker compose exec backend alembic upgrade head
```

## Слоты портов

| Слот | frontend | backend | postgres | ml   | Кому                   |
|------|----------|---------|----------|------|------------------------|
| 0    | 3000     | 8000    | 5432     | 8010 | основной чекаут (main) |
| 1    | 3001     | 8001    | 5433     | 8011 | свободный слот         |
| 2    | 3002     | 8002    | 5434     | 8012 | свободный слот         |
| 3    | 3003     | 8003    | 5435     | 8013 | свободный слот         |
| 4    | 3004     | 8004    | 5436     | 8014 | свободный слот         |

Слот 0 всегда у основного репозитория, его не занимаем под фичи. Кто какой слот взял
прямо сейчас — в реестре [../.dev-notes/worktrees.md](../.dev-notes/worktrees.md).
Берем слот только после того, как посмотрели реестр, и сразу вписываем себя туда.

Оговорка про машину dev1: на ней порты 3000 и 8000 уже заняты другими проектами,
поэтому слот 0 там не поднимется и основной чекаут использует сдвинутые порты.
В `.env.example` при этом лежат стандартные 3000, 8000 и 5432, потому что именно
их ожидает README и по ним проект будут запускать снаружи.

## Реестр

Как только worktree создан, в [../.dev-notes/worktrees.md](../.dev-notes/worktrees.md)
добавляется строка: ветка, папка, dev, машина, слот и порты. Реестр коммитится,
поэтому все трое видят, какие порты уже заняты.

## Снос после мерджа

**Сразу после мерджа PR** worktree убирается, а порты освобождаются. Не откладываем,
иначе слоты кончатся и порты будут висеть занятыми.

```bash
# в папке worktree: погасить контейнеры и удалить его volume
docker compose down -v

# из основного репозитория
git worktree remove ../hackalem-worktrees/features-dev1-auth
git worktree prune
```

Затем **удалить строку этого worktree из реестра** `.dev-notes/worktrees.md`
и закоммитить. Слот снова считается свободным. Этот пункт есть в чеклисте
шаблона pull request, чтобы о нем не забывали.

Проверить, что осталось:

```bash
git worktree list
docker compose ls
```
