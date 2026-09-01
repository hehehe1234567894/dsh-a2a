#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guard_all.py —— TaskHub 统一看门狗（服务器/个人电脑通用）

单一实现覆盖两种部署形态，由 TASKHUB_MODE 切换：

  server 模式（云主机/常开服务器）:
      guard_all.py            # 前台常驻循环：每 TICK 秒确保恰好一个 worker 存活
      guard_all.py --once     # 单次巡检（给 cron * * * * * 兜底用，systemd 用户不需要）
    服务器上推荐直接用 systemd 拉起 worker_all.py（Restart=always，见 deploy/server/），
    systemd 本身就是看门狗；guard_all.py 仅作无 systemd 主机的兜底。

  laptop 模式（个人电脑/笔记本，开机不自启抢任务程序）:
      guard_all.py --once     # 计划任务每分钟调一次：
      锚进程（默认 "DSH Desktop"）在运行 → 确保恰好一个 worker 存活（WMI/proc 检测+去重）；
      锚进程不在运行 → 结束 worker（DSH 关闭即停止抢任务，电脑开机零占用）。

worker 存活检测（零第三方依赖）:
    Windows : powershell Get-CimInstance 扫 python 进程命令行（含 worker_all.py 者），
              失败时回退 worker.pid
    POSIX   : 扫 /proc/<pid>/cmdline

环境变量（均可选，与 worker_all.py 同一套）:
    TASKHUB_MODE=server|laptop   模式（缺省: Windows→laptop，其余→server）
    TASKHUB_ANCHOR="DSH Desktop" laptop 模式锚进程名
    TASKHUB_PYTHON=python.exe    拉起 worker 用的解释器（缺省 sys.executable）
    TASKHUB_SKILL_DIR=...        worker_all.py 所在目录（缺省本脚本同目录）
    TASKHUB_TASK_DIR=...         运行时目录（日志/pid/inbox，缺省同上）
    TASKHUB_GUARD_TICK=60        server 常驻循环间隔（秒）

对齐 AGENTS.md：本脚本只管生命周期；认领/心跳/退避/容量全部在 worker_all.py（§3/§10/§11/§12）。
"""
import argparse
import os
import platform
import subprocess
import sys
import time

MODE = os.environ.get("TASKHUB_MODE", "").strip().lower()
if MODE not in ("server", "laptop"):
    MODE = "laptop" if platform.system() == "Windows" else "server"
ANCHOR = os.environ.get("TASKHUB_ANCHOR", "DSH Desktop")
PYTHON = os.environ.get("TASKHUB_PYTHON") or sys.executable
GUARD_TICK = max(15, int(os.environ.get("TASKHUB_GUARD_TICK", "60")))

BASE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.environ.get("TASKHUB_SKILL_DIR") or BASE
TASK_DIR = os.environ.get("TASKHUB_TASK_DIR") or BASE
WORKER = os.path.join(SKILL_DIR, "worker_all.py")
PID_FILE = os.path.join(TASK_DIR, "worker.pid")
OUT_LOG = os.path.join(TASK_DIR, "worker.out.log")
ERR_LOG = os.path.join(TASK_DIR, "worker.err.log")
GUARD_LOG = os.path.join(TASK_DIR, "guard.log")


def log(*a):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(map(str, a)))
    try:
        with open(GUARD_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


# ── worker 存活检测 ──────────────────────────────────────────────

def _win_worker_pythons():
    """Windows: 用 powershell(Win32_Process) 找命令行含 worker_all.py 的 python 进程。"""
    try:
        creation = 0x08000000  # CREATE_NO_WINDOW
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
             "| Where-Object { $_.CommandLine -match 'worker_all' } "
             "| ForEach-Object { \"$($_.ProcessId)\" }"],
            capture_output=True, text=True, timeout=20, creationflags=creation).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def _posix_worker_pythons():
    """POSIX: 扫 /proc/<pid>/cmdline，命令行含 worker_all.py 的进程。"""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "worker_all" in cmd and ("python" in cmd or "python3" in cmd):
            pids.append(int(entry))
    return pids


def worker_pids():
    if platform.system() == "Windows":
        pids = _win_worker_pythons()
        if pids:
            return pids
        # 回退：pid 文件 + 进程名核验
        try:
            with open(PID_FILE, encoding="ascii") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return []
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                kernel32.CloseHandle(h)
                return [pid]
        except Exception:
            pass
        return []
    return _posix_worker_pythons()


def read_pid_file():
    try:
        with open(PID_FILE, encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def stop_pid(pid):
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    except Exception as e:
        log("stop pid=%s 失败: %s" % (pid, e))


# ── worker 拉起 ──────────────────────────────────────────────────

def spawn_worker():
    os.makedirs(TASK_DIR, exist_ok=True)
    os.makedirs(os.path.join(SKILL_DIR, "inbox"), exist_ok=True)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    kwargs = {}
    if platform.system() == "Windows":
        # DETACHED_PROCESS: 脱离看门狗生命周期独立存活；无控制台窗口
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    with open(OUT_LOG, "ab") as out, open(ERR_LOG, "ab") as err:
        p = subprocess.Popen([PYTHON, "-u", WORKER], stdout=out, stderr=err,
                             cwd=TASK_DIR, **kwargs)
    with open(PID_FILE, "w", encoding="ascii") as f:
        f.write(str(p.pid))
    log("spawned worker pid=%s worker=%s" % (p.pid, WORKER))


# ── 锚进程检测（仅 laptop 模式）─────────────────────────────────

def anchor_running():
    if platform.system() == "Windows":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[bool](Get-Process -Name '%s' -ErrorAction SilentlyContinue)" % ANCHOR.replace("'", "''")],
                capture_output=True, text=True, timeout=20,
                creationflags=0x08000000).stdout
            return "True" in out
        except Exception:
            return True  # 检测失败宁可保守（继续抢任务）
    # POSIX: 锚进程名出现在任一 /proc/<pid>/stat 的 comm 或 cmdline
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if ANCHOR in cmd:
            return True
    return False


# ── 巡检逻辑 ────────────────────────────────────────────────────

def tick():
    if MODE == "laptop":
        if not anchor_running():
            pids = worker_pids()
            for pid in pids:
                stop_pid(pid)
            if pids:
                log("锚进程 '%s' 未运行: 停止 worker %s（停止抢任务）" % (ANCHOR, pids))
            return
        pids = worker_pids()
        if len(pids) > 1:
            for dup in sorted(pids)[1:]:
                stop_pid(dup)
                log("去重: 结束多余 worker pid=%s（保留 %s）" % (dup, sorted(pids)[0]))
        elif not pids:
            spawn_worker()
        return
    # server 模式: 确保恰好一个 worker
    pids = worker_pids()
    if len(pids) > 1:
        for dup in sorted(pids)[1:]:
            stop_pid(dup)
            log("去重: 结束多余 worker pid=%s" % dup)
    elif not pids:
        spawn_worker()


def main():
    ap = argparse.ArgumentParser(description="TaskHub unified watchdog")
    ap.add_argument("--once", action="store_true",
                    help="单次巡检后退出（cron/计划任务场景；server 模式缺省常驻）")
    ap.add_argument("--loop", action="store_true",
                    help="强制常驻循环（laptop 模式缺省单次；在一台机器上手动常驻时用）")
    args = ap.parse_args()
    log("guard_all 启动 mode=%s anchor=%r tick=%ss worker=%s" % (MODE, ANCHOR, GUARD_TICK, WORKER))
    if args.loop or (MODE == "server" and not args.once):
        while True:
            try:
                tick()
            except Exception as e:
                log("巡检异常（继续）: %s" % e)
            time.sleep(GUARD_TICK)
        return
    tick()  # --once 或 laptop 缺省：单次巡检（计划任务每分钟驱动，秒退不留驻留）


if __name__ == "__main__":
    main()
