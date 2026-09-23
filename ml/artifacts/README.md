# Артефакты модели

Сюда обучающая сессия кладет файлы обученной модели. Сервис читает их при старте,
а пока их нет, отвечает по паспортной кривой мощности и помечает ответ `BASELINE_MODEL`.

| Файл | Что внутри | Схема |
|---|---|---|
| `model_info.json` | паспорт модели, `kind: "lightgbm_quantile"` | `ModelInfo` в `src/ml_service/schemas.py` |
| `metrics.json` | качество на отложенном периоде | `ModelMetrics` в `src/ml_service/schemas.py` |
| файлы LightGBM | по одному бустеру на квантиль, например `lgbm_q10.txt` | читает `src/ml_service/predictors/lgbm.py` |

Подробно, как подключить обученную модель: [../README.md](../README.md).
