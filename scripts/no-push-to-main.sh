#!/usr/bin/env bash
# Git-хук pre-push: запрещает пушить напрямую в main.
# Ставится командой `make hooks`, которая копирует этот файл в .git/hooks/pre-push.
#
# Git передает хуку строки вида "<local ref> <local sha> <remote ref> <remote sha>".
# Обходится только осознанно: PROTECTED_BRANCH_OVERRIDE=1 git push ...
set -euo pipefail

PROTECTED="refs/heads/main"

if [ "${PROTECTED_BRANCH_OVERRIDE:-0}" = "1" ]; then
    echo "pre-push: запрет на main снят через PROTECTED_BRANCH_OVERRIDE" >&2
    exit 0
fi

blocked=0
while read -r _local_ref _local_sha remote_ref _remote_sha; do
    [ -z "${remote_ref:-}" ] && continue
    if [ "$remote_ref" = "$PROTECTED" ]; then
        blocked=1
    fi
done

if [ "$blocked" = "1" ]; then
    cat >&2 <<'MSG'

  Прямой push в main запрещен.

  Работаем так: ветка features-dev{1,2,3}-<name>, затем PR в main и мердж.
  Подробнее: docs/git-workflow.md

      git switch -c features-dev1-<name>
      git push -u origin features-dev1-<name>
      gh pr create --base main --fill

MSG
    exit 1
fi

exit 0
