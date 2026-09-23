# Реестр живых worktree

Кто какой слот портов занял прямо сейчас. Файл коммитится, чтобы все трое видели
занятые порты.

Правила: создал worktree — сразу добавил строку. **Смерджил PR — сразу удалил строку
и освободил порты** (`docker compose down -v`, `git worktree remove`). Слоты и порядок
работы: [../docs/worktrees.md](../zhel-ai/docs/worktrees.md).

Колонки портов соответствуют переменным `FRONTEND_PORT`, `BACKEND_PORT` и `DB_PORT`
из `.env` того worktree.

| Ветка | Папка worktree | dev | Машина | Слот | FRONTEND_PORT | BACKEND_PORT | DB_PORT |
|-------|----------------|-----|--------|------|---------------|--------------|---------|
| main  | HACKALEM AI    | dev1 | ноут Meirzhan | сдвинут | 3100 | 8100 | 5533 |
| features-dev1-gateway | hackalem-worktrees/features-dev1-gateway | dev1 | ноут Meirzhan | 1 | 3001 | 8001 | 5433 |
| features-dev1-frontend | hackalem-worktrees/features-dev1-frontend | dev1 | ноут Meirzhan | 2 | 3002 | 8002 | 5434 |
| features-dev1-auth | hackalem-worktrees/features-dev1-auth | dev1 | ноут Meirzhan | 3 | 3003 | 8003 | 5435 |
| features-dev1-compose | hackalem-worktrees/features-dev1-compose | dev1 | ноут Meirzhan | 4 | 3004 | 8004 | 5436 |

Основной чекаут на машине dev1 использует сдвинутые порты, потому что 3000 и 8000
там заняты другими проектами.

Пример строки, которую добавляешь под себя (удалить после мерджа):

```
| features-dev1-auth | hackalem-worktrees/features-dev1-auth | dev1 | ноут Meirzhan | 1 | 3001 | 8001 | 5433 |
```
| features-dev2-ensemble-calibration | hackalem-worktrees/features-dev2-ensemble-calibration | dev2 | ноут Aibek | — | — | — | — |
| features-dev2-dispatch-kpi | hackalem-worktrees/features-dev2-dispatch-kpi | dev2 | ноут Aibek | — | — | — | — |
