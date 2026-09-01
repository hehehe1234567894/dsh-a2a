#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
executor_web.py —— TaskHub 执行器：通过 DSH web API 派生执行会话（web 侧边栏可见）

替代旧的 `dsh --profile headless` 子进程派生方式：
  - 认领任务后，调用本地 DSH web 的 RPC API 创建一个会话并发送任务 prompt，
    执行过程在 web 侧边栏实时可见（标题形如 "[taskhub-exec:#54] ..."）；
  - 执行器会话跑在 DSH 宿主进程内（无 landlock 沙箱），不再有
    "无法写 ~/.dsh/profiles/headless/cordis.yml" 的 EACCES 崩溃；
  - worker 侧只需 HTTP 轮询会话状态，不 spawn 任何子进程。

API 协议（dsh-host-apiproxy）：
  POST {base}/api/session.create   {"type":"client-request","rpcId":"...","method":"session.create","payload":{...}}
  POST {base}/api/session.prompt   payload: {sessionId, mode:"queue", content:[{type:"text",text:...}]}
  POST {base}/api/session.list     payload: {}  → items[{sessionId, running, projections.values.title}]

环境变量（均可选）：
  TASKHUB_WEB_API   默认 http://127.0.0.1:3080 （DSH web 地址）
"""
import json
import os
import time
import urllib.request

BASE = os.environ.get("TASKHUB_WEB_API", "http://127.0.0.1:3080")


def rpc(method, payload, base=None, timeout=15):
    url = (base or BASE) + "/api/" + method
    body = json.dumps({
        "type": "client-request",
        "rpcId": "r-%d-%d" % (int(time.time() * 1000), os.getpid()),
        "method": method,
        "payload": payload,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    res = d.get("result") or {}
    if not res.get("ok"):
        err = res.get("error") or {}
        raise RuntimeError("%s: %s %s" % (method, err.get("code", "?"), err.get("message", res)))
    return res.get("value")


def api_available(base=None):
    """探测 DSH web API 是否可达。"""
    try:
        rpc("session.list", {}, base=base, timeout=5)
        return True
    except Exception:
        return False


def create_session(cwd=None, session_id=None, base=None, **extra):
    """创建会话。session_id 可指定（幂等自愈：同名会话存在则返回既有）；extra 透传
    其余 payload 字段（如 agentPreset，§10.1 执行空间预配映射）。"""
    payload = {}
    if cwd:
        payload["cwd"] = cwd
    if session_id:
        payload["sessionId"] = session_id
    payload.update(extra)
    v = rpc("session.create", payload, base=base)
    return v.get("sessionId")


def prompt(session_id, text, base=None):
    """向会话发送任务 prompt（queue 模式：不打断其他输入）。"""
    return rpc("session.prompt", {
        "sessionId": session_id,
        "mode": "queue",
        "content": [{"type": "text", "text": text}],
    }, base=base)


def cancel(session_id, base=None):
    """打断会话当前正在执行的轮次（§10.1 紧急插队用）。"""
    return rpc("session.cancel", {"sessionId": session_id}, base=base)


def session_status(session_id, base=None):
    """返回 (running: bool, title: str|None, updatedAt: int|None)。会话不存在 → (False, None, None)。"""
    try:
        items = (rpc("session.list", {}, base=base) or {}).get("items", [])
    except Exception:
        return False, None, None
    for s in items:
        if s.get("sessionId") == session_id:
            vals = (s.get("projections") or {}).get("values") or {}
            return (bool(s.get("running")), vals.get("title"), s.get("updatedAt"))
    return False, None, None


def session_exists(session_id, base=None):
    """三态存在性：True=会话在列（无论 running/title）；False=确认不存在；
    None=查询失败（网络/API 异常，调用方不得据此判死）。"""
    try:
        items = (rpc("session.list", {}, base=base) or {}).get("items", [])
    except Exception:
        return None
    return any(s.get("sessionId") == session_id for s in items)


def models(session_id, base=None):
    """会话可用模型目录：{current, groups:[{id,name,models:[{id,name,...}]}], failures}。"""
    return rpc("session.models", {"sessionId": session_id}, base=base) or {}


def select_model(session_id, provider, model, base=None):
    """切换会话模型（§10.1 任务级『模型：』声明）。"""
    return rpc("session.selectModel", {"sessionId": session_id,
                                       "provider": provider, "model": model}, base=base)


if __name__ == "__main__":
    # 自检：列出 web 上的会话
    print("web api:", BASE)
    print("available:", api_available())
    items = rpc("session.list", {}).get("items", [])
    print("会话数:", len(items))
    for s in items[:10]:
        vals = (s.get("projections") or {}).get("values") or {}
        print(" -", s.get("sessionId", "")[:24], "| running:", s.get("running"),
              "|", (vals.get("title") or "")[:40])
