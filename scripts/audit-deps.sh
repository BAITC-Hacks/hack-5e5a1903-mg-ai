#!/usr/bin/env bash
# Проверка зависимостей на известные уязвимости. Запускается перед каждым мерджем.
# Подробности и что делать с находками: docs/dependency-audit.md
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
status=0

for project in backend ml; do
    echo "== Python ($project): pip-audit по зафиксированным версиям из uv.lock =="
    if uv export --project "$ROOT/$project" --no-dev --no-emit-project \
            --format requirements-txt >"$ROOT/.audit-requirements.txt" 2>/dev/null; then
        uvx pip-audit --requirement "$ROOT/.audit-requirements.txt" --strict || status=1
        rm -f "$ROOT/.audit-requirements.txt"
    else
        echo "Не удалось выгрузить зависимости $project" >&2
        status=1
    fi
    echo
done

echo
echo "== npm: npm audit =="
if [ -f "$ROOT/frontend/package.json" ]; then
    (cd "$ROOT/frontend" && npm audit --omit=dev) || status=1
else
    echo "frontend/package.json пока нет, проверять нечего"
fi

echo
if [ "$status" = "0" ]; then
    echo "Известных уязвимостей не найдено."
else
    echo "Есть находки. Не мерджим, пока не разобрались: docs/dependency-audit.md" >&2
fi
exit "$status"
