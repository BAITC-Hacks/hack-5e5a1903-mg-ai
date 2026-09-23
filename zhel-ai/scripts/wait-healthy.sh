#!/usr/bin/env bash
# Ждет, пока сервис compose станет healthy. Нужен, чтобы миграции не стартовали
# раньше, чем поднимется база: иначе первый запуск у судьи падает на ровном месте.
set -euo pipefail

service="${1:?нужно имя сервиса}"
attempts="${2:-60}"

for attempt in $(seq 1 "$attempts"); do
    status="$(docker compose ps --format '{{.Health}}' "$service" 2>/dev/null | head -1)"
    case "$status" in
        healthy) echo "$service готов"; exit 0 ;;
        "")      : ;;  # контейнер еще не создан
    esac
    printf '.'
    sleep 2
done

echo >&2
echo "$service не стал healthy за $((attempts * 2)) секунд. Логи: docker compose logs $service" >&2
exit 1
