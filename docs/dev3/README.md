# dev3

## Зона ответственности

Все входные данные прогноза: погода, история турбин (SCADA), часовой пояс, сборка таблиц
для модели (#2). Пакеты `backend/src/forecast/weather/` и `backend/src/forecast/dataset/`.

## Что делает

- Отдает погоду строго на момент прогноза, без прогонов, опубликованных позже (#7).
- Отдает историю турбин по часам в UTC с флагами очистки (#9), пояс SCADA проверен (#10).

## Файлы и модули

- `backend/src/forecast/weather/sources.py` — реестр источников погоды: шаг прогонов,
  задержка публикации и ее обоснование, папка кэша. После #5 переедет в конфиг.
- `backend/src/forecast/weather/asof.py` — `AsOfStore`: `get_nwp`, `get_nwp_multi`,
  `run_events`, ошибки `LeakageError` и `NoRunAvailable`, проверка `check_no_leakage`.
- `backend/src/forecast/dataset/scada.py` — `load_scada()`: ScadaHistory из #2, обе турбины
  в длинном формате, часовой шаг, `time_utc`, флаги очистки (#9).
- `backend/src/forecast/dataset/config.py` — пути (`DATA_DIR`, `REPORTS_DIR`), правило пояса
  `SCADA_UTC_OFFSET_H`, паспорт GW109 и пороги флагов.
- `backend/src/forecast/dataset/scada_summary.py` — генерирует `reports/scada_summary.md`.
- `backend/src/analysis/tz_check.py` — проверка пояса SCADA (#10), генерирует `reports/tz_check.md`.
  Использует ERA5 как дополнительную диагностику, поэтому лежит вне `src/forecast`.
- Тесты: `backend/tests/forecast/test_asof.py` (синтетический кэш в `synthetic_nwp.py`),
  `backend/tests/forecast/test_scada.py`, `backend/tests/test_tz_check.py`.

Отчеты по SCADA пересобираются из папки `backend`, сеть не нужна:

```bash
uv run python -m src.forecast.dataset.scada_summary
uv run python -m src.analysis.tz_check
```

## Заметки по архитектуре своей части

- Прогон доступен в момент `run_init_utc + задержка источника`. Задержки выбраны
  консервативно: лишний час стоит немного точности, недостающий час — это утечка.
- Для каждого часа берется самый свежий доступный прогон. Если хоть для одного
  запрошенного часа прогона нет, `get_nwp` бросает `NoRunAvailable`, а не отдает неполную таблицу.
- Прогоны вне шага источника (например 06z и 18z у GEM) отбрасываются при загрузке
  с предупреждением в логе.
- Кэш читается один раз на экземпляр `AsOfStore`, а не на уровне модуля.
- Время SCADA — UTC+6 на всём периоде, перехода на UTC+5 01.03.2024 в данных нет.
- Час SCADA в `load_scada()` — среднее записей hh:00…hh:50 с меткой начала часа. Для сравнения
  с мгновенными значениями погоды (`tz_check`) часы центрируются на hh:00.
- Флаги: `frozen_sensor`, `cut_out`, `icing`, `downtime`, `curtailment`, пустая строка для чистого часа.
  Один флаг на час, по приоритету из `FLAG_PRIORITY`.
