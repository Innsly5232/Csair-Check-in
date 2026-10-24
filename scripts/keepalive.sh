#!/usr/bin/env bash
set -euo pipefail

last_commit=$(git log -1 --format=%ct)
now=$(date +%s)
if (( now - last_commit < 30 * 86400 )); then
  echo '最近 30 天已有提交，本次无需保活。'
  exit 0
fi

git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git commit --allow-empty -m 'chore: keep scheduled workflows active'
# 普通推送：远端若有并发提交则失败，不强推。
git push origin "HEAD:${TARGET_BRANCH:?未指定默认分支}"
echo '已创建保活提交。'
