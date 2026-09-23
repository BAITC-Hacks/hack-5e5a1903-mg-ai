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
POST /api/auth/login     {"email": "...", "password": "..."}  ->  {access_token, refresh_token}
GET  /api/auth/me        Authorization: Bearer <access_token>
```

Все эндпоинты прогноза требуют заголовок `Authorization: Bearer <access_token>`.
Без него приходит 401 в общем конверте ошибки. Вход `admin:admin` делается
отдельной задачей, до нее пользователь создается скриптом `make seed`.

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

## Что нужно от dev3: сервис погоды

Адрес берется из `WEATHER_SERVICE_URL`, таймаут из `WEATHER_SERVICE_TIMEOUT`.
Предлагаемая форма, обсуждаем и правим:

```
GET /nwp?source=ecmwf_ifs&as_of=2026-01-31T02:00:00Z&from=...&to=...
    -> [{valid_time_utc, source, run_init_utc, available_at_utc, lead_h,
         ws80, ws100, ws120, wd100, gust10, t2m, rh2m, psfc}]
    Обязательное правило: ни одной строки с available_at_utc > as_of.

GET /runs?from=...&to=...
    -> [{source, run_init_utc, available_at_utc}]
    События «прогон стал доступен», по ним агент пересчитывает выпуск.

GET /scada?until=2026-01-31T02:00:00Z
    -> [{time_utc, turbine, power_norm, wind_ms, temp_c, flag}]
```

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
