#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
board.py — TaskHub 跨机器 Agent 任务黑板客户端（GitHub Issues 版）

零依赖：仅用 Python 标准库。把本文件拷到任何有 python3 的机器即可接入看板。

看板模型：
  任务   = Issue（标题=任务名，正文=给 agent 的完整指令/上下文/验收标准）
  状态   = 标签  pending(待领) -> claimed(进行中) -> done / failed / cancelled
  认领人 = 标签 claimed-by-<worker>（便于人看；真正的锁在评论里）
  认领锁 = 评论  __CLAIM_BY__ <worker> lease=<RFC3339到期时间> claim_id=<uuid>
  结果   = 评论  __RESULT__ <结果说明>

防抢占协议（核心，与版本一 task_board.py 契合）：
  GitHub 评论 ID 全局单调递增，天然全序。任何客户端按同一规则重算持有者：
    1. 按评论顺序扫描认领评论，维护 current_owner；
    2. 新认领评论只有在前一任的租约已过期时才生效，否则视为无效抢注；
    3. 无租约字段（v1 旧格式认领）= 永久租约，永不被接管（v1 语义完全保留）；
    4. 最终持有者 = 最后一条生效认领，且其租约仍未过期（v1 无租约视为永久有效）。
  认领流程：发认领评论 -> 回读重算 -> 我持有则换标签，否则删除自己的评论（无痕回滚）。
  长任务定期 heartbeat 续租；agent 崩溃后租约到期，任务自动回到可领状态。

配置（按顺序查找令牌）：
  --token > 环境变量 GITHUB_TOKEN > --credentials 文件 > ~/.taskhub/credentials.env > 脚本同目录 credentials.env
  credentials.env 内容一行：GITHUB_TOKEN=github_pat_xxx

用法示例：
  python3 board.py list
  python3 board.py create --title "抓取xx数据" --body "目标/上下文/验收标准" --priority P1
  python3 board.py claim --worker agent-pc-1
  python3 board.py heartbeat --issue 12 --worker agent-pc-1
  python3 board.py complete --issue 12 --worker agent-pc-1 --result "已完成，结果..."
  python3 board.py fail --issue 12 --worker agent-pc-1 --error "原因..."
  python3 board.py selftest    # 端到端自检
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# Windows 控制台默认 GBK 会破坏 emoji/中文输出，统一切成 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

API = "https://api.github.com"
API_VERSION = "2022-11-28"
CLAIM_PREFIX = "__CLAIM_BY__ "
RESULT_PREFIX = "__RESULT__ "
STATUS_LABELS = ("pending", "claimed", "done", "failed", "cancelled")
PRIORITIES = {"P0": 0, "P1": 1, "P2": 2}
DEFAULT_REPO = "hehehe1234567894/agent-tasks"
DEFAULT_LEASE_MIN = 30
PER_PAGE = 100
MIN_DT = datetime.min.replace(tzinfo=timezone.utc)
FOREVER_DT = datetime.max.replace(tzinfo=timezone.utc)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def die(msg, code=1):
    print("错误: %s" % msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- 配置

def load_credentials_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def resolve_token(args):
    if getattr(args, "token", None):
        return args.token
    env = os.environ.get("GITHUB_TOKEN")
    if env:
        return env.strip()
    if getattr(args, "credentials", None):
        token = load_credentials_file(args.credentials)
        if token:
            return token
        die("凭据文件中没有 GITHUB_TOKEN: %s" % args.credentials)
    candidates = [os.path.expanduser("~/.taskhub/credentials.env"),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.env")]
    for p in candidates:
        token = load_credentials_file(p)
        if token:
            return token
    die("未找到 GitHub 令牌：设 GITHUB_TOKEN 环境变量，或在 ~/.taskhub/credentials.env 写入 GITHUB_TOKEN=...")


def state_path_for(args):
    if getattr(args, "state", None):
        return args.state
    if os.environ.get("TASKHUB_STATE"):
        return os.environ["TASKHUB_STATE"]
    cred = getattr(args, "credentials", None) or os.environ.get("TASKHUB_CREDENTIALS")
    if cred and os.path.exists(cred):
        return os.path.join(os.path.dirname(os.path.abspath(cred)), "claims.json")
    if os.path.exists(os.path.expanduser("~/.taskhub/credentials.env")):
        return os.path.expanduser("~/.taskhub/claims.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "claims.json")


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, data):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print("警告: 状态文件写入失败(%s): %s" % (path, e), file=sys.stderr)


def record_claim(state_path, repo, number, entry):
    data = load_state(state_path)
    data.setdefault(repo, {})[str(number)] = entry
    save_state(state_path, data)


def drop_claim(state_path, repo, number):
    data = load_state(state_path)
    repo_map = data.get(repo, {})
    if str(number) in repo_map:
        del repo_map[str(number)]
        save_state(state_path, data)


# ---------------------------------------------------------------- GitHub API

class GitHub:
    def __init__(self, token, repo):
        if "/" not in repo:
            die("repo 需为 owner/name 形式，当前: %s" % repo)
        self.token = token
        self.repo = repo
        self.base = "%s/repos/%s" % (API, repo)

    def request(self, method, path, body=None, retries=None, tolerant_404=False, soft=False):
        url = path if path.startswith("http") else self.base + path
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "taskhub-board/1.0",
            "Authorization": "Bearer " + self.token,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if retries is None:
            retries = 2 if method == "GET" else 0
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {"_status": resp.status}
            except urllib.error.HTTPError as e:
                if e.code == 404 and tolerant_404:
                    return None
                raw = ""
                try:
                    raw = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if e.code == 403 and e.headers.get("Retry-After") and attempt < 2:
                    time.sleep(float(e.headers["Retry-After"]) + 1)
                    continue
                if e.code in (500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                if soft:
                    return None
                hint = ""
                if e.code == 401:
                    hint = "（令牌无效或已过期）"
                elif e.code == 403:
                    hint = "（令牌权限不足，需该仓库 Issues 读写权限）"
                elif e.code == 404:
                    hint = "（资源不存在或无权限）"
                die("GitHub API %s %s -> HTTP %d %s%s" % (method, path, e.code, raw[:300], hint))
            except urllib.error.URLError as e:
                if attempt < retries:
                    time.sleep(2)
                    continue
                if soft:
                    return None
                die("网络错误: %s" % e)
        die("请求失败: %s" % path)

    # --- 封装 ---
    def issue(self, number):
        return self.request("GET", "/issues/%d" % number)

    def issues(self, state="open", labels=None):
        q = "?state=%s&per_page=%d&sort=created&direction=asc" % (state, PER_PAGE)
        if labels:
            q += "&labels=" + urllib.parse.quote(",".join(labels))
        return self.request("GET", "/issues" + q) or []

    def comments(self, number):
        return self.request("GET", "/issues/%d/comments?per_page=%d" % (number, PER_PAGE)) or []

    def add_comment(self, number, body):
        return self.request("POST", "/issues/%d/comments" % number, {"body": body})

    def edit_comment(self, comment_id, body):
        return self.request("PATCH", "/issues/comments/%d" % comment_id, {"body": body})

    def del_comment(self, comment_id):
        """成功返回 truthy；404/无权限等失败返回 None（调用方据此走回退路径）。"""
        return self.request("DELETE", "/issues/comments/%d" % comment_id, soft=True)

    def edit_issue(self, number, **fields):
        return self.request("PATCH", "/issues/%d" % number, fields)

    def add_labels(self, number, names):
        return self.request("POST", "/issues/%d/labels" % number, {"labels": names})

    def remove_label(self, number, name):
        return self.request("DELETE", "/issues/%d/labels/%s" % (number, urllib.parse.quote(name)),
                            tolerant_404=True)


def transition(gh, number, add, remove):
    for name in remove:
        gh.remove_label(number, name)
    if add:
        gh.add_labels(number, add)


# ---------------------------------------------------------------- 协议核心

def parse_claims(comments):
    """从评论列表解析认领评论。"""
    out = []
    for c in comments:
        body = c.get("body") or ""
        if not body.startswith(CLAIM_PREFIX):
            continue
        fields = body[len(CLAIM_PREFIX):].split()
        worker = fields[0] if fields else "?"
        lease, cid = None, None
        for f in fields[1:]:
            if f.startswith("lease="):
                lease = parse_iso(f[6:])
            elif f.startswith("claim_id="):
                cid = f[9:]
        out.append({
            "id": c["id"],
            "worker": worker,
            "lease": lease,
            "claim_id": cid,
            "created": parse_iso(c.get("created_at")) or MIN_DT,
        })
    return out


def lease_dt(claim):
    """无租约字段（版本一旧格式）视为永久租约 —— 与 v1"第一条认领永久获胜"语义契合：
    v1 认领永不被 v2 接管，杜绝过渡期双重执行；崩溃自动接管只发生在带租约的 v2 认领之间。"""
    return claim["lease"] or FOREVER_DT


def resolve_owner(claims, now_dt):
    """防抢占核心规则：按顺序重算唯一持有者。"""
    owner = None
    for c in sorted(claims, key=lambda x: (x["created"], x["id"])):
        if owner is None or lease_dt(owner) < c["created"]:
            owner = c
    if owner is None or lease_dt(owner) <= now_dt:
        return None
    return owner


def claim_body(worker, lease_until, cid):
    return "%s%s lease=%s claim_id=%s" % (CLAIM_PREFIX, worker, lease_until, cid)


def try_claim(gh, issue, worker, lease_min, state_path=None, repo=None):
    """对单个任务尝试认领；失败自动无痕回滚并返回 None。"""
    number = issue["number"]
    cid = uuid.uuid4().hex
    lease_until = iso(now_utc() + timedelta(minutes=lease_min))
    comment = gh.add_comment(number, claim_body(worker, lease_until, cid))
    claims = parse_claims(gh.comments(number))
    owner = resolve_owner(claims, now_utc())
    if owner and owner["claim_id"] == cid:
        transition(gh, number, add=["claimed", "claimed-by-" + worker], remove=["pending"])
        entry = {"worker": worker, "claim_id": cid, "comment_id": comment["id"], "lease": lease_until}
        if state_path and repo:
            record_claim(state_path, repo, number, entry)
        return {
            "issue": number,
            "worker": worker,
            "title": issue.get("title") or "",
            "body": issue.get("body") or "",
            "url": issue.get("html_url") or "",
            "lease_until": lease_until,
            "claim_id": cid,
        }
    # 抢注失败：删除自己的评论，无痕回滚
    gh.del_comment(comment["id"])
    return None


def find_my_owner(gh, number, worker, claim_id=None):
    claims = parse_claims(gh.comments(number))
    owner = resolve_owner(claims, now_utc())
    if owner is None:
        return None, "当前无有效持有者（租约已过期或无人认领）"
    if claim_id and owner["claim_id"] == claim_id:
        return owner, None
    if owner["worker"] == worker:
        return owner, None
    return None, "当前持有者是 %s（租约至 %s），不是 %s" % (
        owner["worker"], iso(lease_dt(owner)), worker)


def parse_eligibility(iss, worker):
    """按 AGENTS.md 第 3 节解析任务正文的资格声明。

    返回 (eligible:bool, reason:str)。
      careful：专属任务、父组任务、无资格声明都视作不可领（宁缺毋滥）。
    """
    title = iss.get("title") or ""
    body = iss.get("body") or ""
    # 公告/非任务
    if title.strip().startswith("[公告]") or title.strip().startswith("[公告"):
        return False, "公告，非任务"
    if "documentation" in [l["name"] for l in iss.get("labels", [])]:
        return False, "documentation 公告"
    # 无资格行
    if "资格" not in body:
        return False, "无资格声明"
    # 解析资格行
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("资格"):
            continue
        if "通用" in line:
            return True, "通用任务"
        # 专属 X
        m = re.search(r"专属\s*[：:]\s*(\S+)", line) or re.search(r"专属\s+(\S+)", line)
        if m:
            target = m.group(1).strip()
            return worker == target, "专属任务(%s)" % target
        # 父 <n>
        m2 = re.search(r"父\s*[：:]\s*(\d+)", line) or re.search(r"父\s+(\d+)", line)
        if m2:
            return False, "父组子任务，需先认领父任务%s" % m2.group(1)
        return False, "无法识别的资格声明"
    return False, "无资格行（解析失败）"


def my_active_count(gh, worker):
    """统计 worker 名下进行中任务数（第 10 节：已认领但未 done/fail/cancel/release）。

    判定：state=open 且 label 含 claimed-by-<worker>。
    """
    active = 0
    for i in gh.issues(state="open"):
        if "pull_request" in i:
            continue
        lb = {l["name"] for l in i.get("labels", [])}
        if "claimed-by-" + worker in lb:
            active += 1
    return active


def do_claim(gh, args, worker, count, lease_min, tags):
    issues = [i for i in gh.issues(state="open", labels=["pending"] + tags)
              if "pull_request" not in i]
    issues.sort(key=lambda x: (min(PRIORITIES.get(l, 9) for l in
                                   [lb["name"] for lb in x["labels"]] or [9]),
                               x.get("created_at") or "", x["number"]))
    # ── 第 10 节：自我认知 / 容量控制 ────────────────────────────
    max_load = int(getattr(args, "max_load", 0) or os.environ.get("TASKHUB_MAX_LOAD", "1"))
    active = my_active_count(gh, worker)
    slots = max_load - active  # 还能接几个通用任务
    # ── 第 11 节：退避计数（调用方传入 args.fail_since 或由 worker 管理）──
    claimed = []
    skip_reasons = []
    for iss in issues:
        if len(claimed) >= count:
            break
        # 第 3 节：按文本解析资格（专属不等于我的，绝不抢）
        eligible, reason = parse_eligibility(iss, worker)
        if not eligible:
            skip_reasons.append("#%s 跳过(%s)" % (iss["number"], reason))
            continue
        # 第 10 节：通用任务受容量限制
        lb = [l["name"] for l in iss.get("labels", [])]
        if slots <= 0 and not any(lb.startswith("for:") for l in lb):
            skip_reasons.append("#%s 跳过(超出MAX_LOAD=%s)" % (iss["number"], max_load))
            continue
        got = try_claim(gh, iss, worker, lease_min, args and state_path_for(args), gh.repo)
        if got:
            claimed.append(got)
            slots -= 1
        else:
            skip_reasons.append("#%s 抢注失败" % iss["number"])
    if skip_reasons:
        print("（资格/容量过滤：%s）" % "; ".join(skip_reasons[:6]))
    return claimed


def do_heartbeat(gh, number, worker, lease_min, state_path=None, repo=None):
    entry = (load_state(state_path).get(repo, {}).get(str(number)) if state_path and repo else None)
    owner, err = find_my_owner(gh, number, worker, entry and entry.get("claim_id"))
    if owner is None:
        die("无法续租 #%d：%s" % (number, err), code=3)
    new_lease = iso(now_utc() + timedelta(minutes=lease_min))
    gh.edit_comment(owner["id"], claim_body(owner["worker"], new_lease, owner["claim_id"] or "unknown"))
    if state_path and repo:
        record_claim(state_path, repo, number,
                     {"worker": worker, "claim_id": owner["claim_id"],
                      "comment_id": owner["id"], "lease": new_lease})
    return new_lease


def do_complete(gh, number, worker, result, force=False, state_path=None, repo=None):
    if not force:
        entry = (load_state(state_path).get(repo, {}).get(str(number)) if state_path and repo else None)
        owner, err = find_my_owner(gh, number, worker, entry and entry.get("claim_id"))
        if owner is None:
            die("无法完成 #%d：%s（如确需强制，请加 --force）" % (number, err), code=3)
    gh.add_comment(number, RESULT_PREFIX + result)
    transition(gh, number, add=["done"], remove=["claimed", "pending", "claimed-by-" + worker])
    gh.edit_issue(number, state="closed")
    if state_path and repo:
        drop_claim(state_path, repo, number)


def do_fail(gh, number, worker, error, force=False, state_path=None, repo=None):
    if not force:
        entry = (load_state(state_path).get(repo, {}).get(str(number)) if state_path and repo else None)
        owner, err = find_my_owner(gh, number, worker, entry and entry.get("claim_id"))
        if owner is None:
            die("无法标记失败 #%d：%s（如确需强制，请加 --force）" % (number, err), code=3)
    gh.add_comment(number, RESULT_PREFIX + "FAIL: " + error)
    transition(gh, number, add=["failed"], remove=["claimed", "pending", "claimed-by-" + worker])
    gh.edit_issue(number, state="closed")
    if state_path and repo:
        drop_claim(state_path, repo, number)


def do_release(gh, number, worker, force=False, state_path=None, repo=None):
    entry = (load_state(state_path).get(repo, {}).get(str(number)) if state_path and repo else None)
    owner, err = (find_my_owner(gh, number, worker, entry and entry.get("claim_id"))
                  if not force else (None, None))
    if not force and owner is None:
        die("无法释放 #%d：%s（如确需强制，请加 --force）" % (number, err), code=3)
    target = owner or entry
    if target and target.get("id"):
        # 优先删除认领评论；删不掉则把租约改成过去（等效释放）
        if gh.del_comment(target["id"]) is None:
            gh.edit_comment(target["id"], claim_body(worker, iso(MIN_DT), target.get("claim_id") or "unknown"))
    transition(gh, number, add=["pending"], remove=["claimed", "claimed-by-" + worker])
    if state_path and repo:
        drop_claim(state_path, repo, number)


def do_create(gh, title, body, priority, tags, count=1):
    created = []
    for i in range(count):
        t = title if count == 1 else "%s (%d/%d)" % (title, i + 1, count)
        iss = gh.request("POST", "/issues", {
            "title": t, "body": body or "",
            "labels": ["pending", priority] + list(tags),
        })
        created.append(iss)
    return created


def classify(gh, iss, now_dt):
    labels = {l["name"] for l in iss.get("labels", [])}
    if iss.get("state") == "closed":
        for s in ("done", "failed", "cancelled"):
            if s in labels:
                return s, None
        return "closed", None
    owner = resolve_owner(parse_claims(gh.comments(iss["number"])), now_dt)
    if owner:
        return "claimed", owner
    return "pending", None


def cmd_list(args):
    gh = GitHub(resolve_token(args), args.repo)
    state = "all" if args.status in ("all", "done", "failed", "cancelled", "closed") else "open"
    issues = [i for i in gh.issues(state=state) if "pull_request" not in i]
    now_dt = now_utc()
    rows = []
    for iss in issues:
        status, owner = classify(gh, iss, now_dt)
        keep = (args.status == "all"
                or (args.status == "open" and status in ("pending", "claimed"))
                or status == args.status)
        if not keep:
            continue
        pri = min([PRIORITIES.get(lb["name"], 9) for lb in iss.get("labels", [])] or [9])
        lease_left_min = None
        if owner and owner.get("lease"):
            lease_left_min = max(0, int((lease_dt(owner) - now_dt).total_seconds() // 60))
        rows.append({
            "issue": iss["number"], "status": status,
            "worker": owner["worker"] if owner else "",
            "lease_until": iso(lease_dt(owner)) if owner and owner.get("lease") else "",
            "lease_left_min": lease_left_min,
            "priority": "P%s" % pri if pri < 9 else "-",
            "title": iss.get("title") or "", "url": iss.get("html_url") or "",
        })
    rows.sort(key=lambda r: (PRIORITIES.get(r["priority"], 9), r["issue"]))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("（看板上没有匹配的任务）")
        return
    ICON = {"pending": "⏳ 待领", "claimed": "🔧 进行", "done": "✅ 完成",
            "failed": "❌ 失败", "cancelled": "🚫 取消", "closed": "🔒 关闭"}
    print("📋 任务看板 %s（共 %d 个）" % (args.repo, len(rows)))
    for r in rows:
        lease_left = "-"
        if r.get("lease_left_min") is not None:
            m = r["lease_left_min"]
            lease_left = ("%d 分钟" % m) if m >= 1 else "即将到期"
        print("  %s  #%-4d  %s  %-12s  租约:%-8s  %s" % (
            ICON.get(r["status"], r["status"]), r["issue"], r["priority"],
            r["worker"] or "-", lease_left, r["title"][:44]))


def cmd_show(args):
    gh = GitHub(resolve_token(args), args.repo)
    iss = gh.issue(args.issue)
    LINE = "═" * 50
    print(LINE)
    print("#%s [%s] %s" % (iss["number"], iss["state"], iss["title"]))
    print("标签: %s" % " ".join(l["name"] for l in iss.get("labels", [])))
    print("链接: %s" % iss.get("html_url"))
    print(LINE)
    print("📜 任务正文:")
    body = iss.get("body") or "(空)"
    for line in body.splitlines() or ["(空)"]:
        print("  " + line)
    comments = gh.comments(args.issue)
    now_dt = now_utc()
    print(LINE)
    print("💬 协作对话（%d 条，按时间正序）:" % len(comments))
    if not comments:
        print("  （还没有对话）")
    for c in comments:
        b = c.get("body") or ""
        t = c["created_at"].replace("T", " ")[:19]
        who = c["user"]["login"]
        if b.startswith(CLAIM_PREFIX):
            cl = parse_claims([c])[0]
            if cl["lease"]:
                state = "【当前有效持有】" if lease_dt(cl) > now_dt else "【租约已过期】"
                print("  ── 🙋 认领  %s  worker=%s  租约至=%s %s" % (t, cl["worker"], iso(cl["lease"]), state))
            else:
                print("  ── 🙋 认领  %s  worker=%s  无租约(v1 永久认领)" % (t, cl["worker"]))
        elif b.startswith(RESULT_PREFIX):
            print("  ── 🏁 结果  %s  [%s]" % (t, who))
            for line in b[len(RESULT_PREFIX):].splitlines() or ["(空)"]:
                print("      " + line)
        else:
            print("  ── 💬 发言  %s  [%s]" % (t, who))
            for line in b.splitlines() or ["(空)"]:
                print("      " + line)
    print(LINE)


def cmd_create(args):
    gh = GitHub(resolve_token(args), args.repo)
    created = do_create(gh, args.title, args.body, args.priority, args.tag, args.count)
    if args.json:
        print(json.dumps([{"issue": i["number"], "title": i["title"], "url": i.get("html_url")}
                          for i in created], ensure_ascii=False, indent=2))
        return
    for iss in created:
        print("已创建 #%s %s %s" % (iss["number"], iss["title"], iss.get("html_url", "")))


def cmd_claim(args):
    gh = GitHub(resolve_token(args), args.repo)
    claimed = do_claim(gh, args, args.worker, args.count, args.lease_min, args.tag)
    if args.json:
        print(json.dumps({"claimed": claimed}, ensure_ascii=False, indent=2))
        return
    if not claimed:
        print("（当前没有可认领的任务）")
        return
    LINE = "─" * 46
    for c in claimed:
        print(LINE)
        print("🙋 已认领 #%s  %s" % (c["issue"], c["title"]))
        print("   认领人: %s    租约至: %s" % (c["worker"], c["lease_until"]))
        print("   链接  : %s" % c["url"])
        if c["body"]:
            print("   ── 任务正文 ──")
            for line in c["body"].splitlines():
                print("   " + line)
    print(LINE)
    print("下一步: 长任务先 heartbeat 续租；完成后 complete --issue %s --worker %s --result \"结果\"" % (
        claimed[0]["issue"], claimed[0]["worker"]))


def cmd_heartbeat(args):
    gh = GitHub(resolve_token(args), args.repo)
    new_lease = do_heartbeat(gh, args.issue, args.worker, args.lease_min,
                             state_path_for(args), args.repo)
    print("✓ #%s 租约已续至 %s" % (args.issue, new_lease))


def cmd_complete(args):
    gh = GitHub(resolve_token(args), args.repo)
    do_complete(gh, args.issue, args.worker, args.result, args.force,
                state_path_for(args), args.repo)
    print("✓ #%s 已完成并关闭。" % args.issue)


def cmd_fail(args):
    gh = GitHub(resolve_token(args), args.repo)
    do_fail(gh, args.issue, args.worker, args.error, args.force,
            state_path_for(args), args.repo)
    print("✓ #%s 已标记失败并关闭。可用 reopen 重新打开。" % args.issue)


def cmd_release(args):
    gh = GitHub(resolve_token(args), args.repo)
    do_release(gh, args.issue, args.worker, args.force, state_path_for(args), args.repo)
    print("✓ #%s 已释放回 pending。" % args.issue)


def cmd_cancel(args):
    gh = GitHub(resolve_token(args), args.repo)
    transition(gh, args.issue, add=["cancelled"], remove=["pending", "claimed"])
    gh.edit_issue(args.issue, state="closed")
    print("✓ #%s 已取消并关闭。" % args.issue)


def cmd_reopen(args):
    gh = GitHub(resolve_token(args), args.repo)
    transition(gh, args.issue, add=["pending"], remove=["done", "failed", "cancelled"])
    gh.edit_issue(args.issue, state="open")
    print("✓ #%s 已重新打开，状态 pending。" % args.issue)


# ---------------------------------------------------------------- 自检

def cmd_selftest(args):
    gh = GitHub(resolve_token(args), args.repo)
    stamp = now_utc().strftime("%m%d%H%M%S")
    results = []

    def check(name, ok, extra=""):
        results.append(ok)
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" | " + extra) if extra else ""))
        return ok

    print("== TaskHub 端到端自检（%s）==" % stamp)
    t1, t2, t3 = [do_create(gh, "[selftest] %s %s" % (name, stamp),
                            "selftest 自动任务，可忽略", "P2", [], 1)[0]
                  for name in ("并发认领A", "全链路B", "并发抢注C")]

    # 1 顺序认领
    c1 = try_claim(gh, t1, "selftest-w1", DEFAULT_LEASE_MIN)
    c2 = try_claim(gh, t2, "selftest-w2", DEFAULT_LEASE_MIN)
    check("顺序认领", bool(c1) and bool(c2))
    labels1 = {l["name"] for l in gh.issue(t1["number"])["labels"]}
    check("认领后标签正确", "claimed" in labels1 and "pending" not in labels1
          and "claimed-by-selftest-w1" in labels1)

    # 2 并发抢注同一任务：必须唯一胜者，失败者无痕回滚
    outcomes = []
    def _run(worker):
        outcomes.append(try_claim(gh, t3, worker, DEFAULT_LEASE_MIN))
    th1 = threading.Thread(target=_run, args=("selftest-w1",))
    th2 = threading.Thread(target=_run, args=("selftest-w2",))
    th1.start(); th2.start(); th1.join(); th2.join()
    winners = [o for o in outcomes if o]
    check("并发认领唯一胜者", len(winners) == 1, "胜者=%s" % (winners[0]["worker"] if winners else "无"))
    check("失败者评论已回滚", len(parse_claims(gh.comments(t3["number"]))) == 1)

    # 3 租约未到期不可被抢
    steal = try_claim(gh, t3, "selftest-w2", DEFAULT_LEASE_MIN)
    check("租约期内他人不可抢", steal is None)
    owner_now = resolve_owner(parse_claims(gh.comments(t3["number"])), now_utc())
    check("持有者未被篡改", bool(owner_now) and winners and owner_now["worker"] == winners[0]["worker"])

    # 3.5 v1 兼容：旧格式认领（无 lease）= 永久持有，v2 不可接管
    t4 = do_create(gh, "[selftest] v1兼容 %s" % stamp, "旧格式认领兼容验证", "P2", [], 1)[0]
    gh.add_comment(t4["number"], CLAIM_PREFIX + "v1-worker")
    check("v1旧格式任务不可被v2抢", try_claim(gh, t4, "selftest-w1", DEFAULT_LEASE_MIN) is None)
    o4 = resolve_owner(parse_claims(gh.comments(t4["number"])), now_utc())
    check("v1任务归属判定正确", bool(o4) and o4["worker"] == "v1-worker")

    # 4 心跳续租
    before = owner_now["lease"] if owner_now else None
    new_lease = do_heartbeat(gh, t3["number"], winners[0]["worker"], DEFAULT_LEASE_MIN)
    check("心跳续租", bool(before) and parse_iso(new_lease) > before, "%s -> %s" % (iso(before), new_lease))

    # 5 完成 / 失败 / 重新打开 / 取消
    do_complete(gh, t3["number"], winners[0]["worker"], "selftest 完成", state_path=None)
    iss3 = gh.issue(t3["number"])
    check("complete 闭环", iss3["state"] == "closed"
          and "done" in {l["name"] for l in iss3["labels"]}
          and any((c.get("body") or "").startswith(RESULT_PREFIX) for c in gh.comments(t3["number"])))
    do_fail(gh, t2["number"], "selftest-w2", "模拟失败", state_path=None)
    iss2 = gh.issue(t2["number"])
    check("fail 闭环", iss2["state"] == "closed" and "failed" in {l["name"] for l in iss2["labels"]})
    gh.edit_issue(t2["number"], state="open")
    transition(gh, t2["number"], add=["pending"], remove=["failed"])
    cmd_cancel(argparse.Namespace(issue=t2["number"], repo=gh.repo, token=resolve_token(args)))
    iss2b = gh.issue(t2["number"])
    check("reopen/cancel", iss2b["state"] == "closed"
          and "cancelled" in {l["name"] for l in iss2b["labels"]})
    do_complete(gh, t1["number"], "selftest-w1", "selftest 完成", force=True, state_path=None)
    do_complete(gh, t4["number"], "v1-worker", "selftest 完成", force=True, state_path=None)

    total, passed = len(results), sum(1 for r in results if r)
    print("== 自检结果: %d/%d 通过 ==" % (passed, total))
    sys.exit(0 if passed == total else 1)


# ---------------------------------------------------------------- CLI

def global_flags(p):
    p.add_argument("--repo", default=os.environ.get("TASKHUB_REPO", DEFAULT_REPO),
                   help="GitHub 仓库 owner/name")
    p.add_argument("--token", help="GitHub 令牌（默认读环境变量/凭据文件）")
    p.add_argument("--credentials", help="凭据文件路径（含 GITHUB_TOKEN=...）")
    p.add_argument("--state", help="本地认领状态文件路径")


def default_worker():
    return os.environ.get("TASKHUB_WORKER") or socket.gethostname()


def build_parser():
    p = argparse.ArgumentParser(description="TaskHub GitHub Issues 任务黑板客户端")
    global_flags(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="列出任务")
    s.add_argument("--status", default="open",
                   choices=["open", "pending", "claimed", "done", "failed", "cancelled", "all"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("create", help="发布任务")
    s.add_argument("--title", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--priority", default="P1", choices=["P0", "P1", "P2"])
    s.add_argument("--tag", action="append", default=[], help="附加标签，可多次")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_create)

    s = sub.add_parser("claim", help="原子认领任务")
    s.add_argument("--worker", default=default_worker())
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--max-load", type=int, default=1,
                   help="并发进行中任务上限（第10节；达到就不再接通用任务）")
    s.add_argument("--lease-min", type=int, default=DEFAULT_LEASE_MIN)
    s.add_argument("--tag", action="append", default=[], help="只认领带这些标签的任务")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("show", help="查看任务详情与评论")
    s.add_argument("--issue", type=int, required=True)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("heartbeat", help="续租（长任务定期执行）")
    s.add_argument("--issue", type=int, required=True)
    s.add_argument("--worker", default=default_worker())
    s.add_argument("--lease-min", type=int, default=DEFAULT_LEASE_MIN)
    s.set_defaults(func=cmd_heartbeat)

    s = sub.add_parser("complete", help="完成任务并回传结果")
    s.add_argument("--issue", type=int, required=True)
    s.add_argument("--worker", default=default_worker())
    s.add_argument("--result", required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_complete)

    s = sub.add_parser("fail", help="标记任务失败")
    s.add_argument("--issue", type=int, required=True)
    s.add_argument("--worker", default=default_worker())
    s.add_argument("--error", required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_fail)

    s = sub.add_parser("release", help="放弃认领，任务回到 pending")
    s.add_argument("--issue", type=int, required=True)
    s.add_argument("--worker", default=default_worker())
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("cancel", help="取消任务（发布者用）")
    s.add_argument("--issue", type=int, required=True)
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("reopen", help="重新打开已关闭任务")
    s.add_argument("--issue", type=int, required=True)
    s.set_defaults(func=cmd_reopen)

    s = sub.add_parser("selftest", help="端到端自检")
    s.set_defaults(func=cmd_selftest)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
