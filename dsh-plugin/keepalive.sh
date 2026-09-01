#!/usr/bin/env bash
# ============================================================
# keepalive.sh —— 旧版保活入口（dsh-for-all 新版薄封装）
# 外部每分钟调度（旧 cron/提醒器）继续调本脚本；实际工作全部交给
# guard_all.py（常驻时本脚本秒退零开销，guard 挂了才兜底拉起 + QQ 通知）
# ============================================================
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/keepalive.log"

# 常驻 guard 还活着 → 什么都不用做
if pgrep -f "python3 (-u )?guard_all\.py" >/dev/null 2>&1; then
  exit 0
fi

# guard 不在 → 兜底单次巡检（会自动拉起 worker_all）
set -a; source "$DIR/taskhub.env" 2>/dev/null; set +a
python3 "$DIR/guard_all.py" --once >> "$LOG" 2>&1

# 巡检后 worker 起来了 → 发掉线恢复通知（QQ 发送失败不影响保活）
if pgrep -f "worker_all\.py" >/dev/null 2>&1; then
  python3 "$DIR/qq_notify.py" "🔄 [TaskHub] dsh-tencent 守护曾掉线，已自动恢复运行（dsh-for-all 新版）" >> "$LOG" 2>&1 || true
fi
