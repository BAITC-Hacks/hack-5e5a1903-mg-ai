# ML-сервис прогноза выработки

Зона dev2. HTTP-сервис принимает прогнозы погоды, доступные на момент T, и возвращает
вероятностный прогноз P10/P50/P90 для турбин T1, T2 и станции целиком на часы T+1 … T+48.
Почему это отдельный сервис: [../docs/adr/0006-ml-service.md](../docs/adr/0006-ml-service.md).

- Контракт: [openapi.json](openapi.json). Файл генерируется из `src/ml_service/schemas.py`,
  тест `tests/test_openapi.py` падает, если файл разошелся с кодом.
- Swagger: `http://localhost:8010/docs` (порт из `ML_PORT`).
- Вызывает сервис только backend. Frontend к нему напрямую не ходит.

## Эндпоинты

| Метод | Путь | Зачем | Где на дашборде |
|---|---|---|---|
| `GET` | `/health` | жив ли сервис и какая модель загружена | статус агента |
| `POST` | `/predict` | прогноз на момент T | «Обзор», «Диспетчер», «Объект», шаг `run_model` агента, ползунок «что если», пересчет |
| `GET` | `/model-info` | паспорт модели: входы, признаки с важностью, кривая мощности, окно обучения | «Модель» |
| `GET` | `/metrics` | качество на отложенном периоде: D+1, D+2, по дням, по часам горизонта, прогноз и факт | «Бэктест», KPI «Ожидаемая ошибка», недобор на «Диспетчере» |

## Как backend получает прогноз

1. Берет строки погоды из `GET /nwp?as_of=T` сервиса погоды dev3 и кладет их в поле `rows` как есть:
   имена `ws80`, `ws100`, `ws120`, `wd100`, `gust10`, `t2m`, `rh2m`, `psfc` те же, незнакомые поля
   игнорируются. Скорости ветра в м/с: у Open-Meteo нужно запрашивать `wind_speed_unit=ms`.
2. Отправляет `POST /predict` с таймаутом `ML_SERVICE_TIMEOUT`.
3. `200`: `p10`, `p50`, `p90` — доли от номинала, умножаются на `capacity_mw` и дают МВт.
   `422`: ошибка во входных данных, причина в `error.code`. Таймаут или `5xx`: агент
   переходит на запасной прогноз и помечает выпуск как `degraded`.

Сценарии дашборда не требуют отдельных эндпоинтов:

- ползунок «что если» — тот же запрос с `options.wind_shift_ms`;
- пересчет с широким интервалом, когда модели погоды расходятся, — `options.interval_scale`;
- по умолчанию прогноз на `T1` и `T2`, 96 строк; станция целиком — `options.turbines: ["station"]`.

```bash
curl -s localhost:8010/predict -H 'Content-Type: application/json' -d '{
  "issue_time_utc": "2026-01-31T02:00:00Z",
  "horizon_hours": 1,
  "rows": [{"valid_time_utc": "2026-01-31T03:00:00Z", "source": "ecmwf_ifs",
            "run_init_utc": "2026-01-30T18:00:00Z", "available_at_utc": "2026-01-31T01:30:00Z",
            "ws100": 8.4, "wd100": 255, "t2m": -6.1}]
}'
```

## Ошибки

Всегда конверт `{"error": {"code", "message", "details"}}`, как в backend.

| Код | HTTP | Когда |
|---|---|---|
| `VALIDATION_ERROR` | 422 | запрос не по схеме: нет обязательного поля, значение вне диапазона |
| `ISSUE_TIME_NOT_ON_HOUR` | 422 | `issue_time_utc` не начало часа |
| `LEAKAGE_DETECTED` | 422 | есть прогон погоды, доступный позже момента прогноза |
| `INCONSISTENT_RUN_TIMES` | 422 | `run_init_utc` позже `available_at_utc` |
| `OUT_OF_HORIZON` | 422 | строка погоды не на один из часов T+1 … T+horizon |
| `DUPLICATE_ROWS` | 422 | две строки на один час и одну модель погоды |
| `INSUFFICIENT_INPUTS` | 422 | на какой-то час нет скорости ветра ни от одной модели погоды |
| `MISSING_REQUIRED_SOURCE` | 422 | нет обязательной для модели модели погоды на весь горизонт |
| `METRICS_NOT_AVAILABLE` | 404 | метрики модели еще не посчитаны |
| `INTERNAL_ERROR` | 500 | ошибка сервиса, текст наружу не отдается |

## Как подключить обученную модель

Пока в `artifacts/` нет обученной модели, сервис отвечает по паспортной кривой GW109,
`/health` показывает `degraded`, а в ответе есть предупреждение `BASELINE_MODEL`.
Чтобы подключить LightGBM:

1. **Признаки строятся тем же кодом, что при прогнозе.** Для каждого исторического выпуска
   вызвать `ml_service.frame.build_frame_from_rows(issue_time, 48, rows)`, где `rows` —
   строки погоды в формате `WeatherRow`, доступные к моменту выпуска. Получится таблица
   `frame.wide` с колонками `lead_h` и `<source>__<переменная>`, например `ecmwf_ifs__ws100`
   и `ecmwf_ifs__hub`. Цель — факт мощности SCADA за `valid_time_utc`.
2. Обучить по бустеру на квантиль: `objective="quantile"`, `alpha` 0.1, 0.5 и 0.9.
   Сохранить в `artifacts/`, например `lgbm_q10.txt`.
3. Написать `src/ml_service/predictors/lgbm.py` с функцией `load(artifacts_dir, info)`.
   Она возвращает объект с полем `info` и методом `predict(frame, turbines)`, который отдает
   `valid_time_utc, turbine, p10, p50, p90`. Сортировку квантилей, обрезку в [0, 1]
   и расширение интервала делает сервис.
4. `artifacts/model_info.json` по схеме `ModelInfo` с `kind: "lightgbm_quantile"`.
   В `inputs.sources` перечислить модели погоды, на которых училась модель: backend читает
   этот список через `GET /model-info` и знает, какие источники запрашивать у сервиса погоды.
5. `artifacts/metrics.json` по схеме `ModelMetrics`: D+1 — lead 1–24, D+2 — lead 25–48.

Если модель не загрузилась, сервис пишет ошибку в лог и продолжает работать на кривой мощности.

LightGBM на macOS требует OpenMP: `brew install libomp`. В Docker-образе нужная библиотека уже стоит.

## Запуск

```bash
docker compose up -d --build ml          # в составе стека, Swagger на :8010/docs
make ml-install && cd ml && uv run uvicorn ml_service.main:app --app-dir src --port 8010   # локально
make ml-test && make ml-lint              # проверки
make ml-openapi                           # после изменения схем: обновить openapi.json
```
