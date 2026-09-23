# Контракт API

Договоренность между интерфейсом, центральным бэкендом и сервисами команды.
Схемы живут в `backend/src/modules/forecast/schemas.py`, они и есть источник истины.
Формы полей не меняются в одностороннем порядке: на них завязан фронтенд.

Роли: **dev3** отдает погоду и историю турбин, **dev2** отдает предсказание модели,
**dev1** собирает это в центральном бэкенде и отдает интерфейсу.

```
frontend  ->  backend (dev1)  ->  weather (dev3)
                              ->  ml      (dev2)
                              ->  postgres
```

## Что уже работает

Все эндпоинты ниже подняты и покрыты тестами (`backend/tests/test_forecast_api.py`).
Числа пока синтетические: каждый ответ несет поле `data_source` со значением `stub`.
Когда данные начнут приходить из сервисов dev2 и dev3, значение станет `live`,
а формы ответов не изменятся. Интерфейс показывает это значение пользователю,
чтобы демонстрационные числа нельзя было принять за настоящие.

## Авторизация

```
POST /api/auth/login     {"username": "admin", "password": "admin"}  ->  {access_token, refresh_token}
GET  /api/auth/me        Authorization: Bearer <access_token>
```

Идентификатор пользователя передается ключом `username` или ключом `email`, это
псевдонимы одного поля: интерфейс волен слать любой из двух. Значение не обязано быть
почтой. Пользователь создается скриптом `make seed`, по умолчанию это демонстрационная
пара **admin/admin**, ее можно переопределить через `SEED_ADMIN_EMAIL` и
`SEED_ADMIN_PASSWORD` до первого запуска скрипта.

Все эндпоинты прогноза требуют заголовок `Authorization: Bearer <access_token>`.
Без него приходит 401 в общем конверте ошибки. Неверный пароль дает 401 с кодом
`INVALID_CREDENTIALS`, тело без идентификатора — 422 с полем `email` в `details`.

## Эндпоинты прогноза

| Метод и путь | Отдает | Для какой страницы |
|---|---|---|
| `GET /api/forecast/issues` | страница из `IssueSummary`, всего 28 | шапка, выбор дня |
| `GET /api/forecast/{date}` | `ForecastResponse`: 48 часов на турбину, KPI, вывод агента | Обзор |
| `GET /api/forecast/{date}/agent-log` | список `AgentDecision` | Агент |
| `GET /api/forecast/{date}/weather` | `WeatherResponse`: прогоны, модели, ансамбль | Погода |
| `GET /api/forecast/{date}/dispatch?risk=0.2` | `DispatchResponse`: заявка на сутки D | Диспетчер |
| `POST /api/forecast/{date}/recompute` | `ForecastResponse` заново | кнопка «Пересчитать» |
| `GET /api/forecast/backtest` | `BacktestMetrics` | Бэктест |
| `GET /api/forecast/model` | `ModelInfo` | Модель |
| `GET /api/forecast/site` | `SiteInfo` | Объект |

Даты выпуска: с 31.01.2026 по 27.02.2026, момент выпуска 02:00 UTC, то есть
07:00 по Астане накануне целевых суток. Дата вне этого диапазона дает 404
с кодом `ISSUE_NOT_FOUND`.

Единицы и соглашения:

- `p10`, `p50`, `p90` в долях номинала, от 0 до 1, всегда `p10 <= p50 <= p90`;
- `p50_mw` мощность одной турбины в МВт, номинал 2,5 МВт, станция 5 МВт;
- время в UTC в полях `*_utc`, местное время UTC+5 в полях `*_local`;
- `lead_h` от 1 до 48;
- `flags`: `cut_out_risk`, `icing_risk`, `ramp`, `degraded`, `source_spread`.

## Погода: сервис dev3

Адрес из `WEATHER_SERVICE_URL`, таймаут из `WEATHER_SERVICE_TIMEOUT`. Полная схема
и описание приедут вместе с кодом сервиса: `docs/dev3/weather-openapi.json`
и `docs/dev3/weather-service.md`.

- `GET /nwp?source=...&as_of=...` — строки погоды. Окно по умолчанию это горизонт
  +1…+48 ч, строк с `available_at_utc` позже `as_of` в ответе не бывает. Эти строки
  уходят в `POST /predict` ML-сервиса **как есть**, без переименования полей.
- Источник `ensemble` нельзя отправлять в `/predict` вместе с его участниками:
  ML-сервис усредняет источники сам.
- `GET /runs?as_of=...` — статусы прогонов для страницы «Погода». Их `after_as_of`
  это наш `after_issue`.
- `NO_RUN_AVAILABLE` (404) — штатная ситуация, а не сбой: агент берет следующий
  источник и пишет решение в журнал с кодом `FALLBACK`.

Внутри сервиса лежит пакет `backend/src/forecast/weather/`: `AsOfStore` отдает только
прогоны, опубликованные к моменту прогноза, и бросает `LeakageError` на данные
из будущего. Описание зоны dev3: [dev3/README.md](dev3/README.md).

## Что нужно от dev2: сервис модели

Адрес из `ML_SERVICE_URL`, таймаут из `ML_SERVICE_TIMEOUT`. Контракт dev2 принят на основе
предложения выше, полностью описан в [../ml/openapi.json](../ml/openapi.json), Swagger
на `http://localhost:8010/docs`, пояснения в [../ml/README.md](../ml/README.md).

```
POST /predict
    {"issue_time_utc": "...", "rows": [ <строки погоды из /nwp как есть> ],
     "options": {"turbines": ["T1", "T2"], "interval_scale": 1.0, "wind_shift_ms": 0.0}}
    -> {"model": {...}, "capacity_mw": {...}, "degraded": false, "warnings": [...],
        "forecast": [{valid_time_utc, lead_h, turbine, p10, p50, p90}],
        "hourly_inputs": [{valid_time_utc, wind_speed_hub_ms, wind_spread_ms, t2m, sources}]}

GET /model-info   -> ModelInfo: имя, квантили, окно обучения, train_rows, признаки с важностью,
                     кривая мощности {wind_ms, power_norm}, walk_forward
GET /metrics      -> ModelMetrics: nmae_d1_pct, nmae_d2_pct, nrmse_48_pct, skill_vs_persistence_pct,
                     coverage_p10_p90_pct, baselines, by_day, by_lead, series
GET /health       -> ok, если загружена обученная модель; degraded, пока работает кривая мощности
```

Ответ — объект, а не список: в нем версия модели, признак `degraded` и предупреждения
для журнала агента. Ползунок «что если» на «Обзоре» — тот же `POST /predict`
с `wind_shift_ms`, пересчет с широким интервалом — с `interval_scale`. Ошибки в общем
конверте, `422` означает ошибку во входных данных, например `LEAKAGE_DETECTED`.

## Правила для обеих границ

- У каждого вызова наружу явный таймаут из переменной окружения.
- Отказ соседнего сервиса превращается в `BusinessError` с понятным кодом,
  а не в 500 и не в пустой экран.
- Недоступность основного источника погоды это штатный сценарий: агент уходит
  на запасной и помечает выпуск флагом `degraded`.
- Новых переменных окружения без записи в `.env.example` не бывает.

## Как backend ходит к соседям

Реализация: `backend/src/modules/forecast/`. Цикл агента живет в `orchestrator.py`,
клиенты границ в `clients/`, предметные проверки в `analyze.py`, журнал решений
в `decisions.py`, демонстрационные числа в `service.py`.

Порядок шага `fetch_weather`: агент спрашивает у ML-сервиса `GET /model-info`, берет
`inputs.sources` (на каких моделях погоды обучена модель) и запрашивает именно их.
Паспорт недоступен или источников не называет — берется список по умолчанию
из `config.DEFAULT_SOURCES`. Источник `ensemble` вместе с его участниками
не запрашивается: ML усредняет модели сам.

Источник погоды спрятан за одним интерфейсом `WeatherSource` с тремя реализациями:

| Реализация | Что это | Когда работает |
|---|---|---|
| `HttpWeatherSource` | сервис погоды dev3, `WEATHER_SERVICE_URL` | основной путь |
| `LocalWeatherSource` | тот же кэш прогонов пакетом в образе, `src/forecast/weather` | сервис не ответил |
| `WeatherGateway` | связка первых двух | то, что создает агент |

Переключение на запасной источник пишется в журнал решений, молча оно не происходит.
Ответ `404 NO_RUN_AVAILABLE` от сервиса это штатная ситуация, а не отказ: агент берет
следующий источник и пишет решение с кодом `FALLBACK`.

Коды ошибок границы. Разные причины дают разные коды, все приходят в общем конверте:

| Код | Когда | HTTP |
|---|---|---|
| `WEATHER_NO_RUN`, `ML_TIMEOUT`, `ML_UNAVAILABLE`, `ML_REJECTED`, `ML_BAD_RESPONSE`, `WEATHER_TIMEOUT`, `WEATHER_UNAVAILABLE`, `WEATHER_BAD_RESPONSE` | сосед не ответил или ответил не по контракту | не доходит до клиента: выпуск собирается заглушкой, код виден в журнале агента |
| `WEATHER_LEAKAGE` | погода опубликована позже момента выпуска | 502 |
| `METRICS_NOT_AVAILABLE` | модель еще не оценена, страница «Бэктест» | 404 |
| `ISSUE_NOT_FOUND` | дата вне ретро-симуляции | 404 |

`data_source` становится `live` только когда данные действительно пришли от соседей.
Отказ соседа не роняет запрос: пользователь получает демонстрационный выпуск
с `data_source="stub"`, а причина видна на странице «Агент». Исключение одно — утечка
будущего: ее не подменяют демонстрацией, а показывают ошибкой.
