# DESIGN.md —— dsh/ 与 dsh-laptop/ 两套 worker 的设计经验与差异总结

> 本文是 `dsh-for-all/` 统一方案的设计依据：先总结 `dsh/`（云服务器版）与
> `dsh-laptop/`（个人电脑版）两套 worker 各自踩过的坑与取舍，再说明统一版如何吸收两者。

## 1. 两套专版的基本盘

| 维度 | dsh/（worker_dsh.py，云服务器） | dsh-laptop/（worker_laptop.py + guard.ps1，个人电脑） |
|---|---|---|
| 宿主 | 公网服务器，7×24 常开，Linux | Windows 笔记本/桌面机，人用为主，开关机/休眠频繁 |
| 设计第一目标 | **常驻稳定**：绝不因守护掉线漏任务 | **省电不打扰**：开机不自启、DSH 不开不抢任务 |
| 生命周期 | nohup 直跑 + cron `keepalive.sh` 每分钟保活 + `@reboot` 开机拉起 | 计划任务每 60s 调 `guard.ps1`；锚进程（DSH Desktop）在→保活，不在→杀掉 worker |
| 认领引擎 | 严格契约 §3：只认正文「资格：…」行 | 多格式容错：正文→标题括号→标签→`[任务]`兜底（应对旧看板漏格式的任务） |
| 候选发现 | `open + pending` 标签（§8） | 全量扫 open issue（不依赖 pending 标签，兼容漏打标签） |
| 执行闭环 | 认领后自动派生 `dsh --profile headless` 会话真实执行 + pid 自愈对账 | 只认领+心跳+落盘 inbox，执行交给 DSH 会话侧（claim-wake 插件注入） |
| 通知 | QQ 默认开 | QQ 默认关（免打扰），走 claim-wake 会话注入 |
| 已知取舍 | 格式严格→旧任务可能漏领；全量执行→服务器负载更高 | 格式宽松→更耐脏；不自动执行→依赖会话侧自觉 |

## 2. 值得固化的经验（两版共同验证）

1. **认领锁放评论、标签只给人看**。GitHub 评论 ID 全局单调递增，天然全序；
   `__CLAIM_BY__ <worker> lease=<RFC3339> claim_id=<uuid>` + "租约未过期不可抢"裁决，
   让任何客户端独立重算出唯一持有者。两版共用同一份 `board.py`，这是全仓库最稳的一层。
2. **心跳=编辑认领评论**，不是新评论。30 分钟租约 + ≥20 分钟节流，长任务不丢、看板不刷屏。
3. **§10 容量自查（MAX_LOAD，默认 1）** 是防"接了做不完→租约过期白做→霸占看板"的关键；
   专属任务是唯一例外（明确指派，允许突破但仍计数）。
4. **§11 退避**：连续 2 次抢失败本轮停手、轮询 ≥15s——多机并发抢注时防评论刷屏与 API 限流。
5. **资格解析宁可错过、不可误抢**：`[公告]`、documentation 标签、无法解析的声明一律不领。

## 3. 各自踩过的坑（统一版必须内置的对策）

### 服务器版（dsh/）的坑
- **cron 环境 PATH 干瘪**：没有 `~/.local/node/bin`，headless 会话拉不起来 →
  worker 主动把 node/pnpm 目录插进子进程 PATH。
- **守护崩溃=全停**：只有 cron 每分钟兜底，最坏 1 分钟空窗 → systemd `Restart=always`
  把空窗压到 15 秒（统一版主路线）。
- **只认 pending 标签**：发布方漏打标签任务永远不可见 → 统一版全量扫描兜底。

### 笔记本版（dsh-laptop/）的坑
- **计划任务默认 cwd=System32**：沙箱对 System32 授 ACL 必败，headless 会话所有命令通道
  不可用（#53 教训）→ 执行器必须显式 `cwd` 到可写工作区（`TASKHUB_EXEC_CWD`）。
- **Windows 上 `os.kill(pid, 0)` 不是探测是TerminateProcess** → 存活探测改用
  `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` 只读句柄。
- **cmd /c 调 Electron 引号地狱**（路径含空格）→ 直接 `ELECTRON_RUN_AS_NODE=1` 跑
  `desktop-cli.js`，绕开 cmd。
- **GBK 控制台毁中文/emoji** → `PYTHONUTF8=1` + stdout reconfigure（board.py 已内置）。
- **计划任务闪黑窗** → `pythonw`/wscript GUI 链路，全程无窗口、免管理员。
- **git schannel 损坏** → git 走 openssl 后端；Python urllib 不受影响（board.py 纯标准库）。
- **代理家庭化**（127.0.0.1:10891）→ 安装器写进用户级环境变量，守护断网自愈重试。

### 客户端层发现的潜在 bug（两版共有，统一版已规避）
- `dsh/board.py` 与 `dsh-laptop/board.py` 的 `parse_eligibility()` 使用了 `re.search`
  但文件头部**没有 `import re`**——走 `board.py claim` CLI 手动认领时会 NameError
  （守护进程用自带解析逻辑，不受影响）。建议后续在两份专版里补一行 `import re`。

## 4. 统一方案的核心决策（dsh-for-all）

1. **一份认领引擎，生命周期外置**。`worker_all.py` 只管「发现→资格门禁→认领→心跳→
   落盘→执行器自愈」；"什么时候该运行/该退出"全部交给 `guard_all.py`/systemd/计划任务。
   服务器与笔记本的差异被压缩到**生命周期层**，认领逻辑零分叉、修一处两场景受益。
2. **唯一模式开关 `TASKHUB_MODE=server|laptop`**（缺省按系统自动：Windows→laptop，
   其余→server）。看门狗用它选行为：server=常驻确保恰一 worker；laptop=锚进程门控。
   worker 自己也读它（日志标注/省电策略），但**不会**因模式改变资格与契约行为。
3. **资格解析：契约优先 + 可关兼容梯子**。正文「资格：」行是唯一权威（无法解析→不领）；
   只有正文**完全没有**声明行时才走 标题括号→标签→`[任务]` 梯子，且默认
   `TASKHUB_UNDECLARED=skip`（严格 §3）。要兼容旧看板改成 `通用` 即可——把笔记本版
   的"耐脏"变成显式选项而不是隐式默认。
4. **执行器自愈搬进统一版**（服务器版精华）：pid 文件 + 每轮对账，守护重启后接管既有
   执行器、孤儿认领补派生、超时回收。笔记本上同样受益（休眠唤醒后继续推进）。
5. **退出干净**：SIGTERM/SIGINT 优雅退出；laptop 停 worker 走 `taskkill /T` 连执行会话
   一起收（"不留残余进程"是硬要求）；租约到期自动回 pending，不占坑。
6. **省电选项 `TASKHUB_IDLE_EXIT_MIN`**（缺省 0=不启用）：空闲 N 分钟自行退出，
   看门狗/锚进程恢复活动时按需拉起——把"资源占用低"做成可选策略而非强制行为。
7. **board.py 单一真源**：dsh-for-all 不再内置第三份拷贝，安装器从 `dsh/`（服务器）或
   `dsh-laptop/`（笔记本）复用同一份 v2 客户端，避免三份文件漂移。

## 5. 交付物与验收对照

| 验收标准 | 落点 |
|---|---|
| 仓库根目录新建 `dsh-for-all/` | 本目录 |
| 统一守护核心代码 | `worker_all.py`（认领引擎）+ `guard_all.py`（看门狗） |
| README.md 部署说明（服务器+个人电脑两场景） | `README.md` |
| 明确写出两场景配置差异点/切换方式 | `README.md`「两种场景的差异点」一节 |
| 认领/完成逻辑符合 AGENTS.md §3/§10/§11 | `worker_all.py`（正文为准门禁、MAX_LOAD 容量、2 次退避、≥15s 轮询）+ §9 实体交付走 `Result/` |
