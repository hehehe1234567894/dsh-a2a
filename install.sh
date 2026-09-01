#!/usr/bin/env bash
# ============================================================
# install.sh —— TaskHub 看板插件 + dsh-for-all 守护 一键安装
# 零第三方依赖：只需 python3 + curl（公开仓库，下载源码无需令牌）
#
# 用法:
#   GITHUB_TOKEN=github_pat_xxx WORKER_NAME=dsh-tencent bash install.sh --with-daemon
#   可选环境变量: SKILL_DIR=<dir> 覆盖 skill 目录（默认 ~/.dsh/skills/taskhub）
#                 TASKHUB_HOME=<dir> 覆盖运行目录（默认 ~/DSHBuild/taskhub）
#
# DSH 机器推荐用一条 dsh 指令安装（agent 自动执行本脚本），见 README「快速安装」
# ============================================================
set -euo pipefail

REPO="hehehe1234567894/dsh-a2a"
BRANCH="main"
WORKER_NAME="${WORKER_NAME:-dsh-all}"
SKILL_DIR="${SKILL_DIR:-$HOME/.dsh/skills/taskhub}"
TASKHUB_HOME="${TASKHUB_HOME:-$HOME/DSHBuild/taskhub}"
WITH_DAEMON=0
for a in "$@"; do [ "$a" = "--with-daemon" ] && WITH_DAEMON=1; done

say() { printf '%s\n' "[install] $*"; }

command -v python3 >/dev/null 2>&1 || { echo "[install] 缺 python3"; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "[install] 缺 curl"; exit 1; }

# 1) 获取源码：TASKHUB_SRC_DIR 非空则用现成源码目录（离线安装），否则从 codeload 下载（公开仓库，无需令牌）
if [ -n "${TASKHUB_SRC_DIR:-}" ]; then
  SRC="$TASKHUB_SRC_DIR"
  say "使用本地源码: $SRC"
else
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  say "下载 $REPO@$BRANCH 源码..."
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" | tar -xz -C "$TMP"
  SRC="$TMP/dsh-a2a-$BRANCH"
  [ -d "$SRC" ] || SRC="$TMP/$(ls "$TMP" | head -1)"
fi

# 2) 安装 skill（DSH 技能入口：board.py + SKILL.md）
say "安装 skill → $SKILL_DIR"
if ! mkdir -p "$SKILL_DIR" 2>/dev/null; then
  say "!! $SKILL_DIR 不可写（DSH 沙箱限制），回退到 $TASKHUB_HOME/skill"
  SKILL_DIR="$TASKHUB_HOME/skill"
  mkdir -p "$SKILL_DIR"
fi
install -m 0755 "$SRC/dsh-plugin/board.py" "$SKILL_DIR/board.py"
install -m 0644 "$SRC/dsh-plugin/SKILL.md" "$SKILL_DIR/SKILL.md"

# 3) 安装运行时（认领引擎 + 看门狗 + 通知 + 客户端 + 保活薄封装）
say "安装运行时 → $TASKHUB_HOME"
mkdir -p "$TASKHUB_HOME/inbox"
for f in worker_all.py guard_all.py qq_notify.py notify_check.py executor_web.py; do
  install -m 0644 "$SRC/dsh-for-all/$f" "$TASKHUB_HOME/$f"
done
install -m 0755 "$SRC/dsh-plugin/board.py"     "$TASKHUB_HOME/board.py"
install -m 0755 "$SRC/dsh-plugin/keepalive.sh" "$TASKHUB_HOME/keepalive.sh"

# 4) 配置（已存在则不覆盖）
if [ ! -f "$TASKHUB_HOME/taskhub.env" ]; then
  cat > "$TASKHUB_HOME/taskhub.env" <<EOF
TASKHUB_MODE=server
TASKHUB_WORKER=$WORKER_NAME
TASKHUB_CREDENTIALS=$TASKHUB_HOME/credentials.env
TASKHUB_INBOX=$TASKHUB_HOME/inbox/claims.log
TASKHUB_POLL=15
TASKHUB_MAX_LOAD=1
TASKHUB_LEASE_MIN=30
TASKHUB_HEARTBEAT_MIN=20
TASKHUB_NOTIFY_QQ=1
EOF
  say "taskhub.env 已生成（worker=$WORKER_NAME）"
fi
if [ ! -f "$TASKHUB_HOME/credentials.env" ]; then
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    umask 077; printf 'GITHUB_TOKEN=%s\n' "$GITHUB_TOKEN" > "$TASKHUB_HOME/credentials.env"; umask 022
    say "credentials.env 已写入（600，需 Issues 读写权限的 fine-grained PAT）"
  else
    say "!! 未提供 GITHUB_TOKEN：请手动写入 $TASKHUB_HOME/credentials.env（GITHUB_TOKEN=github_pat_xxx）"
  fi
fi

# 5) 守护（可选 --with-daemon）
if [ "$WITH_DAEMON" = "1" ]; then
  if pgrep -f "guard_all\.py" >/dev/null 2>&1; then
    say "守护已在运行，跳过启动"
  else
    say "启动常驻看门狗 guard_all.py..."
    set -a; . "$TASKHUB_HOME/taskhub.env"; set +a
    ( cd "$TASKHUB_HOME" && nohup python3 -u guard_all.py </dev/null >/dev/null 2>&1 & )
    sleep 2
    pgrep -f "guard_all\.py" >/dev/null 2>&1 && say "guard 已拉起（worker 由其自动接管）" || say "!! guard 未见进程，查看 $TASKHUB_HOME/guard.log"
  fi
  # crontab 保活（容器/无权限时自动跳过）
  if (crontab -l 2>/dev/null || true; echo "# probe") | crontab - >/dev/null 2>&1; then
    ( { crontab -l 2>/dev/null | grep -v "keepalive.sh" || true; } ; echo "* * * * * $TASKHUB_HOME/keepalive.sh >/dev/null 2>&1" ) | crontab - \
      && say "crontab 保活已注册（每分钟巡检）" \
      || say "crontab 写入失败，跳过保活注册"
  else
    say "crontab 不可用，跳过保活注册（可用 systemd 或手动保活）"
  fi
fi

# 6) 自检（单轮认领引擎，不动常驻进程）
say "自检：TASKHUB_ONCE=1 单轮运行..."
set -a; . "$TASKHUB_HOME/taskhub.env"; set +a
if ( cd "$TASKHUB_HOME" && TASKHUB_ONCE=1 timeout 60 python3 worker_all.py ); then
  say "自检通过 ✓"
else
  say "!! 自检异常：检查 credentials.env 令牌与网络后重试"
fi

say "完成。skill: $SKILL_DIR | 运行: $TASKHUB_HOME"
[ "$WITH_DAEMON" = "1" ] || say "未启动守护。需要时: set -a; . $TASKHUB_HOME/taskhub.env; set +a; nohup python3 -u $TASKHUB_HOME/guard_all.py &"
