# HACKALEM AI

В репозитории два независимых решения: ZHEL.ai в `zhel-ai/` (прогноз выработки ВЭС, всё ниже
про него) и OrgDiff в `check-theory-kazakhtelecom/` (свой README, свои команды). Общий
указатель для жюри: `README.md` в корне.

- **Что мы должны сдать, чекбоксы по ТЗ и критерии оценки: @zhel-ai/GOAL.md**
- Команды, конвенции кода и архитектурные правила: @zhel-ai/AGENTS.md
- Кто я в команде (dev1 / dev2 / dev3): @.dev-notes/me.md
- Правила хакатона (выжимка из PDF): @rules/README.md
- Вся документация и архитектура: @zhel-ai/docs/README.md
- Архитектурные best practices, по которым пишем код: @zhel-ai/docs/architecture-guidelines.md
- **Новый инструмент или библиотеку не добавляем молча.** Если задача упирается в ограничение, которое решает известный инструмент, его надо предложить и обосновать, но не внедрять без явного «делаем» от команды. Реестр предложений: @zhel-ai/docs/proposals.md
- **Каждый PR привязан к задаче:** `Closes #N`, `Fixes #N` или `Refs #N` в описании, задачи нет — сначала заводим ее: @zhel-ai/docs/git-workflow.md
- **Все PR идут в `dev`, а не в `main`.** Ветка `features-dev{1,2,3}-<name>` от `origin/dev`, PR в `dev`, мердж без ревью остальных. В `main` попадает только PR из `dev` в `main`. Напрямую не пушим ни в `main`, ни в `dev`: @zhel-ai/docs/git-workflow.md
- **Перед каждым мерджем проверяем зависимости на уязвимости:** `make audit`. Для Python это `pip-audit` по `uv.lock`, для frontend `npm audit`. Есть находки — не мерджим: @zhel-ai/docs/dependency-audit.md
- Worktree на каждую фичу, свой слот портов, снос сразу после мерджа: @zhel-ai/docs/worktrees.md
- Реестр занятых worktree и портов (строка добавляется при создании, удаляется после мерджа): @.dev-notes/worktrees.md
- Заметки команды и правила работы с ними: @.dev-notes/README.md
- Чеклист перед сдачей проекта: @zhel-ai/docs/pre-submit-checklist.md
