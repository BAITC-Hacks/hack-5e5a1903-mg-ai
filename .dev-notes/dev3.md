# Заметки dev3

## Сейчас делаю

-

## Сделано

- #9 и #10: `load_scada()`, флаги, `reports/scada_summary.md`, `reports/tz_check.md`.

## Нужно от других

- dev1 (#5): когда появится `src/forecast/config.py`, перенести туда пути, паспорт турбины и
  `SCADA_UTC_OFFSET_H` из `src/forecast/dataset/config.py`.
- dev2 (#27): `flag` в ScadaHistory — строка, пустая для чистого часа. Для обучения берите часы
  с `flag == ""`. Когда появится `ml/schema.py`, `load_scada()` перейдет на колонки оттуда.
- dev2 (#11): в флаге обледенения паспортная кривая между 3 и 10,3 м/с растет как куб скорости
  (`passport_curve` в `dataset/scada.py`). Если бейзлайн «curve» возьмет другую форму,
  стоит сойтись на одной.
