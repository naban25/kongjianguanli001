# scripts/

主仓部署链路辅助脚本。

## push-all.sh

一键按顺序推送三个仓并自动 bump 主仓指针。

```bash
# 在主仓根目录
./scripts/push-all.sh "feat(tables): 新增按列类型筛选"
```

依赖：
- `git`
- `gh` CLI（用于等待 GitHub Actions CI）：`brew install gh`
- `gh auth login` 已登录且有目标仓的写权限

行为：详见脚本顶部注释 + `DEPLOYMENT.md`。

跳过 CI 等待（紧急情况）：
```bash
SKIP_CI_WAIT=1 ./scripts/push-all.sh "hotfix: ..."
```

---

## sync-config.sh

比 `.env.example` 与本地 `.env`，输出 Dashboard 应设置但本地未配置的变量清单。

```bash
./scripts/sync-config.sh                # 检查所有子仓
./scripts/sync-config.sh apps/api       # 只检查 api
./scripts/sync-config.sh --diff         # 详细差异模式
```

仅做只读检查，不会自动修改任何 dashboard 配置。
dashboard 设置必须人工执行，详见 `DEPLOYMENT.md`。