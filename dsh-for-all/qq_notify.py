#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qq_notify.py —— QQ 汇报发送器（TaskHub 看板通用）

凭据来源（按序）：
  1) 环境变量 QQBOT_APP_ID / QQBOT_APP_SECRET
  2) ~/.dsh/.env（appId）
  3) ~/.dsh/.credentials.yaml（appSecret，与 DSH qqbot 插件同源）

调 QQ 开放平台机器人 API，把消息发到 ~/.dsh/qqbot/*.targets.json 里登记的所有目标
（私聊 c2c + 群 group）。

用法:
  python3 qq_notify.py "要发送的文本"
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = os.path.expanduser("~/.dsh/.env")
CREDS_YAML = os.path.expanduser("~/.dsh/.credentials.yaml")
TARGETS_DIR = os.path.expanduser("~/.dsh/qqbot")


def _parse_kv(path, sep="="):
    d = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and sep in line and not line.startswith("#"):
                    k, v = line.split(sep, 1)
                    k = k.strip()
                    if k and not k.startswith((" ", "-", "{")):
                        d[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return d


def load_credentials():
    d = {}
    d.update(_parse_kv(ENV_PATH, "="))       # .env
    d.update(_parse_kv(CREDS_YAML, ":"))     # .credentials.yaml（secret 主要在这）
    d.update({k: v for k, v in os.environ.items() if k.startswith("QQBOT_")})
    return d


def api(url, payload, headers=None, timeout=20):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def get_token(env):
    app_id = env.get("QQBOT_APP_ID", "")
    secret = env.get("QQBOT_APP_SECRET", "")
    if not app_id or not secret:
        return None, "缺少 QQBOT_APP_ID / QQBOT_APP_SECRET"
    st, body = api("https://bots.qq.com/app/getAppAccessToken",
                   {"appId": app_id, "clientSecret": secret})
    if st == 200:
        try:
            return json.loads(body).get("access_token"), None
        except ValueError:
            return None, body[:200]
    return None, "HTTP %s %s" % (st, body[:200])


def send(token, target_id, scope, text):
    base = "https://api.sgroup.qq.com/v2"
    path = ("users/%s/messages" if scope == "c2c" else "groups/%s/messages") % urllib.parse.quote(target_id)
    # 主动消息：不要带 msg_id（伪造 msg_id 会 400 "msg_id无效或越权"）；msg_seq 保留即可
    payload = {"msg_type": 0, "content": text, "msg_seq": 1}
    return api(base + "/" + path, payload, {"Authorization": "QQBot " + token})


def load_targets():
    out = {}
    try:
        for fn in os.listdir(TARGETS_DIR):
            if fn.endswith(".targets.json"):
                with open(os.path.join(TARGETS_DIR, fn), encoding="utf-8") as f:
                    data = json.load(f)
                for key, t in (data.get("targets") or {}).items():
                    out[key] = t
    except OSError:
        pass
    return out


def main():
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    if not text:
        print("用法: qq_notify.py <文本>", file=sys.stderr)
        return 1
    env = load_credentials()
    token, err = get_token(env)
    if not token:
        print("QQ 令牌获取失败: %s" % err, file=sys.stderr)
        return 2
    targets = load_targets()
    if not targets:
        print("没有找到 QQ 发送目标（~/.dsh/qqbot/*.targets.json）", file=sys.stderr)
        return 3
    ok = 0
    for key, t in targets.items():
        st, body = send(token, t["targetId"], t["scope"], text)
        note = ""
        if st == 400 and "40034105" in body:
            note = "（群主动推送无权限，QQ 平台限制）"
        print("-> %s(%s...) HTTP %s %s%s" % (t["scope"], t["targetId"][:8], st, body[:80], note))
        if st in (200, 201):
            ok += 1
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
