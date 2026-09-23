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

## Прогноз по своему датасету

| Метод и путь | Отдает | Для какой страницы |
|---|---|---|
| `POST /api/forecast/upload` | `UploadForecastResponse` | загрузка данных пользователя |

Пользователь приносит выгрузку SCADA в формате организаторов и получает по ней
прогноз, не дожидаясь, пока его данные попадут в обучение модели. Кривая мощности
и интервал P10…P90 строятся **из этого же файла**, а ветер берется из погоды
на момент выпуска тем же путем, что и у обычного выпуска: `WeatherGateway`,
тот же `as_of`, никакой погоды, опубликованной позже момента выпуска.

Запрос `multipart/form-data` под тем же токеном, что и остальные эндпоинты:

| Поле | Обязательно | Что это |
|---|---|---|
| `files` | да | один или два CSV, до 25 МБ каждый |
| `issue_date` | нет | день выпуска, по умолчанию `2026-01-31`, допустим диапазон ретро-симуляции |
| `turbine_names` | нет | имена турбин по порядку файлов, по умолчанию `T1` и `T2` |

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "admin"}' | jq -r .access_token)

curl -s -X POST http://localhost:8000/api/forecast/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F 'files=@data/Dataset HackAlemAI turbine 1.csv' \
  -F 'files=@data/Dataset HackAlemAI turbine 2.csv' \
  -F 'issue_date=2026-01-31' \
  -F 'turbine_names=T1' -F 'turbine_names=T2' | jq '.dataset, .kpi'
```

Ответ это обычный `ForecastResponse` (`issue`, `kpi`, `summary`, `hours`) плюс
четыре поля, поэтому фронтенд разбирает часы и KPI тем же кодом:

- `data_source` всегда `uploaded`: числа посчитаны по данным пользователя,
  а не нашей моделью и не заглушкой;
- `dataset` — паспорт комплекта: `files` (`name`, `rows`, `turbine`),
  `period_start`, `period_end`, `hours`, `step_minutes`, `dropped_rows`
  и `drop_reasons` вида «код причины → сколько строк»;
- `power_curve` — эмпирическая кривая: `wind_ms` (центр корзины шириной 0,5 м/с),
  `power_norm` (медиана), `p10`, `p90` (процентили наблюденной мощности),
  `samples`;
- `warnings` — список `{code, message}`.

Как разбирается файл. Колонки узнаются по русским заголовкам («Статистическое
время», «Средняя скорость ветра», «Нормализованная активная мощность»), лишние
игнорируются. 10-минутные строки сворачиваются в часы усреднением, час
засчитывается при четырех точках из шести. Отбрасываются строки с пропусками,
мощностью вне `[0, 1]`, отрицательным ветром и повторами по времени: их число
и причины видны в `drop_reasons`, а не прячутся в лог.

Коды причин отбраковки: `bad_timestamp`, `missing_value`, `negative_wind`,
`power_out_of_range`, `duplicate_time`, `sparse_hour`.

Коды предупреждений: `ROWS_DROPPED` (часть строк не прошла проверку),
`CURVE_INTERPOLATED` (корзины ветра, где меньше 20 наблюдений, достроены
интерполяцией между соседними), `DEGRADED_WEATHER` (погода собрана не в полной
конфигурации источников). Сюда же попадают замечания шага `fetch_weather`,
например переход на запасной источник погоды.

Коды ошибок:

| Код | Когда | HTTP |
|---|---|---|
| `UPLOAD_NO_FILES` | в запросе нет ни одного файла | 400 |
| `UPLOAD_TOO_LARGE` | файл больше 25 МБ | 413 |
| `UPLOAD_BAD_FORMAT` | это не CSV, нет нужных колонок или файлов больше двух | 422 |
| `UPLOAD_BAD_VALUES` | все строки отброшены проверкой или ветер стоит на месте | 422 |
| `UPLOAD_NOT_ENOUGH_DATA` | годных часов меньше 720 | 422 |
| `ISSUE_NOT_FOUND` | день выпуска вне ретро-симуляции | 404 |

Ограничения честно: файл живет ровно один запрос, никуда не сохраняется и на диск
не пишется, поэтому повторный просмотр требует повторной загрузки. Кривая строится
по всему комплекту сразу, отдельной кривой на турбину нет. Отказ источника погоды
здесь не подменяется демонстрационными числами: приходит ошибка границы.

## Погода: сервис dev3

Сервис `weather` в `docker-compose.yml`, образ backend с командой
`uvicorn src.weather_service.main:app`. Адрес из `WEATHER_SERVICE_URL` (`http://weather:8000`),
таймаут из `WEATHER_SERVICE_TIMEOUT`. Контракт согласован в #37, фактическая схема —
[dev3/weather-openapi.json](dev3/weather-openapi.json), генерируется из кода и проверяется тестом.
Swagger на `http://localhost:${WEATHER_PORT}/docs`, пояснения в
[dev3/weather-service.md](dev3/weather-service.md).

```
GET /health       -> ok или degraded, прогоны и период кэша по источникам, часов SCADA
GET /sources      -> реестр: тип API, шаг, задержка публикации, высоты, пустые колонки,
                     период архива, состав ансамбля, MAE ветра
GET /nwp?source=gfs,ensemble&as_of=2026-01-31T02:00:00Z[&from=...&to=...]
    -> {"as_of_utc", "from_utc", "to_utc", "sources", "missing",
        "rows": [{valid_time_utc, source, run_init_utc, available_at_utc, lead_h,
                  ws10, ws80, ws100, ws120, wd100, gust10, t2m, rh2m, psfc, ws_spread, members}]}
    Ни одной строки с available_at_utc > as_of. Окно по умолчанию — as_of+1 ч … as_of+48 ч.
    В каждой строке есть ветер хотя бы на одной высоте и t2m.
GET /runs?as_of=...[&issue_time=...&source=...&status=used,stale,after_as_of][&from=...&to=...]
    -> [{source, run_init_utc, available_at_utc, status, lead_from_h, lead_to_h, hours_used}]
GET /runs?from=...&to=...  -> события «прогон стал доступен» в (from, to] для пересчета
GET /scada?from=...&until=...[&turbine=T1]
    -> [{time_utc, turbine, power_norm, wind_ms, temp_c, flag}]
```

Как это читает backend (`HttpWeatherSource`):

- `GET /nwp?source=<один источник>&as_of=&from=&to=`, строки берутся из `rows` (схема
  `NwpResponse`), остальные поля конверта игнорируются. `t2m` в `NwpRow` обязательна: у часа,
  где у свежего прогона нет температуры, сервис отдает прогон старше, как и для часов без ветра.
- `GET /runs?as_of=&from=&to=` — прогоны, покрывающие горизонт выпуска и ставшие доступными
  в `(from, to]`: по ним агент решает, пересчитывать ли выпуск.
- 404 `NO_RUN_AVAILABLE` — штатная ситуация, а не сбой: агент берет следующий источник
  и пишет решение в журнал с кодом `FALLBACK`. Сервис не ответил совсем или ответил
  не по контракту — агент переключается на запасной путь, `AsOfStore` в своем процессе
  поверх того же кэша, и пишет это в журнал решением `use_spare_weather`.

Строки `rows` уходят в `POST /predict` как есть, схема `WeatherRow` у ML-сервиса. Строки
`ensemble` туда не отправляются вместе с участниками: ML-сервис сам усредняет источники.
Статус `after_as_of` соответствует `after_issue` в `WeatherRun`. Источники: `ifs`, `ifs025`,
`gfs`, `icon`, `gem`, `ensemble`. Ошибки в общем конверте: `NO_RUN_AVAILABLE` (404),
`UNKNOWN_SOURCE` и `VALIDATION_ERROR` (422), `LEAKAGE_GUARD` (500, не должен случаться никогда),
`DATA_UNAVAILABLE` (503, нет SCADA).

Совместимость с backend и ML-сервисом проверяет `backend/tests/weather_service/test_compat.py`
по копиям их схем. Внутри сервиса лежит пакет `backend/src/forecast/weather/`: `AsOfStore`
отдает только прогоны, опубликованные к моменту прогноза, и бросает `LeakageError`
на данные из будущего. Описание зоны dev3: [dev3/README.md](dev3/README.md).

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
