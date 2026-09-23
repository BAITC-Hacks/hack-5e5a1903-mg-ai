#!/usr/bin/env bash
# Включает защиту ветки main на стороне GitHub: без pull request влить нельзя.
# Запускается один раз, когда появился командный репозиторий: `make protect-main`.
#
# Локальные хуки можно обойти, серверное правило обойти нельзя, поэтому
# это основной механизм запрета, а хук pre-push только подстраховка.
set -euo pipefail

if ! command -v gh >/dev/null 2>&1; then
    echo "Нужен GitHub CLI: https://cli.github.com" >&2
    exit 1
fi

REPO="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
echo "Включаю защиту main в $REPO"

# required_pull_request_reviews с нулем одобрений: PR обязателен, но ревью
# остальных разработчиков не требуется, как договорились в docs/git-workflow.md
gh api "repos/$REPO/branches/main/protection" \
    --method PUT \
    --header "Accept: application/vnd.github+json" \
    --input - <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["Lint and tests", "Docker build"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

echo
echo "Готово. Теперь в main нельзя влить без pull request,"
echo "force-push и удаление ветки запрещены, а CI обязан быть зеленым."
