#!/usr/bin/env bash
# scripts/push-all.sh
# ----------------------------------------------------------------
# 在主仓 kongjianguanli001 根目录运行此脚本，自动按顺序推送三个仓
# 并在主仓 bump 子模块指针。
#
# 用法：
#   scripts/push-all.sh "feat(tables): xxx"
#   scripts/push-all.sh                    # 复用根目录未提交变更
#
# 行为：
#   1. 检查每个仓是否干净（无未提交变更）
#   2. 推 apps/api 子仓（如有变更），等 CI 全绿
#   3. 推 apps/web 子仓（如有变更），等 CI 全绿
#   4. 主仓 bump 子模块 pointer 并 push
# ----------------------------------------------------------------

set -euo pipefail

MSG="${1:-}"

if [[ -z "$MSG" ]]; then
  # 复用最近一次 commit 信息（仅主仓）
  MSG=$(git log -1 --pretty=%s 2>/dev/null || echo "chore: bump submodules")
fi

echo "🚀 push-all: 提交信息 = $MSG"
echo

ROOT_DIR=$(git rev-parse --show-toplevel)
cd "$ROOT_DIR"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

step() { echo -e "${YELLOW}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

check_clean() {
  local repo_dir=$1
  local repo_name=$2
  (
    cd "$repo_dir"
    if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
      fail "$repo_name 有未提交的变更，请先手动 commit 或 stash"
    fi
  )
}

commit_if_dirty() {
  local repo_dir=$1
  local repo_name=$2
  local msg=$3
  (
    cd "$repo_dir"
    if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
      step "$repo_name: 自动暂存并提交"
      git add -A
      git commit -m "$msg"
    else
      ok "$repo_name: 无变更，跳过"
    fi
  )
}

push_repo() {
  local repo_dir=$1
  local repo_name=$2
  (
    cd "$repo_dir"
    step "$repo_name: git push origin main"
    git push origin main
  )
}

wait_for_ci() {
  local repo=$1
  local branch=${2:-main}
  step "$repo: 等待 GitHub Actions CI 全绿（最多 5 分钟）..."

  local max_wait=300
  local elapsed=0
  local interval=15

  while (( elapsed < max_wait )); do
    local status
    status=$(gh api "repos/$repo/commits/$(git rev-parse HEAD)/check-runs" \
      --jq '[.check_runs[] | select(.conclusion != null) | .conclusion] | if length > 0 then (if any(. == "failure" or . == "cancelled") then "failure" elif all(. == "success") then "success" else "pending" end) else "pending" end' \
      2>/dev/null || echo "pending")

    case "$status" in
      success) ok "$repo: CI 全绿"; return 0 ;;
      failure) fail "$repo: CI 失败，请到 GitHub Actions 查看详情" ;;
      *)       echo "  等待中... ($elapsed/$max_wait s)" ;;
    esac

    sleep $interval
    elapsed=$((elapsed + interval))
  done

  fail "$repo: CI 超时未结束"
}

# ---------- 0. 检查 gh CLI ----------
command -v gh >/dev/null 2>&1 || fail "需要安装 gh CLI（用于等 CI）：brew install gh"

# ---------- 1. 推送 apps/api ----------
step "[1/3] 处理 apps/api 子仓"
check_clean "apps/api" "apps/api"
commit_if_dirty "apps/api" "apps/api" "$MSG"

api_sha=$(cd apps/api && git rev-parse HEAD)
api_remote_sha=$(cd apps/api && git rev-parse origin/main 2>/dev/null || echo "")
if [[ "$api_sha" != "$api_remote_sha" ]]; then
  push_repo "apps/api" "apps/api"
  wait_for_ci "naban25/kongjianguanli-api"
else
  ok "apps/api: 远端已是最新，跳过 push 与 CI 等待"
fi

# ---------- 2. 推送 apps/web ----------
step "[2/3] 处理 apps/web 子仓"
check_clean "apps/web" "apps/web"
commit_if_dirty "apps/web" "apps/web" "$MSG"

web_sha=$(cd apps/web && git rev-parse HEAD)
web_remote_sha=$(cd apps/web && git rev-parse origin/main 2>/dev/null || echo "")
if [[ "$web_sha" != "$web_remote_sha" ]]; then
  push_repo "apps/web" "apps/web"
  wait_for_ci "naban25/kongjianguanli-web"
else
  ok "apps/web: 远端已是最新，跳过 push 与 CI 等待"
fi

# ---------- 3. bump 主仓 pointer ----------
step "[3/3] 主仓 bump 子模块指针"
check_clean "." "主仓"

if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  git add apps/api apps/web
  git commit -m "chore(submodule): bump 子模块指针

$MSG"
  git push origin main
  wait_for_ci "naban25/kongjianguanli001"
  ok "主仓 pointer 已 bump 并推送"
else
  ok "主仓: 子模块指针已是最新，跳过"
fi

echo
ok "✅ 全部推送完成"
echo "  → apps/api:    $(cd apps/api && git rev-parse --short HEAD)"
echo "  → apps/web:    $(cd apps/web && git rev-parse --short HEAD)"
echo "  → 主仓:        $(git rev-parse --short HEAD)"