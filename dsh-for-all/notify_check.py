#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify_check.py —— 认领事件 → 会话播报检查器（单次执行，可重复调用，不重不漏）

读取 inbox/claims.log（JSONL，由 worker_all.py 每次认领后追加）与 .offset（已播报行数），
把新认领事件格式化输出到 stdout——供 DSH 会话提醒器执行并把结果显示到对话，
实现"认领即显示在 DSH 对话"。

用法:
  python3 notify_check.py          # 有新事件 → 人类可读播报；无 → "⏳ 无新认领"
  python3 notify_check.py --json   # JSON 输出（程序解析用）
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.environ.get("TASKHUB_INBOX", os.path.join(BASE, "inbox", "claims.log"))
OFFSET = os.path.join(os.path.dirname(os.path.abspath(INBOX)), ".offset")


def load_offset():
    try:
        with open(OFFSET, encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def save_offset(n):
    try:
        with open(OFFSET, "w", encoding="utf-8") as f:
            f.write(str(n))
    except OSError:
        pass


def main():
    as_json = "--json" in sys.argv
    off = load_offset()
    try:
        with open(INBOX, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    events = []
    for ln in lines[off:]:
        if not ln.strip():
            continue
        try:
            events.append(json.loads(ln))
        except ValueError:
            events.append({"raw": ln[:120]})

    if as_json:
        print(json.dumps({"total": len(lines), "offset": off, "new": events},
                         ensure_ascii=False, indent=2))
    elif not events:
        print("⏳ 无新认领")
    else:
        for e in events:
            if "raw" in e:
                print("• (未解析事件) %s" % e["raw"])
                continue
            elig = e.get("eligibility") or {}
            etype = elig.get("type", "?") if isinstance(elig, dict) else str(elig)
            print("🙋 认领 #%s [%s] %s" % (e.get("issue"), etype, e.get("title")))
            print("   链接  : %s" % e.get("url"))
            print("   租约至: %s（守护自动续租；会话侧请及时 complete/fail）" % e.get("lease_until"))
    save_offset(len(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
