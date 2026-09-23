# Документация проекта

Что требует ТЗ и чекбоксы готовности: [../GOAL.md](../GOAL.md). Исходное ТЗ и
контрольные документы лежат в [../TZ/](../TZ/).

Здесь описывается вся архитектура. `CLAUDE.md` ссылается сюда одной строкой,
а этот файл ссылается на остальное.

## Архитектура и код

- Компоненты, диаграммы, стек, структура репозитория: [ARCHITECTURE.md](ARCHITECTURE.md)
- Best practices, по которым пишем код: [architecture-guidelines.md](architecture-guidelines.md)
- Предложенные, но не внедренные инструменты: [proposals.md](proposals.md)
- Backend: разбор FastAPI-шаблона, как добавлять модули: [backend.md](backend.md)
- Контракт API между фронтендом, бэкендом и сервисами команды: [api-contract.md](api-contract.md)
- ML-сервис: эндпоинты, контракт, как подключить обученную модель: [../ml/README.md](../ml/README.md)
- Почему приняты основные решения: [adr/](adr/)
- Исследование нашего кейса про прогноз выработки ВЭС, бриф и разбор: [research/wind-forecast-case/](research/wind-forecast-case/)

## Процесс

- Ветки, коммиты, PR, запрет прямого push в main: [git-workflow.md](git-workflow.md)
- Проверка зависимостей на уязвимости перед мерджем: [dependency-audit.md](dependency-audit.md)
- Worktree на фичу, слоты портов, снос после мерджа: [worktrees.md](worktrees.md)
- Как команда работала с ИИ: [ai-workflow.md](ai-workflow.md)
- Чеклист перед сдачей: [pre-submit-checklist.md](pre-submit-checklist.md)

## Зоны ответственности

- dev1: [dev1/README.md](dev1/README.md)
- dev2: [dev2/README.md](dev2/README.md)
- dev3: [dev3/README.md](dev3/README.md)

Правило: новый модуль или сервис получает свой `.md` здесь и строку в этом списке.
Значимое решение получает запись в `adr/`.
