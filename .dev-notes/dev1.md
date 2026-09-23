# Заметки dev1

## Сейчас делаю

- #19: после мерджа #5 перевожу цели `replay`, `backtest` и `leakcheck`
  на `$(PIPELINE)` вторым коммитом.

## Сделано

- 2026-09-23: #19, сервис `pipeline` в compose, каталоги `outputs/` и `reports/`,
  цель `make pipeline`.
- 2026-09-23: #4, GOAL.md под кейс ВЭС, research помечен как выбранный кейс.

## Нужно от других

- #5 (сессия hackalem-ai-ad, ветка `features-dev1-pipeline-skeleton`): точка входа
  образа уже настроена на `python -m src.forecast.cli` с рабочим каталогом `/app`.
  Цели Makefile для пайплайна в #5 остаются на `uv`, на Docker их переведу я.
