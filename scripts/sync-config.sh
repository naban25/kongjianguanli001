#!/usr/bin/env bash
# scripts/sync-config.sh
# ----------------------------------------------------------------
# 比对 .env.example 与本地 .env / Dashboard 配置，输出差异清单。
# 不会自动修改任何 dashboard 配置——所有 dashboard 操作必须人工执行。
#
# 用法：
#   scripts/sync-config.sh                 # 检查全部仓
#   scripts/sync-config.sh apps/api        # 只检查 api
#   scripts/sync-config.sh --diff          # 详细差异模式
# ----------------------------------------------------------------

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
err()  { echo -e "${RED}✗ $1${NC}"; }

ROOT_DIR=$(git rev-parse --show-toplevel)
TARGETS=("apps/api" "apps/web")

# 解析命令行参数
DIFF_MODE=0
if [[ "${1:-}" == "--diff" ]]; then
  DIFF_MODE=1
  shift
fi
if [[ -n "${1:-}" ]]; then
  TARGETS=("$1")
fi

check_one() {
  local dir=$1
  local env_example="$dir/.env.example"
  local env_local="$dir/.env"

  echo
  echo "==========================================="
  echo "📦 $dir"
  echo "==========================================="

  if [[ ! -f "$env_example" ]]; then
    warn "$env_example 不存在，跳过"
    return 0
  fi

  # 从 .env.example 读取所有变量名（忽略空行和注释）
  local expected_keys
  expected_keys=$(grep -E "^[A-Z_][A-Z0-9_]*=" "$env_example" | cut -d= -f1 | sort -u)

  echo " 期望配置 ($(echo "$expected_keys" | wc -l | xargs) 个变量):"
  echo "$expected_keys" | sed 's/^/    /'

  if [[ ! -f "$env_local" ]]; then
    warn "$env_local 不存在（本地未配置）"
    echo
    echo "💡 提示："
    echo "    cp $env_example .env  # 然后填入真实值"
  else
    echo
    echo " 本地 .env 中已配置:"
    local found_keys
    found_keys=$(grep -E "^[A-Z_][A-Z0-9_]*=" "$env_local" | cut -d= -f1 | sort -u)
    echo "$found_keys" | sed 's/^/    /'

    if [[ "$DIFF_MODE" == "1" ]]; then
      echo
      echo " 详细差异："
      local missing extra
      missing=$(comm -23 <(echo "$expected_keys") <(echo "$found_keys"))
      extra=$(comm -13 <(echo "$expected_keys") <(echo "$found_keys"))
      if [[ -n "$missing" ]]; then
        err "本地缺失以下变量："
        echo "$missing" | sed 's/^/    /'
      fi
      if [[ -n "$extra" ]]; then
        warn "本地多余变量（可能未在 .env.example 声明）："
        echo "$extra" | sed 's/^/    /'
      fi
      if [[ -z "$missing" && -z "$extra" ]]; then
        ok "本地 .env 与 .env.example 变量集一致"
      fi
    fi
  fi

  # Dashboard 配置检查（Cloudflare Pages / Railway）
  echo
  echo " Dashboard 必要配置："
  case "$dir" in
    apps/web)
      cat <<EOF
   Cloudflare Pages Dashboard → Settings → Environment variables:
     - NEXT_PUBLIC_API_BASE_URL  ← 前端调用的后端根地址（不入 git）
EOF
      ;;
    apps/api)
      cat <<EOF
   Railway Dashboard → Variables:
     - DATABASE_URL             ← Neon PostgreSQL
     - LLM_PROVIDER             ← 至少设占位值（如 "nvidia"），否则 LLM 模块静默降级
     - JWT_SECRET_KEY           ← JWT 签名密钥
     - LLM_API_KEY (如需 LLM)
     - LLM_BASE_URL (如自定义)
     - RAILWAY_API_TOKEN        ← 部署自动化（注意是 _API_）
     - SCHEDULER_LOCK_PATH      ← 默认 /tmp/office_lease_scheduler.lock
EOF
      ;;
  esac
}

echo "🔍 配置同步检查工具"
echo "   检查 .env.example 与本地 .env / Dashboard 配置的对齐情况"

for target in "${TARGETS[@]}"; do
  cd "$ROOT_DIR"
  check_one "$target"
done

echo
echo "==========================================="
echo "📋 总结"
echo "==========================================="
echo " - 本工具只做只读检查，不会修改任何 dashboard"
echo " - Dashboard 设置需人工操作，详见 DEPLOYMENT.md"
echo " - 添加新环境变量时务必同步更新对应 .env.example"