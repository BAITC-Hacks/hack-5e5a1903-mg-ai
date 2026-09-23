"""Прогноз по датасету пользователя: разбор CSV, кривая из данных, выпуск.

Файлы генерируются прямо здесь: настоящие датасеты организаторов весят по 6 МБ,
и класть их в репозиторий ради теста незачем. Погода подменяется поддельным
источником из ``test_forecast_gateway``, чтобы тест не зависел ни от сети,
ни от кэша прогнозов.
"""

import io
import random
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from src.core.security import create_access_token
from src.modules.auth.models import User
from src.modules.forecast import orchestrator, upload
from src.modules.forecast.config import CUT_IN_MS, CUT_OUT_MS, HORIZON_HOURS, RATED_MS
from tests.test_forecast_gateway import FakeWeather

UPLOAD = "/api/forecast/upload"
ISSUE = "2026-01-31"
HEADER = "ID,Статистическое время,Средняя скорость ветра(m/s),Нормализованная активная мощность,Средняя температура окружающей среды(°C)"


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def weather(monkeypatch):
    """Погода отвечает по контракту, но из теста, а не из сети."""

    def setup(source: FakeWeather | None = None) -> FakeWeather:
        fake = source or FakeWeather()
        monkeypatch.setattr(orchestrator, "weather_source", lambda: fake)
        return fake

    return setup


# --- генератор датасета в формате организаторов --------------------------


def reference_power(wind_ms: float) -> float:
    """Опорная кривая, по которой рисуются данные. В коде ее нет, только в тесте."""
    if wind_ms < CUT_IN_MS or wind_ms >= CUT_OUT_MS:
        return 0.0
    if wind_ms >= RATED_MS:
        return 1.0
    return ((wind_ms - CUT_IN_MS) / (RATED_MS - CUT_IN_MS)) ** 3


def stamp(moment: datetime) -> str:
    """Время ровно в том виде, в каком его пишут организаторы: час без нуля слева."""
    return f"{moment:%Y-%m-%d} {moment.hour}:{moment.minute:02d}:{moment.second:02d}"


def dataset_csv(
    *,
    hours: int = 900,
    points_per_hour: int = 6,
    start: datetime = datetime(2025, 1, 1),
    seed: int = 7,
    extra_rows: list[str] | None = None,
    extra_column: str | None = None,
) -> bytes:
    """CSV в формате организаторов: 10-минутный шаг, мощность от 0 до 1.

    Ветер метет весь рабочий диапазон, чтобы корзины кривой набрали наблюдения.
    Шум добавляется и внутри часа, и на час целиком: второй переживает
    усреднение и дает корзинам тот самый наблюденный разброс P10…P90.
    """
    rng = random.Random(seed)
    step = timedelta(minutes=60 // points_per_hour)
    tail = f",{extra_column}" if extra_column else ""
    lines = [HEADER + (",Служебная колонка" if extra_column else "")]
    number = 0
    for hour in range(hours):
        base = round((hour * 20.0) / hours + rng.uniform(-0.2, 0.2), 2) if hours else 0.0
        offset = rng.uniform(-0.08, 0.08)
        for point in range(points_per_hour):
            number += 1
            moment = start + timedelta(hours=hour) + point * step
            wind = max(0.0, round(base + rng.uniform(-0.1, 0.1), 2))
            power = min(1.0, max(0.0, round(reference_power(wind) + offset + rng.uniform(-0.03, 0.03), 4)))
            lines.append(f"{number},{stamp(moment)},{wind},{power},{round(rng.uniform(-10, 25), 2)}{tail}")
    lines.extend(extra_rows or [])
    return ("\n".join(lines) + "\n").encode("utf-8")


def csv_part(content: bytes, name: str = "turbine-1.csv") -> tuple[str, tuple[str, io.BytesIO, str]]:
    return ("files", (name, io.BytesIO(content), "text/csv"))


async def post(client: AsyncClient, user: User, parts: list, **form):
    return await client.post(UPLOAD, headers=auth(user), files=parts, data={"issue_date": ISSUE, **form})


# --- основной путь -------------------------------------------------------


async def test_upload_requires_a_token(client: AsyncClient):
    response = await client.post(UPLOAD, files=[csv_part(dataset_csv(hours=10))])

    assert response.status_code == 401


async def test_a_good_pair_of_files_gives_a_forecast_for_both_turbines(client: AsyncClient, user: User, weather):
    weather()
    parts = [csv_part(dataset_csv(seed=1), "turbine-1.csv"), csv_part(dataset_csv(seed=2), "turbine-2.csv")]

    response = await post(client, user, parts, turbine_names=["T1", "T2"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data_source"] == "uploaded"
    assert len(body["hours"]) == HORIZON_HOURS * 2
    assert {hour["turbine"] for hour in body["hours"]} == {"T1", "T2"}
    assert sorted({hour["lead_h"] for hour in body["hours"]}) == list(range(1, HORIZON_HOURS + 1))
    assert all(hour["p10"] <= hour["p50"] <= hour["p90"] for hour in body["hours"])
    assert [file["name"] for file in body["dataset"]["files"]] == ["turbine-1.csv", "turbine-2.csv"]
    assert body["dataset"]["hours"] == 1800


async def test_the_interval_comes_from_the_data_and_is_not_degenerate(client: AsyncClient, user: User, weather):
    weather()

    response = await post(client, user, [csv_part(dataset_csv())])

    assert response.status_code == 200, response.text
    hours = response.json()["hours"]
    widths = [hour["p90"] - hour["p10"] for hour in hours]
    # Вырожденный интервал это либо нулевая ширина, либо весь диапазон 0…1.
    assert all(width > 0.01 for width in widths), widths
    assert not any(hour["p10"] == 0.0 and hour["p90"] == 1.0 for hour in hours)
    assert 0.01 < sum(widths) / len(widths) < 0.5


async def test_the_power_curve_is_built_from_the_upload(client: AsyncClient, user: User, weather):
    weather()

    response = await post(client, user, [csv_part(dataset_csv())])

    curve = response.json()["power_curve"]
    assert len(curve) == upload.BIN_COUNT
    assert [point["wind_ms"] for point in curve][:3] == [0.25, 0.75, 1.25]
    # Неубывающая до номинала.
    assert all(left["power_norm"] <= right["power_norm"] for left, right in zip(curve, curve[1:], strict=False))
    # Ниже включения ноль, в рабочем диапазоне интервал взят из наблюдений.
    assert all(point["power_norm"] == 0.0 for point in curve if point["wind_ms"] < CUT_IN_MS)
    working = [point for point in curve if CUT_IN_MS <= point["wind_ms"] <= RATED_MS and point["samples"] >= upload.MIN_BIN_SAMPLES]
    assert working
    assert all(point["p10"] <= point["power_norm"] <= point["p90"] for point in working)
    assert any(point["p90"] - point["p10"] > 0.02 for point in working)


async def test_sparse_bins_are_interpolated_and_the_answer_says_so(client: AsyncClient, user: User, weather):
    weather()

    response = await post(client, user, [csv_part(dataset_csv())])

    body = response.json()
    codes = {warning["code"] for warning in body["warnings"]}
    # Ветер в выборке до 20 м/с, корзины выше остаются пустыми и достраиваются.
    assert upload.WARN_CURVE_INTERPOLATED in codes
    assert any(point["samples"] == 0 for point in body["power_curve"])


# --- разбор и отбраковка -------------------------------------------------


async def test_ten_minute_rows_are_folded_into_hours(client: AsyncClient, user: User, weather):
    weather()

    response = await post(client, user, [csv_part(dataset_csv(hours=800))])

    dataset = response.json()["dataset"]
    assert dataset["step_minutes"] == 10
    assert dataset["hours"] == 800
    assert dataset["period_start"].startswith("2025-01-01T00:00")
    assert dataset["period_end"].startswith("2025-02-03T07:00")


async def test_an_hour_with_fewer_than_four_points_is_dropped(client: AsyncClient, user: User, weather):
    weather()
    # Три точки в часе, следующем сразу за выборкой: в час он не складывается.
    lonely = [f"999{index},2025-02-03 8:{index * 10:02d}:00,7.0,0.3,5.0" for index in range(3)]

    response = await post(client, user, [csv_part(dataset_csv(hours=800, extra_rows=lonely))])

    dataset = response.json()["dataset"]
    assert dataset["hours"] == 800
    assert dataset["drop_reasons"][upload.DROP_SPARSE_HOUR] == 3


async def test_rubbish_values_land_in_drop_reasons_instead_of_breaking_the_request(client: AsyncClient, user: User, weather):
    weather()
    rubbish = [
        "900001,не дата,7.0,0.3,5.0",
        "900002,2025-02-03 9:00:00,,0.3,5.0",
        "900003,2025-02-03 9:10:00,7.0,,5.0",
        "900004,2025-02-03 9:20:00,-3.0,0.3,5.0",
        "900005,2025-02-03 9:30:00,7.0,5.0,5.0",
        "900006,2025-02-03 9:40:00,7.0,-0.2,5.0",
        "900007,2025-01-01 0:00:00,7.0,0.3,5.0",
    ]

    response = await post(client, user, [csv_part(dataset_csv(extra_rows=rubbish))])

    assert response.status_code == 200, response.text
    reasons = response.json()["dataset"]["drop_reasons"]
    assert reasons[upload.DROP_BAD_TIMESTAMP] == 1
    assert reasons[upload.DROP_MISSING_VALUE] == 2
    assert reasons[upload.DROP_NEGATIVE_WIND] == 1
    assert reasons[upload.DROP_POWER_OUT_OF_RANGE] == 2
    assert reasons[upload.DROP_DUPLICATE_TIME] == 1
    assert response.json()["dataset"]["dropped_rows"] == sum(reasons.values())
    assert upload.WARN_ROWS_DROPPED in {warning["code"] for warning in response.json()["warnings"]}


async def test_extra_columns_are_ignored(client: AsyncClient, user: User, weather):
    weather()

    response = await post(client, user, [csv_part(dataset_csv(extra_column="не наше дело"))])

    assert response.status_code == 200, response.text


# --- отказы --------------------------------------------------------------


async def test_a_request_without_files_is_refused(client: AsyncClient, user: User, weather):
    weather()

    response = await client.post(UPLOAD, headers=auth(user), data={"issue_date": ISSUE})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == upload.CODE_NO_FILES


async def test_a_file_without_the_needed_columns_is_refused(client: AsyncClient, user: User, weather):
    weather()
    content = b"ID,timestamp,speed,power\n1,2025-01-01 00:00:00,7.0,0.3\n"

    response = await post(client, user, [csv_part(content)])

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == upload.CODE_BAD_FORMAT
    assert "Средняя скорость ветра" in body["message"]


async def test_a_file_that_is_not_a_csv_is_refused(client: AsyncClient, user: User, weather):
    weather()

    response = await post(client, user, [csv_part(b"\x00\x01\x02 not a csv at all")])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == upload.CODE_BAD_FORMAT


async def test_a_short_file_cannot_carry_a_power_curve(client: AsyncClient, user: User, weather):
    weather()

    response = await post(client, user, [csv_part(dataset_csv(hours=14 * 24))])

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == upload.CODE_NOT_ENOUGH_DATA
    assert body["details"] == {"hours": 336, "required": upload.MIN_HOURS}


async def test_a_file_where_every_row_is_rubbish_is_refused(client: AsyncClient, user: User, weather):
    weather()
    rows = [f"{index},2025-01-01 {index:02d}:00:00,7.0,9.0,5.0" for index in range(24)]

    response = await post(client, user, [csv_part(("\n".join([HEADER, *rows]) + "\n").encode())])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == upload.CODE_BAD_VALUES


async def test_a_dataset_with_a_single_wind_speed_cannot_give_a_curve(client: AsyncClient, user: User, weather):
    weather()
    moments = [datetime(2025, 1, 1) + timedelta(minutes=10 * index) for index in range(800 * 6)]
    rows = [f"{index},{stamp(moment)},7.0,0.3,5.0" for index, moment in enumerate(moments)]

    response = await post(client, user, [csv_part(("\n".join([HEADER, *rows]) + "\n").encode())])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == upload.CODE_BAD_VALUES


async def test_three_files_are_more_than_a_set(client: AsyncClient, user: User, weather):
    weather()
    parts = [csv_part(dataset_csv(hours=10), f"turbine-{index}.csv") for index in range(3)]

    response = await post(client, user, parts)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == upload.CODE_BAD_FORMAT


async def test_a_day_outside_the_replay_is_refused(client: AsyncClient, user: User, weather):
    weather()

    response = await client.post(
        UPLOAD,
        headers=auth(user),
        files=[csv_part(dataset_csv(hours=10))],
        data={"issue_date": "2026-03-15"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ISSUE_NOT_FOUND"


async def test_a_file_over_the_limit_is_refused(client: AsyncClient, user: User, weather, monkeypatch):
    weather()
    monkeypatch.setattr(upload, "MAX_FILE_BYTES", 1024)

    response = await post(client, user, [csv_part(dataset_csv(hours=10))])

    assert response.status_code == 413
    assert response.json()["error"]["code"] == upload.CODE_TOO_LARGE


async def test_weather_that_never_answers_is_an_honest_error(client: AsyncClient, user: User, weather):
    weather(FakeWeather(by_source={}))

    response = await post(client, user, [csv_part(dataset_csv())])

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WEATHER_NO_RUN"


# --- предметные функции без HTTP ----------------------------------------


def test_the_curve_is_zero_outside_the_working_range():
    curve = [
        upload.PowerCurveBin(wind_ms=round((number + 0.5) * upload.BIN_WIDTH_MS, 2), power_norm=0.5, p10=0.4, p90=0.6, samples=50)
        for number in range(upload.BIN_COUNT)
    ]

    assert upload.curve_value(curve, CUT_IN_MS - 0.1) == (0.0, 0.0, 0.0)
    assert upload.curve_value(curve, CUT_OUT_MS) == (0.0, 0.0, 0.0)
    assert upload.curve_value(curve, CUT_OUT_MS + 5) == (0.0, 0.0, 0.0)
    assert upload.curve_value(curve, 8.0) == (0.4, 0.5, 0.6)


def test_turbine_names_fall_back_to_the_site():
    assert upload._turbine_names(None, 2) == ["T1", "T2"]
    assert upload._turbine_names(["", "  "], 2) == ["T1", "T2"]
    assert upload._turbine_names(["WTG-01"], 2) == ["WTG-01", "T2"]
