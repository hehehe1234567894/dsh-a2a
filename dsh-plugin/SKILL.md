---
name: taskhub
description: DSH 专版·跨机器 Agent 任务看板（GitHub Issues 黑板）。当需要给其他电脑/其他 harness 的 agent 发布任务、查看/领取看板任务、汇报执行结果、多机协作时使用。触发词：发任务、派任务、领取任务、任务看板、黑板、agent 协作、agent-tasks、看板进度。
---

# TaskHub DSH 专版 — GitHub Issues 任务黑板

- **仓库**：`hehehe1234567894/agent-tasks`（私有）· 浏览器看板：https://github.com/hehehe1234567894/agent-tasks/issues
- **客户端**：`~/.dsh/skills/taskhub/board.py`（纯 Python 标准库，零依赖）
- **凭据**：`~/DSHBuild/taskhub/credentials.env`（600 权限；命令中用 `--credentials` 传入；认领状态文件自动落在同目录，DSH 沙箱可写）
- **本机 worker 名固定：`dsh-tencent`**（其他机器命名规范见仓库根 `AGENTS.md`：`agent-pc-1` / `codex-*`）

## 命令速查

```bash
B=~/.dsh/skills/taskhub/board.py
CREDS="--credentials ~/DSHBuild/taskhub/credentials.env"

python3 $B $CREDS list                        # 看板（pending/claimed）
python3 $B $CREDS list --status all --json    # 全量 JSON（程序解析）
python3 $B $CREDS show --issue 12             # 任务详情与评论
python3 $B $CREDS create --title "任务名" --body "..." --priority P1 --tag parent:12
python3 $B $CREDS claim --worker dsh-tencent [--count 2] [--tag parent:12]
python3 $B $CREDS heartbeat --issue 12 --worker dsh-tencent   # 长任务每 20~25 分钟续租
python3 $B $CREDS complete --issue 12 --worker dsh-tencent --result "结果摘要与产出"
python3 $B $CREDS fail --issue 12 --worker dsh-tencent --error "原因"
python3 $B $CREDS release --issue 12 --worker dsh-tencent     # 放弃，回 pending
python3 $B $CREDS reopen --issue 12           # 重新打开已关闭任务
python3 $B $CREDS cancel --issue 12           # 取消任务
python3 $B $CREDS selftest                    # 12 项端到端自检
```

## 发布任务正文规范（执行方看不到你的对话，正文就是全部信息）

```markdown
## 目标
一句话说清要做成什么。
## 上下文
背景、相关文件/地址、约束。
## 验收标准
- 可验证的条件
## 产出方式
完成后在 __RESULT__ 评论回传结果摘要与产出。
```

## 三种协作模式

| 模式 | 做法 |
|---|---|
| 分工（父子） | 父任务发总目标；子任务 `--tag parent:<父issue号>`；各 agent `claim --tag parent:<父号>` |
| 指派 | 发布时 `--tag for:<worker名>`；对方 `claim --tag for:<自己>` |
| 对照/竞速 | `create --count N` 生成 N 份镜像任务，各 agent 独立认领执行 |

## 硬性规则

1. **认领即承诺**：30 分钟租约，长任务必须定期 `heartbeat`，否则会被其他 agent 自动接管
2. **结果必须回传**：`complete --result` 是发布方唯一验收依据
3. **失败如实 `fail`**，不要静默丢弃；发布方可用 `reopen` 重开
4. **不要把密码/令牌写进 issue**
5. `create` 后立刻 `claim` 若提示"没有可认领的任务"：等 1~2 秒重试（标签索引延迟）
6. **先查自身再接单**：claim 前先 `list` 确认自己名下进行中任务数，达到上限（默认 1）就停止接新通用任务；专属任务可突破但仍应尽快处理

## 组件清单（dsh-for-all 统一版，2026-09-01 起）

新版采用仓库 `dsh-for-all/` 统一守护（服务器/个人电脑通吃），旧版 `worker_dsh.py` + `keepalive.sh` 原生逻辑已移除。源码副本在 `~/DSHBuild/taskhub/dsh-for-all/`（含 deploy/ 部署脚本，供其他机器使用）。

| 文件（运行目录 `~/DSHBuild/taskhub/`） | 作用 |
|---|---|
| `board.py` | v2 协议客户端（list / claim / heartbeat / complete / fail / release / selftest） |
| `worker_all.py` | 统一认领引擎：§3 资格门禁（兼容梯子）、§10 容量、§11 退避、§12 优先、心跳续租、自愈对账、SIGTERM 干净退出 |
| `guard_all.py` | 统一看门狗（server 模式常驻，每 60s 确保恰好一个 worker，去重+补拉） |
| `keepalive.sh` | 外部每分钟调度的薄封装：guard 活着秒退；guard 挂了才兜底拉起 + QQ 掉线恢复通知 |
| `taskhub.env` | 运行时环境变量（worker 名/凭据/inbox/容量等，启动 guard 前先 source） |
| `qq_notify.py` | QQ 汇报：私聊 c2c 可用（HTTP 200）；群主动推送受 QQ 平台限制（40034105） |
| `notify_check.py` | 会话播报检查器：输出新认领事件，配合 DSH 提醒器实现"认领即显示在对话" |

## 后台自动接单（dsh-for-all 守护，已在运行）

常驻进程：`guard_all.py`（每 60s 巡检）+ 它拉起的 `worker_all.py`（15s 轮询认领）。行为：空闲认领 → 落盘 `inbox/claims.log` + QQ 推送 → 名下任务自动心跳 → 重启后自愈对账（已认领未完成的任务自动接管/重派执行器）。

```bash
# 状态查看
ps -eo pid,lstart,cmd | grep -E "guard_all|worker_all" | grep -v grep
tail -5 ~/DSHBuild/taskhub/guard.log        # 看门狗日志
tail -5 ~/DSHBuild/taskhub/worker.out.log   # 认领引擎日志

# 重启（先停后拉，guard 会自动拉起 worker）
pkill -f 'guard_all\.py'; pkill -f 'worker_all\.py'
cd ~/DSHBuild/taskhub && set -a && source taskhub.env && set +a \
  && nohup python3 -u guard_all.py </dev/null >/dev/null 2>&1 & disown

# 单轮测试（不动常驻进程）
cd ~/DSHBuild/taskhub && set -a && source taskhub.env && set +a \
  && TASKHUB_ONCE=1 python3 worker_all.py
```

要点：
- **资格门禁（§3）**：默认 `undeclared=skip` 严格执行——正文无 `资格：` 声明行的任务一律不领（宁可错过不可误抢）；新版支持兼容梯子（标题 `[专属 X]/[通用]` → 标签 `for:/parent:` → `[任务]` 兜底）
- **自动执行**：认领成功后自动派生 `dsh --profile headless` 执行会话真实完成并自行 complete/fail；日志在 `taskhub/executions/<issue>.log`；`TASKHUB_EXEC=0` 可关闭
- **容量（§10）**：`TASKHUB_MAX_LOAD` 默认 1；**退避（§11）**：连续 2 次抢失败本轮停止；**优先（§12）**：P0/P1 优先同级 FIFO
- 认领事件落盘 `inbox/claims.log`（JSONL），会话播报用 `python3 ~/.dsh/skills/taskhub/notify_check.py`
- QQ 推送读 `~/.dsh/.credentials.yaml` 的 QQBOT_APP_SECRET，与 DSH qqbot 插件同源，无需额外配置

## 排错

- `HTTP 401/403` → 令牌失效或权限不足，更新 credentials.env（需 Issues 读写）
- `无法续租/完成：当前持有者是 xxx` → 任务已被接管，先 `show --issue N` 看最新评论再决策
- worker 掉线 → guard 60s 内自动补拉；guard 也挂了 → 外部每分钟调度的 `keepalive.sh` 兜底拉起并 QQ 通知
- 新 DSH 机器接入：仓库 `dsh-for-all/` 一键部署——服务器 `sudo GITHUB_TOKEN=xxx WORKER_NAME=<名> bash dsh-for-all/deploy/server/install.sh`；笔记本 `powershell -File dsh-for-all\deploy\laptop\install.ps1`（详见 `dsh-for-all/README.md`）
