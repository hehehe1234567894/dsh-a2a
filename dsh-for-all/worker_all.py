#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
worker_all.py —— dsh-for-all 统一后台认领守护（服务器 / 个人电脑通吃版）

吸取 dsh/worker_dsh.py（云服务器：常驻稳定、断线退避、租约续期、异常自愈）与
dsh-laptop/worker_laptop.py + guard.ps1（个人电脑：条件触发、资源占用低、退出干净）
两套专版的实践优点，合并为单一实现；生命周期（何时运行/何时退出）交给
guard_all.py / systemd（见 deploy/），本文件只做「认领引擎」。

严格对齐仓库根目录 AGENTS.md 契约：

资格解析（§3 强制门禁，正文为准）：
    "资格：通用"        → 任何空闲 worker 可领
    "资格：专属 <X>"    → 仅 X 可领（允许突破 MAX_LOAD，但仍计入占用）
    "资格：父 <N>"      → 仅已认领父任务 N 的 worker 可领
  标题 [公告] 开头 / documentation 标签 / 资格行无法解析 → 一律不可领（宁可错过，不可误抢）。
  兼容梯子（仅正文**没有**资格行时才逐层尝试，可用 TASKHUB_UNDECLARED=skip 关闭）：
      标题括号 [专属 X]/[通用]（含全角【】）→ 标签 for:<X>/parent:<N> → 标题 [任务] 前缀
      → 兜底策略（TASKHUB_UNDECLARED=通用 时按通用处理）。
  候选发现不依赖 pending 标签：扫描全部 open issue，剔除 PR / 已终止 / 被有效认领持有 / 公告。

其余契约：§10 MAX_LOAD 容量（专属可超）、§11 连续 2 次抢失败本轮退避 + 轮询 ≥15s、
§12 P0/P1 优先同级 FIFO、心跳节流 ≥20 分钟（编辑认领评论续租，不刷屏）。

自动执行：认领成功后落盘 inbox/claims.log 回传会话；可选派生 DSH headless 会话真实执行
（TASKHUB_EXEC=0 关闭；Windows 复刻 DSH Desktop 调起方式，见 spawn_executor 注释与 #53 教训）。
自愈对账：守护重启/崩溃后，名下"已认领未完成"的任务会被自动接管/重新派生执行器；
SIGTERM/SIGINT 干净退出（任务租约到期自动回 pending，不留残余）。

用法:
  服务器  : systemd 常驻（deploy/server/）或 guard_all.py 常驻循环
  个人电脑: 计划任务每 60s 调 guard_all.py --once（deploy/laptop/），锚进程门控
  手动    : TASKHUB_ONCE=1 python worker_all.py      # 单轮测试
"""
import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board  # noqa: E402  与 dsh/ / dsh-laptop/ 同一份 v2 客户端（协议单一真源）

try:  # web API 执行器（会话在 DSH 宿主内运行，web 侧边栏可见，无沙箱 EACCES）
    import executor_web  # noqa: E402
    EXEC_WEB = executor_web.api_available()
except Exception:
    executor_web = None
    EXEC_WEB = False

BASE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.environ.get("TASKHUB_WORKER", "dsh-all")
REPO = os.environ.get("TASKHUB_REPO", "hehehe1234567894/agent-tasks")
CREDS = os.environ.get("TASKHUB_CREDENTIALS", os.path.join(BASE, "credentials.env"))
POLL = max(15, int(os.environ.get("TASKHUB_POLL", "15")))          # §11: >= 15s
MAX_LOAD = int(os.environ.get("TASKHUB_MAX_LOAD", "1"))            # §10
LEASE_MIN = int(os.environ.get("TASKHUB_LEASE_MIN", "30"))
HB_MIN = int(os.environ.get("TASKHUB_HEARTBEAT_MIN", "20"))
# §3 兜底策略：skip=严格执行"无资格声明不领"（缺省，契约优先）；通用=旧看板兼容（按通用领）
UNDECLARED = os.environ.get("TASKHUB_UNDECLARED", "skip").strip().lower()
if UNDECLARED not in ("skip", "通用"):
    UNDECLARED = "skip"
MODE = os.environ.get("TASKHUB_MODE", "").strip().lower()
if MODE not in ("server", "laptop"):
    MODE = "laptop" if os.name == "nt" else "server"
NOTIFY_QQ = os.environ.get("TASKHUB_NOTIFY_QQ", "1") == "1"
ONCE = os.environ.get("TASKHUB_ONCE", "") == "1"
QQ_SCRIPT = os.environ.get("TASKHUB_QQ_SCRIPT", os.path.join(BASE, "qq_notify.py"))
INBOX = os.environ.get("TASKHUB_INBOX", os.path.join(BASE, "inbox", "claims.log"))
IDLE_EXIT_MIN = int(os.environ.get("TASKHUB_IDLE_EXIT_MIN", "0"))  # >0: 空闲 N 分钟自行退出（laptop 省资源；guard 会按需拉起）
TERMINAL = {"done", "failed", "cancelled"}
BRACKET = re.compile(r"[【\[]\s*(专属|通用|父)\s*[:：\s]?\s*([^\]】]*)[\]】]")

EXEC_ENABLED = os.environ.get("TASKHUB_EXEC", "1") == "1"
EXEC_TIMEOUT_MIN = int(os.environ.get("TASKHUB_EXEC_TIMEOUT_MIN", "60"))
EXEC_DIR = os.path.join(BASE, "executions")
RUNNING = set()  # 已派生/接管执行会话、尚未结束的 issue 号


def _find_dsh():
    """可移植定位 dsh 可执行文件；找不到则禁用执行器（守护仍可认领+通知）。"""
    for cand in (os.environ.get("TASKHUB_DSH_BIN"), shutil.which("dsh"),
                 os.path.expanduser("~/.local/share/pnpm/dsh")):
        if cand and os.path.isfile(cand):
            return cand
    return None


DSH_BIN = _find_dsh()
EXEC_OFF = not (DSH_BIN and EXEC_ENABLED)
EXEC_MODE = "web" if (EXEC_WEB and executor_web is not None) else "headless"
if os.name == "nt":
    CHILD_PATH = os.environ.get("PATH", "")   # Windows: 继承系统 PATH
else:
    _child_paths = [os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")]
    for _d in (os.path.expanduser("~/.local/node/bin"),      # node（cron 环境 PATH 里没有）
               os.path.expanduser("~/.local/share/pnpm")):
        if os.path.isdir(_d):
            _child_paths.insert(0, _d)
    CHILD_PATH = ":".join(_child_paths)
    del _child_paths, _d


def log(*a):
    print("[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), " ".join(map(str, a))),
          flush=True)


def _graceful(signum, _frame):
    """干净退出：不再派生/认领；名下任务租约到期后自动回 pending，不留残余执行器。"""
    log("收到信号 %s，干净退出（名下任务租约到期自动回 pending）" % signum)
    sys.exit(0)


def args_ns():
    return argparse.Namespace(token=None, credentials=CREDS, state=None, repo=REPO)


def gh_for():
    token = board.load_credentials_file(CREDS) or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("找不到令牌：%s" % CREDS)
    return board.GitHub(token, REPO)


# ── 资格解析（§3 门禁，正文为准 + 可关兼容梯子）────────────────────

def _decl(val):
    val = val.strip()
    if val == "通用":
        return {"type": "通用"}
    if val.startswith("专属"):
        w = val[len("专属"):].strip()
        return {"type": "专属", "worker": w} if w else None
    if val.startswith("父"):
        p = val[len("父"):].strip().lstrip("#")
        return {"type": "父", "parent": int(p)} if p.isdigit() else None
    return None


def parse_eligibility(title, body, labels):
    """多格式资格解析。返回 {'type':...} 或 None(不可领)。

    层级：1) 正文契约格式（§3 唯一权威；有声明行但无法解析 → None）
          2~5) 仅当正文无声明行且 UNDECLARED != skip：标题括号 → 标签 → [任务] 前缀 → 兜底策略
    容错：剥 UTF-8 BOM（\ufeff，粘贴/Word 来源常见）后再匹配。"""
    t = (title or "").strip().lstrip("\ufeff")
    labels = set(labels or [])
    if t.startswith("[公告]") or "documentation" in labels:
        return None  # 公告不是任务
    declared = False
    for ln in (body or "").splitlines():
        s = ln.strip().lstrip("\ufeff")
        if s.startswith("资格"):
            declared = True
            raw = s.replace("：", ":", 1)
            if ":" in raw:
                e = _decl(raw.split(":", 1)[1])
                if e:
                    return e
            return None  # 有声明行但无法解析 → §3 不可领（宁可错过，不可误抢）
    if declared:
        return None
    if UNDECLARED == "skip":
        return None  # 严格契约：无资格声明 → 不可领
    # ── 兼容梯子（旧看板/未按契约发布的任务）──
    m = BRACKET.search(t)
    if m:
        kind, val = m.group(1), (m.group(2) or "").strip()
        e = {"type": "通用"} if kind == "通用" else _decl((kind + " " + val).strip())
        if e:
            return e
    for l in sorted(labels):
        if l.startswith("for:") and l[4:].strip():
            return {"type": "专属", "worker": l[4:].strip()}
    for l in sorted(labels):
        if l.startswith("parent:"):
            p = l[7:].lstrip("#")
            if p.isdigit():
                return {"type": "父", "parent": int(p)}
    if t.startswith("[任务]"):
        return {"type": "通用"}
    return {"type": "通用"} if UNDECLARED == "通用" else None


# ── 候选发现与认领（§8 发现 + §10 容量 + §11 退避 + §12 优先）──────

def my_open_claims(gh):
    """我名下租约仍有效的进行中任务（§10 自查 + 心跳对象）。"""
    mine = []
    now = board.now_utc()
    for iss in gh.issues(state="open"):
        if "pull_request" in iss:
            continue
        owner = board.resolve_owner(board.parse_claims(gh.comments(iss["number"])), now)
        if owner and owner["worker"] == WORKER:
            mine.append((iss["number"], iss.get("title") or ""))
    return mine


def candidates(gh, now):
    """候选 = open、非 PR、未终止、无主、非公告（不依赖 pending 标签，兼容漏打标签的任务）。"""
    out, held = [], 0
    for iss in gh.issues(state="open"):
        if "pull_request" in iss:
            continue
        labels = {lb["name"] for lb in iss.get("labels", [])}
        if labels & TERMINAL:
            continue
        t = (iss.get("title") or "").strip().lstrip("\ufeff")
        if t.startswith("[公告]") or "documentation" in labels:
            continue
        owner = board.resolve_owner(board.parse_claims(gh.comments(iss["number"])), now)
        if owner:
            held += 1
            continue  # 已被持有（含 v1 永久租约）
        out.append(iss)
    # §12: P0/P1 优先，同级 FIFO
    out.sort(key=lambda x: (min([board.PRIORITIES.get(lb["name"], 9)
                                 for lb in x.get("labels", [])] or [9]),
                            x.get("created_at") or "", x["number"]))
    return out, held


def pick_and_claim(gh, sp, mine_numbers, capacity_left):
    cands, held = candidates(gh, board.now_utc())
    claimed, fails, skipped = [], 0, 0
    for iss in cands:
        labels = [lb["name"] for lb in iss.get("labels", [])]
        elig = parse_eligibility(iss.get("title"), iss.get("body"), labels)
        if elig is None:
            skipped += 1
            continue
        if elig["type"] == "专属":
            if elig["worker"] != WORKER:
                skipped += 1
                continue  # 他人专属 → 严禁认领（§3 硬性规则）
            # 我的专属：允许突破 MAX_LOAD（§10 唯一例外）
        elif elig["type"] == "父":
            if elig["parent"] not in mine_numbers:
                skipped += 1
                continue  # 未领父任务 → 禁领子任务（§3 硬性规则）
            if capacity_left <= 0:
                continue
        else:  # 通用
            if capacity_left <= 0:
                continue  # 名额满 → 本轮不接通用任务（§10）
        got = board.try_claim(gh, iss, WORKER, LEASE_MIN, sp, REPO)
        if got:
            claimed.append((got, elig))
            if elig["type"] != "专属":
                capacity_left -= 1
            log("✅ 认领 #%s [%s] %s" % (got["issue"], elig["type"], got["title"]))
        else:
            fails += 1
            log("抢注失败 #%s（他人先到）" % iss["number"])
            if fails >= 2:  # §11: 连续 2 次失败 → 本轮退避
                log("连续 2 次抢注失败，本轮停止认领（退避）")
                break
    if cands:
        log("候选 %d 个（他人持有 %d，资格不符跳过 %d）→ 认领 %d"
            % (len(cands), held, skipped, len(claimed)))
    return claimed


# ── 会话回传 / 执行器（认领→执行闭环 + 自愈）──────────────────────

def echo_to_session(claim, elig):
    entry = {
        "time": datetime.datetime.now().astimezone().isoformat(),
        "event": "claimed",
        "worker": WORKER,
        "issue": claim["issue"],
        "title": claim["title"],
        "eligibility": elig,
        "url": claim["url"],
        "lease_until": claim["lease_until"],
        "body": claim["body"][:2000],
    }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(INBOX)), exist_ok=True)
        with open(INBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log("会话回传已落盘: %s" % INBOX)
    except OSError as e:
        log("会话落盘失败:", e)
    if NOTIFY_QQ:
        text = ("🤖 [任务看板] 认领新任务\n"
                "任务 #%s %s\n资格: %s\n"
                "链接: %s\n"
                "租约至 %s（守护进程自动续租，请尽快处理并 complete）"
                % (claim["issue"], claim["title"], elig["type"], claim["url"], claim["lease_until"]))
        try:
            r = subprocess.run([sys.executable, QQ_SCRIPT, text], timeout=40,
                               capture_output=True, text=True)
            log("QQ 汇报: exit=%s %s" % (r.returncode, (r.stdout or r.stderr).strip()[:160]))
        except Exception as e:
            log("QQ 汇报失败:", e)


def spawn_executor(claim):
    """派生 DSH headless 会话真实执行任务并自行 complete/fail。
    轻量原则：每任务至多一个执行器；pid 文件使守护重启后仍能接管/续管；超时自动回收。"""
    n = claim["issue"]
    if EXEC_OFF or n in RUNNING:
        return
    os.makedirs(EXEC_DIR, exist_ok=True)
    prompt = (
        f"[taskhub-exec:#{n}] 你是 TaskHub 看板的任务执行器（worker={WORKER}）。"
        f"请独立完成看板任务 #{n}：{claim['title']}\n"
        f"任务正文：\n{claim['body']}\n\n"
        "执行要求：\n"
        "1. 按正文的目标/验收标准真实完成（写文件、算题、查资料等直接动手，不要只给方案）。\n"
        f"2. 有实体产出时按仓库 AGENTS.md §9 用 Contents API 上传到 Result/{n}_<小写短横线slug>/ 目录。\n"
        f"3. 完成后务必运行：\"{sys.executable}\" -u \"{os.path.join(BASE, 'board.py')}\" --credentials {CREDS} complete --issue {n} "
        f"--worker {WORKER} --result \"<结果摘要，含 Result/ 路径与交付说明>\"\n"
        f"4. 无法完成则运行 fail --issue {n} --worker {WORKER} --error \"原因\"，不要静默放弃。\n"
        "5. 不要发布新任务，不要认领或修改其他 issue，不要动看板协议文件。"
    )
    logf = open(os.path.join(EXEC_DIR, f"{n}.log"), "w", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = CHILD_PATH
    env["HOME"] = os.path.expanduser("~")
    try:
        env["GITHUB_TOKEN"] = board.load_credentials_file(CREDS) or env.get("GITHUB_TOKEN", "")
    except Exception:
        pass
    try:
        if EXEC_WEB and executor_web is not None:
            # web API 模式（推荐）：在 DSH 宿主内创建会话，web 侧边栏实时可见；
            # 会话运行在宿主进程（无 landlock），彻底规避 headless 子进程的 EACCES 崩溃。
            sid = executor_web.create_session(cwd=BASE, session_id="taskhub-%d" % n)
            executor_web.prompt(sid, prompt)
            with open(os.path.join(EXEC_DIR, f"{n}.pid"), "w", encoding="utf-8") as f:
                json.dump({"sid": sid, "pid": 0, "ts": int(time.time()), "issue": n}, f)
            RUNNING.add(n)
            log("🚀 已通过 web API 派生执行会话 #%s（sid=%s…，web 侧边栏可见）" % (n, sid[:20]))
            return
        # 回退：headless 子进程模式（web API 不可用时）
        if os.name == "nt":
            # Windows: 复刻 dsh.cmd 的调起方式但绕开 cmd（路径含空格时 cmd /c 引号解析不可靠）：
            # Electron 以 ELECTRON_RUN_AS_NODE=1 直接跑 desktop-cli.js。
            # DETACHED_PROCESS 使执行会话脱离守护生命周期独立存活，且无控制台窗口。
            exe = os.environ.get("TASKHUB_DSH_EXE", r"C:\Program Files\DSH Desktop\DSH Desktop.exe")
            root = os.path.dirname(exe)
            cli = os.environ.get("TASKHUB_DSH_CLI_JS",
                                 os.path.join(root, "resources", "app.asar", "lib", "desktop-cli.js"))
            env["ELECTRON_RUN_AS_NODE"] = "1"
            env["DSH_HOME"] = os.environ.get("DSH_HOME", os.path.join(os.path.expanduser("~"), ".dsh"))
            env["DSH_DESKTOP_DEFAULT_PROFILE"] = "desktop"
            # cwd 必须是可写工作区：计划任务默认 cwd=System32，沙箱对其授 ACL 必败，
            # 导致执行会话所有命令通道不可用（#53 教训）。
            exec_cwd = os.environ.get("TASKHUB_EXEC_CWD") or os.getcwd()
            proc = subprocess.Popen([exe, "--expose-internals", cli,
                                     "--profile", "headless", prompt],
                                    stdout=logf, stderr=subprocess.STDOUT, env=env,
                                    cwd=exec_cwd,
                                    creationflags=0x00000008 | 0x08000000)
        else:
            proc = subprocess.Popen([DSH_BIN, "--profile", "headless", prompt],
                                    stdout=logf, stderr=subprocess.STDOUT, env=env,
                                    start_new_session=True)
        with open(os.path.join(EXEC_DIR, f"{n}.pid"), "w", encoding="utf-8") as f:
            json.dump({"pid": proc.pid, "ts": int(time.time()), "issue": n}, f)
        RUNNING.add(n)
        log("🚀 已派生执行会话处理 #%s，日志 executions/%s.log" % (n, n))
    except Exception as e:
        log("派生执行会话失败 #%s:" % n, e)


def executor_state(n):
    """读 pid 文件判断执行器状态：alive / dead / timeout / None(无记录)。"""
    pf = os.path.join(EXEC_DIR, f"{n}.pid")
    try:
        with open(pf, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return None, 0
    pid, ts = int(rec.get("pid", 0)), int(rec.get("ts", 0))
    sid = rec.get("sid") or ""
    if sid:  # web API 执行器：按会话 running 状态判定
        running, _title, _upd = (executor_web.session_status(sid)
                                 if executor_web is not None else (False, None, None))
        age = int(time.time() - ts)
        if not running:
            return "dead", age  # 会话结束（完成或崩溃）→ ensure_executors 按任务状态重派生
        if age >= EXEC_TIMEOUT_MIN * 60:
            try:  # 超时：请求取消会话
                executor_web.rpc("session.cancel", {"sessionId": sid})
            except Exception:
                pass
            try:
                os.remove(pf)
            except OSError:
                pass
            return "timeout", age
        return "alive", age
    alive = False
    if pid > 0:
        if os.name == "nt":
            # Windows: os.kill(pid, 0) 会 TerminateProcess！改用 OpenProcess 只读探测。
            try:
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if h:
                    ctypes.windll.kernel32.CloseHandle(h)
                    alive = True
            except Exception:
                alive = False
        else:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
    age = int(time.time() - ts)
    if not alive:
        return "dead", age
    if age >= EXEC_TIMEOUT_MIN * 60:
        try:  # 超时回收：终止整个执行会话进程树
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=20)
            elif hasattr(os, "killpg"):
                os.killpg(pid, 9)
            else:
                os.kill(pid, 9)
        except OSError:
            pass
        try:
            os.remove(pf)
        except OSError:
            pass
        return "timeout", age
    return "alive", age


def ensure_executors(gh, mine):
    """每轮自愈对账：名下每个进行中任务都必须有活着的执行器。
    覆盖守护重启/崩溃后"接了任务没执行"的场景（服务器异常自愈的核心）。"""
    if EXEC_OFF:
        return
    for num, _lease in mine:
        if num in RUNNING:
            st, age = executor_state(num)
            if st == "timeout":
                RUNNING.discard(num)
                log("⏱ 执行器 #%s 超时（≥%d 分钟）已终止；任务租约到期后将自动释放" % (num, EXEC_TIMEOUT_MIN))
            elif st != "alive":
                RUNNING.discard(num)  # 进程已死但仍在 RUNNING → 交给下方重新派生
            continue
        st, age = executor_state(num)
        if st == "alive":
            RUNNING.add(num)  # 守护重启恢复：接管既有执行器，不重复派生
            log("🔗 接管已有执行器 #%s（守护重启恢复，已运行 %d 分钟）" % (num, age // 60))
            continue
        if st == "timeout":
            log("⏱ 旧执行器 #%s 已超时清理" % num)
            continue
        try:
            iss = gh.issue(num)
            claim = {"issue": num, "title": iss.get("title") or "", "body": iss.get("body") or ""}
        except Exception as e:
            log("读取任务 #%s 失败（下轮重试）:" % num, e)
            continue
        spawn_executor(claim)  # 成功时内部已 RUNNING.add(n)


# ── 主循环 ───────────────────────────────────────────────────────

def one_round(gh, sp, last_hb):
    """跑一轮。返回 True 表示"忙"（名下有任务或本轮有新认领）。"""
    mine = my_open_claims(gh)
    mine_numbers = {num for num, _ in mine}
    for n in list(RUNNING - mine_numbers):  # 已结束任务的执行器记录清理
        RUNNING.discard(n)
        try:
            os.remove(os.path.join(EXEC_DIR, f"{n}.pid"))
        except OSError:
            pass
    now_ts = time.time()
    for num, title in mine:
        if now_ts - last_hb.get(num, 0) >= HB_MIN * 60:
            try:
                board.do_heartbeat(gh, num, WORKER, LEASE_MIN, sp, REPO)
                last_hb[num] = now_ts
                log("心跳续租 #%s（租约 %d 分钟）" % (num, LEASE_MIN))
            except SystemExit:
                last_hb.pop(num, None)
    capacity_left = MAX_LOAD - len(mine)
    if mine:
        log("名下进行中 %d/%d %s" % (len(mine), MAX_LOAD,
                                     "(专属可超)" if capacity_left <= 0 else ""))
    claimed = pick_and_claim(gh, sp, mine_numbers, capacity_left)
    for claim, elig in claimed:
        echo_to_session(claim, elig)
        if not EXEC_OFF:
            spawn_executor(claim)
    ensure_executors(gh, mine)  # 自愈对账：重启/孤儿认领也会被补上执行器
    if not claimed and not mine:
        log("暂无可认领任务（空闲 %d/%d）" % (len(mine), MAX_LOAD))
    return bool(mine or claimed)


def main():
    log("worker_all 启动 mode=%s worker=%s poll=%ds max_load=%d lease=%dmin hb>=%dmin "
        "undeclared=%s exec=%s execmode=%s [契约:§3资格门禁 §10容量 §11退避 §12优先]"
        % (MODE, WORKER, POLL, MAX_LOAD, LEASE_MIN, HB_MIN, UNDECLARED,
           "off" if EXEC_OFF else "on", EXEC_MODE))
    signal.signal(signal.SIGINT, _graceful)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _graceful)
    sp = board.state_path_for(args_ns())
    last_hb = {}
    last_busy = time.time()
    while True:
        try:
            gh = gh_for()
            busy = one_round(gh, sp, last_hb)
            if busy:
                last_busy = time.time()
            elif IDLE_EXIT_MIN > 0 and time.time() - last_busy >= IDLE_EXIT_MIN * 60:
                log("空闲已 ≥ %d 分钟，自行退出（看门狗/锚进程恢复活动时会按需拉起）" % IDLE_EXIT_MIN)
                return
        except Exception as e:
            log("异常（重试中）:", e)
        if ONCE:
            log("单轮模式结束")
            return
        time.sleep(POLL)


if __name__ == "__main__":
    main()
