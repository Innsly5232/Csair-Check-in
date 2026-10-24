#!/usr/bin/env bash
# 保活：每次运行都通过 API 重新启用定时工作流；距最近一次提交满 FALLBACK_DAYS 天时再补一个空提交兜底。
set -euo pipefail

: "${GH_TOKEN:?未提供 GH_TOKEN}"
: "${REPO:?未指定仓库}"
: "${TARGET_BRANCH:?未指定默认分支}"
FALLBACK_DAYS="${FALLBACK_DAYS:-45}"
WORKFLOWS=(csair-sign.yml keepalive.yml)

api_failed=0
for workflow in "${WORKFLOWS[@]}"; do
  # 对已启用的工作流重复启用是幂等操作，成功返回 HTTP 204。
  if gh api --method PUT -H 'Accept: application/vnd.github+json' \
      "repos/${REPO}/actions/workflows/${workflow}/enable" --silent; then
    echo "已通过 API 重新启用 ${workflow}。"
  else
    echo "::warning::重新启用 ${workflow} 失败，请检查工作流的 actions: write 权限。"
    api_failed=1
  fi
done

last_commit=$(git log -1 --format=%ct)
age_days=$(( ($(date +%s) - last_commit) / 86400 ))
if (( age_days < FALLBACK_DAYS )); then
  echo "距最近一次提交 ${age_days} 天，未满 ${FALLBACK_DAYS} 天，无需空提交兜底。"
else
  git config user.name 'github-actions[bot]'
  git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
  git commit --allow-empty -m 'chore: keep scheduled workflows active'
  # 普通推送：远端若有并发提交则失败，不强推。
  git push origin "HEAD:${TARGET_BRANCH}"
  echo "距最近一次提交 ${age_days} 天，已创建兜底空提交。"
fi

# API 失败时让任务标红，GitHub 会发邮件提醒；兜底提交已在上面尝试过。
exit "$api_failed"
