#!/usr/bin/env bash
# install.sh —— dsh-for-all 服务器场景一键部署（systemd 主路线 + cron 兜底）
# 服务器场景目标（继承 dsh/ 专版经验）：开机即守护、断线退避、租约续期、异常自愈。
#
# 用法:
#   sudo GITHUB_TOKEN=github_pat_xxx WORKER_NAME=dsh-tencent ./install.sh
#   # 可选: TASKHUB_DIR=/opt/taskhub SYSTEMD=1(默认，无 systemd 自动落 cron)
set -euo pipefail

TASKHUB_DIR="${TASKHUB_DIR:-/opt/taskhub}"
WORKER_NAME="${WORKER_NAME:-dsh-tencent}"
SRC_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # dsh-for-all/
SYSTEMD="${SYSTEMD:-1}"
PY3="$(command -v python3 || true)"
[ -n "$PY3" ] || { echo "需要 python3"; exit 1; }

mkdir -p "$TASKHUB_DIR/inbox"
install -m 0644 "$SRC_DIR/worker_all.py"   "$TASKHUB_DIR/"
install -m 0644 "$SRC_DIR/guard_all.py"    "$TASKHUB_DIR/"
install -m 0644 "$SRC_DIR/qq_notify.py"    "$TASKHUB_DIR/" 2>/dev/null || true
install -m 0644 "$SRC_DIR/notify_check.py" "$TASKHUB_DIR/" 2>/dev/null || true

# board.py 是协议参考客户端，单一真源在 dsh/（与 dsh-laptop/ 同一份）；缺失时走 Contents API 拉取
if [ -f "$SRC_DIR/../dsh/board.py" ]; then
  install -m 0644 "$SRC_DIR/../dsh/board.py" "$TASKHUB_DIR/"
else
  echo "[install] dsh/board.py 不在本地，尝试 Contents API 拉取 ..."
  curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN:?需要 GITHUB_TOKEN}" \
    "https://api.github.com/repos/hehehe1234567894/agent-tasks/contents/dsh/board.py" \
    | "$PY3" -c "import json,sys,base64;sys.stdout.write(base64.b64decode(json.load(sys.stdin)['content']).decode())" \
    > "$TASKHUB_DIR/board.py"
fi

# 凭据（已存在则不覆盖）
if [ ! -f "$TASKHUB_DIR/credentials.env" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
  umask 077; echo "GITHUB_TOKEN=$GITHUB_TOKEN" > "$TASKHUB_DIR/credentials.env"; umask 022
  echo "[install] credentials.env written"
fi

# 运行时环境（systemd EnvironmentFile 与 cron 共用）
cat > "$TASKHUB_DIR/taskhub.env" <<EOF
TASKHUB_WORKER=$WORKER_NAME
TASKHUB_CREDENTIALS=$TASKHUB_DIR/credentials.env
TASKHUB_INBOX=$TASKHUB_DIR/inbox/claims.log
TASKHUB_POLL=15
TASKHUB_MAX_LOAD=1
TASKHUB_LEASE_MIN=30
TASKHUB_HEARTBEAT_MIN=20
TASKHUB_UNDECLARED=skip
EOF

if [ "$SYSTEMD" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  sed -e "s#/opt/taskhub#$TASKHUB_DIR#g" \
      -e "s#/usr/bin/python3#$PY3#g" \
      "$(dirname "$0")/taskhub-worker.service" > /etc/systemd/system/taskhub-worker.service
  systemctl daemon-reload
  systemctl enable --now taskhub-worker.service
  echo "[install] systemd unit enabled: taskhub-worker（开机即守护、崩溃 15s 自拉、断线退避）"
  echo "[install] 查看日志: journalctl -u taskhub-worker -f"
else
  # cron 兜底（无 systemd 主机）：每分钟巡检 + @reboot 开机拉起（服务器场景要的就是开机即守护）
  GUARD="$TASKHUB_DIR/guard_all.py"
  CRON_CMD="TASKHUB_MODE=server TASKHUB_PYTHON=$PY3 TASKHUB_SKILL_DIR=$TASKHUB_DIR TASKHUB_TASK_DIR=$TASKHUB_DIR $PY3 -u $GUARD --once >> $TASKHUB_DIR/guard.cron.log 2>&1"
  ( crontab -l 2>/dev/null | grep -v guard_all.py || true
    echo "* * * * * $CRON_CMD"
    echo "@reboot sleep 20; $CRON_CMD"
  ) | crontab -
  echo "[install] cron watchdog installed（每分钟巡检 + 开机拉起）"
fi

# 连通性自检（失败不阻断安装，给出提示）
"$PY3" "$TASKHUB_DIR/board.py" --credentials "$TASKHUB_DIR/credentials.env" list \
  && echo "[install] 看板连通 ✅" \
  || echo "[install] ⚠️ 看板连通失败：检查代理/令牌（守护会按 §11 退避持续重试）"

echo "[install] done. runtime: $TASKHUB_DIR"
