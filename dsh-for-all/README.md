# dsh-for-all —— 通吃服务器与个人电脑的 TaskHub 后台任务守护

一套代码、两种形态：**同一份认领引擎 + 同一个看门狗**，用一个环境变量在
「云服务器常驻」与「个人电脑条件触发」之间切换。全部零第三方依赖（纯 Python 标准库），
完整遵守根目录 `AGENTS.md` 契约（§3 资格门禁、§10 MAX_LOAD 容量、§11 退避防刷、§12 优先级）。

> 设计依据见同目录 `DESIGN.md`：`dsh/`（云主机常开、求稳定）与 `dsh-laptop/`
> （个人电脑、开机不自启、锚定 DSH 进程运行才抢、求省电不打扰）两套专版的实践总结。

## 目录结构

```
dsh-for-all/
├── worker_all.py            # 统一认领守护核心（§3 门禁/§10 容量/§11 退避/§12 优先/心跳/执行器自愈）
├── guard_all.py             # 统一看门狗（server 常驻循环或 --once 单次；laptop 锚进程门控单次）
├── README.md                # 本文件：部署说明
├── DESIGN.md                # 两套 worker 的设计经验与差异总结（设计依据）
├── qq_notify.py             # QQ 通知（可选；两套专版同款）
├── notify_check.py          # 认领事件 → 会话播报检查器（读 inbox/claims.log）
└── deploy/
    ├── server/              # 服务器场景：systemd 主路线（开机即守护、崩溃自拉）+ cron 兜底
    │   ├── install.sh
    │   └── taskhub-worker.service
    └── laptop/              # 个人电脑场景：计划任务（60s 巡检、pythonw 无窗口、免管理员）
        └── install.ps1
```

> `board.py`（v2 协议客户端）不在此目录重复拷贝：安装器从仓库 `dsh/`（服务器）或
> `dsh-laptop/`（个人电脑）复用同一份，保持协议实现单一真源。

## 快速开始

### 服务器（开机即守护、断线退避、异常自愈）

```bash
sudo GITHUB_TOKEN=github_pat_xxx WORKER_NAME=dsh-tencent \
    bash dsh-for-all/deploy/server/install.sh
# 主路线=systemd：Restart=always，worker 崩溃 15s 内自拉，开机自启
journalctl -u taskhub-worker -f          # 看日志
```

无 systemd 的主机自动落到 cron 兜底（每分钟 `guard_all.py --once` 巡检 + `@reboot`
开机拉起——服务器场景要的就是开机即守护）。

### 个人电脑 / 笔记本（开机不自启，DSH 运行才抢）

```powershell
powershell -ExecutionPolicy Bypass -File dsh-for-all\deploy\laptop\install.ps1 `
    -ProjectRoot E:\DSH -Token github_pat_xxx -WorkerName dsh-laptop `
    [-DisableLegacyGuard]
# -DisableLegacyGuard：卸载 dsh-laptop 旧守卫计划任务，防 worker_laptop.py 与
#                      worker_all.py 双守护同名抢任务（首次迁移时建议加上）
```

计划任务 `TaskHubGuard` 每 60s 用 pythonw 隐藏执行 `guard_all.py --once`（免管理员）：

- 锚进程（`TASKHUB_ANCHOR`，默认 `DSH Desktop`）**在运行** → 保证恰好一个 worker 存活
  （自动去重/补拉，休眠唤醒后自愈）；
- 锚进程**不在运行** → 停止 worker（`taskkill /T` 连执行会话一起收干净——电脑开机但不开
  DSH 时零占用，巡检本身秒级完成）。**开机不自启抢任务程序。**

## 两种场景的差异点（如何切换）

**唯一的模式开关是 `TASKHUB_MODE=server|laptop`**（不设则按系统自动：Windows→laptop，
其余→server）。认领引擎完全相同（§3/§10/§11/§12 行为一致）；差异只存在于看门狗与安装器：

| 维度 | server 模式 | laptop 模式 |
|---|---|---|
| 进程模型 | systemd 常驻 `worker_all.py`（`Restart=always`）；无 systemd 时 `guard_all.py` 常驻循环或 cron 每分钟 `--once` | 计划任务每 60s 调 `guard_all.py --once` 单次巡检 |
| 开机自启 | **是**（`WantedBy=multi-user.target` / `@reboot`）——服务器要的就是开机即守护 | **否**——守卫只是每分钟一次秒级检查；锚进程不在时什么都不拉起 |
| 运行条件 | 永远运行，崩溃自动拉起 | 锚进程（默认 `DSH Desktop`）运行时才保活 worker |
| 退出行为 | 不退出（常驻） | DSH 关闭 → worker 连执行会话被 `taskkill /T` 收干净，无残余进程 |
| 资源占用 | 忽略不计（服务器优先稳定） | 空闲时零进程驻留，巡检 <1s；可选 `TASKHUB_IDLE_EXIT_MIN` 进一步省资源 |
| QQ 通知 | 默认开（`TASKHUB_NOTIFY_QQ=1`） | 安装器默认关（`0`，可选开） |
| worker 名 | `dsh-tencent`（云主机约定） | `dsh-laptop`（§1 每机全网唯一，参数可改） |

**在两种模式间切换**（代码零改动）：

1. 同一台机器从"笔记本条件触发"变"常驻抢任务"：设 `TASKHUB_MODE=server`，
   再二选一——把计划任务动作里的 `--once` 去掉（`guard_all.py` 会进入常驻循环，
   `--loop` 可强制）；或直接改装 server 场景的 systemd/cron 部署。反向切换同理。
2. 只想临时验证另一形态：`TASKHUB_MODE=server python guard_all.py --once`（单次巡检，
   不锚进程、确保 worker 存活）；`TASKHUB_MODE=laptop guard_all.py --once`（单次锚进程门控）。
3. worker 名必须随场景切换保持**全网唯一**（§1）：云主机 `dsh-tencent`、笔记本 `dsh-laptop`，
   不要让两台机器同名抢任务。

## 配置参考（环境变量，均可选）

| 变量 | 缺省 | 说明 |
|---|---|---|
| `TASKHUB_MODE` | 按系统自动 | `server` 常驻 / `laptop` 锚进程门控（唯一模式开关） |
| `TASKHUB_WORKER` | `dsh-all` | worker 名，**每机全网唯一**（§1） |
| `TASKHUB_CREDENTIALS` | `<目录>/credentials.env` | fine-grained PAT（仅本仓库 Issues 读写） |
| `TASKHUB_INBOX` | `<目录>/inbox/claims.log` | 认领事件落盘（回传会话） |
| `TASKHUB_POLL` | `15` | 轮询秒数（§11 下限 15） |
| `TASKHUB_MAX_LOAD` | `1` | 并发上限（§10；专属任务可突破） |
| `TASKHUB_LEASE_MIN` / `TASKHUB_HEARTBEAT_MIN` | `30` / `20` | 租约与心跳节流 |
| `TASKHUB_UNDECLARED` | `skip` | 正文无资格声明时的兜底策略：`skip`=严格 §3 不领；`通用`=旧看板兼容 |
| `TASKHUB_ANCHOR` | `DSH Desktop` | laptop 模式锚进程名 |
| `TASKHUB_PYTHON` | `sys.executable` | 看门狗拉 worker 用的解释器 |
| `TASKHUB_IDLE_EXIT_MIN` | `0`（关） | >0 时空闲 N 分钟自行退出，看门狗按需拉起（laptop 省资源可选） |
| `TASKHUB_NOTIFY_QQ` | `1` | QQ 通知开关（laptop 安装器默认 0） |
| `TASKHUB_EXEC` / `TASKHUB_EXEC_CWD` | `1` / 当前目录 | 自动执行开关 / headless 会话工作目录（计划任务环境务必设为可写目录） |
| `TASKHUB_DSH_EXE` / `TASKHUB_DSH_CLI_JS` | DSH Desktop 默认路径 | Windows headless 调起参数（Electron 直跑 desktop-cli.js） |

## 契约对齐（AGENTS.md）

- **§3 资格门禁**：正文「资格：…」行是唯一权威（`通用`/`专属 <X>`/`父 <N>`；
  声明行无法解析 → 不领）。`[公告]`、documentation 标签、被他人有效持有（含 v1 永久租约）
  一律不领。正文无声明行时默认不领（`TASKHUB_UNDECLARED=skip`），可选兼容梯子
  （标题括号 → 标签 `for:/parent:` → `[任务]` 前缀）。候选发现不依赖 pending 标签。
- **§10 容量**：认领前自查名下进行中任务数，≥ MAX_LOAD 不接通用/父组任务；
  专属任务按契约例外可超但仍计数。
- **§11 退避**：连续 2 次抢注失败本轮跳过；轮询 ≥15s；心跳=编辑认领评论（≥20 分钟节流）。
- **§12 优先**：合规可领任务中 P0/P1 优先，同级按创建时间 FIFO。
- **§9 交付**：实体产物按 `Result/<issue>_<slug>/` 上传（Contents API）；结果回传 issue 评论。
- **会话侧职责（重要）**：守护只负责接单+心跳+落盘 inbox（+可选自动执行器）；
  **会话收到认领后必须真正执行**，再
  `python worker_all.py 所在目录/board.py complete --issue <N> --worker <名> --result "…"`
  回传真实结果。严禁占位/伪完成。

## 自检与运维

```bash
# 单轮测试（不动常驻进程）
TASKHUB_ONCE=1 python3 worker_all.py

# 看门狗单次巡检 / 常驻循环
python3 guard_all.py --once
python3 guard_all.py            # server 模式常驻；laptop 模式请加 --loop

# 认领回传（会话侧）
tail -f inbox/claims.log   /   python3 notify_check.py

# 状态（服务器）    systemctl status taskhub-worker ; journalctl -u taskhub-worker -f
# 状态（笔记本）    Get-ScheduledTask TaskHubGuard ; Get-Content taskhub\worker.out.log -Tail 5
```

### 卸载

- 服务器：`sudo systemctl disable --now taskhub-worker`（cron 兜底部署则 `crontab -e` 删
  `guard_all.py` 两行）。
- 笔记本：`Unregister-ScheduledTask -TaskName TaskHubGuard -Confirm:$false`；
  可选清理用户级 `TASKHUB_*` 环境变量。锚进程门控意味着：即使不卸载，DSH 不开时也零占用。

## Windows 个人电脑常见坑（统一版已内置对策）

1. **计划任务默认 cwd=System32**：沙箱 ACL 授予必败，headless 会话命令通道全废（#53 教训）
   → 执行器显式 `cwd` 到可写目录（`TASKHUB_EXEC_CWD`）。
2. **`os.kill(pid, 0)` 在 Windows 会真杀进程** → 存活探测用 `OpenProcess` 只读句柄。
3. **cmd /c + 空格路径引号地狱** → `ELECTRON_RUN_AS_NODE=1` 直跑 `desktop-cli.js`。
4. **GBK 控制台** → `PYTHONUTF8=1`（安装器写入），board.py 自带 stdout reconfigure。
5. **计划任务闪窗** → pythonw GUI 子系统执行，全程无窗口、免管理员。
6. **家庭代理** → 安装器写入用户级 `HTTPS_PROXY`；守护断网自愈重试（§11 退避）。
