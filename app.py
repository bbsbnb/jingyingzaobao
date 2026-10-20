"""
app.py — AI-CEO 经营早报 本地服务（标准库，零第三方依赖）

启动：  python app.py            （默认 http://127.0.0.1:8000）
启动：  python app.py 9000       （指定端口）

接口：
  GET  /                     看板前端
  POST /api/upload            {filename, data(base64)} -> 解析+存储+返回早报
  GET  /api/weeks             周次列表（含闭环率摘要）
  GET  /api/briefing?week=ID  某周早报 JSON（缺省最新周）
  GET  /api/consolidated      跨周整合（趋势/滚存/人员一致性）
  GET  /api/health            ping

存储：SQLite（ai_ceo.db），单文件零配置；生产可平滑迁移 PostgreSQL（见 README）。
LLM：  可选、可插拔。通过页面「大模型配置」面板或 config.json / 环境变量配置
       国内通用模型（DeepSeek / 通义 / 智谱 / 豆包 / Kimi / 自定义端点）。
       开启后：① 纯文本 / 群消息上传可用大模型抽取行动项；② 早报「一句话结论」
       经大模型增强为自然语言综述 + 督办焦点。未配置则全程规则驱动、离线可跑，
       任何 LLM 失败都会静默回退到规则版，不阻断主流程。
"""

import os
import io
import re
import sys
import json
import base64
import sqlite3
import datetime
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import parser as P
import analyzer as A
import llm_client as L
import wecom as W

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ai_ceo.db")
UPLOADS = os.path.join(BASE, "uploads")
os.makedirs(UPLOADS, exist_ok=True)


# ----------------------------------------------------------------------------
# 数据库
# ----------------------------------------------------------------------------
def db_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weeks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            source TEXT,
            gen_date TEXT,
            ref_month INTEGER,
            ref_day INTEGER,
            created_at TEXT,
            raw_text TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_id INTEGER,
            title TEXT,
            owners_json TEXT,
            status TEXT,
            due TEXT,
            basis TEXT,
            note TEXT,
            raw_status TEXT
        );
        """
    )
    # 轻量迁移：为已存在的库补 people_json 列（责任分布表持久化）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(weeks)").fetchall()]
    if "people_json" not in cols:
        conn.execute("ALTER TABLE weeks ADD COLUMN people_json TEXT")
    # 企微回调去重表：记录已处理的 MsgId，防止企微重试/重复投递造成重复入库
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wecom_log ("
        "  msg_id TEXT PRIMARY KEY, ts TEXT)"
    )
    conn.commit()
    conn.close()


_DATE_IN_NAME = re.compile(r"(\d{4})[.\-_/](\d{1,2})[.\-_/](\d{1,2})")


def derive_label(filename):
    """从文件名推断周次标签。

    例：2026.10.12-2026.10.18（第41周）工作计划安排.xls
        -> 2026.10.12-10.18（第41周）
    取不到日期时退回「周报 上传日期」。
    """
    src = filename or ""
    m = _DATE_IN_NAME.search(src)
    if not m:
        return "周报 " + datetime.date.today().isoformat()
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    span = "%s.%02d.%02d" % (y, mo, d)
    m2 = _DATE_IN_NAME.search(src, m.end())
    if m2 and m2.group(1) == y:
        span += "-%02d.%02d" % (int(m2.group(2)), int(m2.group(3)))
    wk = re.search(r"第\s*(\d+)\s*周", src)
    if wk:
        span = "第%s周 · %s" % (wk.group(1), span)
    return span


def label_ref_date(label):
    """从标签取参考日期（月, 日）。区间取结束日，逾期判定以周内截止为准。"""
    found = _DATE_IN_NAME.findall(label or "")
    if not found:
        return None
    try:
        return (int(found[-1][1]), int(found[-1][2]))
    except ValueError:
        return None


def save_week(meta, parsed):
    conn = db_conn()
    cur = conn.cursor()
    ref = meta.get("ref_date")
    base_label = meta.get("label", "")
    cur.execute(
        "INSERT INTO weeks (label, source, gen_date, ref_month, ref_day, created_at, raw_text, people_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            base_label,
            parsed["source"],
            meta.get("gen_date", ""),
            ref[0] if ref else None,
            ref[1] if ref else None,
            datetime.datetime.now().isoformat(timespec="seconds"),
            parsed.get("text", ""),
            json.dumps(parsed.get("people", []), ensure_ascii=False),
        ),
    )
    wid = cur.lastrowid
    # 文件名已明确「第 N 周」时沿用该周号，不再叠加内部自增编号，避免 W 序号与真实周次打架
    if base_label and not re.search(r"第\s*\d+\s*周", base_label):
        final_label = f"W{wid} · {base_label}"
    else:
        final_label = base_label or f"W{wid}"
    cur.execute("UPDATE weeks SET label=? WHERE id=?", (final_label, wid))
    for t in parsed["tasks"]:
        cur.execute(
            "INSERT INTO tasks (week_id, title, owners_json, status, due, basis, note, raw_status) VALUES (?,?,?,?,?,?,?,?)",
            (
                wid,
                t["title"],
                json.dumps(t["owners"], ensure_ascii=False),
                t["status"],
                t.get("due", ""),
                t.get("basis", ""),
                t.get("note", ""),
                t.get("raw_status", ""),
            ),
        )
    conn.commit()
    conn.close()
    return wid


def load_week(wid):
    conn = db_conn()
    w = conn.execute("SELECT * FROM weeks WHERE id=?", (wid,)).fetchone()
    if not w:
        conn.close()
        return None
    rows = conn.execute("SELECT * FROM tasks WHERE week_id=?", (wid,)).fetchall()
    conn.close()
    tasks = [
        {
            "id": r["id"],
            "title": r["title"],
            "owners": json.loads(r["owners_json"] or "[]"),
            "status": r["status"],
            "due": r["due"],
            "basis": r["basis"],
            "note": r["note"],
            "raw_status": r["raw_status"],
        }
        for r in rows
    ]
    meta = {
        "label": w["label"],
        "gen_date": w["gen_date"],
        "ref_date": (w["ref_month"], w["ref_day"]) if w["ref_month"] else None,
        "source": w["source"],
        "week_id": w["id"],
    }
    try:
        people = json.loads(w["people_json"] or "[]")
    except Exception:
        people = []
    return {"meta": meta, "tasks": tasks, "people": people}


def list_weeks():
    conn = db_conn()
    rows = conn.execute(
        "SELECT w.id, w.label, w.source, w.gen_date, w.created_at, COUNT(t.id) AS total, "
        "SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN t.status='partial' THEN 1 ELSE 0 END) AS partial, "
        "SUM(CASE WHEN t.status='doing' THEN 1 ELSE 0 END) AS doing, "
        "SUM(CASE WHEN t.status='stuck' THEN 1 ELSE 0 END) AS stuck "
        "FROM weeks w LEFT JOIN tasks t ON t.week_id=w.id GROUP BY w.id ORDER BY w.id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# 花名册累积（供 LLM 抽取对齐责任人）
# ----------------------------------------------------------------------------
def get_roster():
    conn = db_conn()
    rows = conn.execute("SELECT owners_json FROM tasks").fetchall()
    conn.close()
    s = set()
    for r in rows:
        for o in json.loads(r[0] or "[]"):
            if o and o != "未指派":
                s.add(o)
    return sorted(s)


# ----------------------------------------------------------------------------
# 业务逻辑
# ----------------------------------------------------------------------------
def _task_brief(tasks, limit=300):
    """上传响应里回传的精简任务清单，供前端在「本次识别结果」中直接展示。"""
    return [
        {
            "id": t.get("id"),
            "title": t.get("title", ""),
            "owners": t.get("owners", []),
            "status": t.get("status", ""),
            "due": t.get("due", ""),
        }
        for t in tasks[:limit]
    ]


def process_upload(filename, data: bytes, use_llm=False):
    parsed = P.parse_file(filename, data)
    # 非结构化源（纯文本兜底结果，或结构化文件中未识别到任何任务结构）：
    # 开启 AI 抽取时交给大模型重抽；规则表识别成功则完全不经过此分支。
    llm_hint = ""
    need_llm = use_llm and parsed["text"].strip() and (parsed.get("heuristic") or not parsed["tasks"])
    if need_llm:
        cfg = L.load_config()
        if not (cfg.get("enabled") and cfg.get("use_for_extract")):
            llm_hint = "已勾选「AI 智能抽取」，但大模型尚未配置或未启用（见下方「大模型配置」），本次按规则解析。"
        else:
            extracted = L.extract_actions(parsed["text"], get_roster())
            if extracted:
                parsed["tasks"] = extracted
                parsed["source"] = parsed["source"] + "+llm"
                parsed["heuristic"] = False
                parsed["mode"] = "llm"
            else:
                llm_hint = "AI 抽取未返回有效任务，已回退规则解析。"
    # 未解析到任务则不落库，避免产生空周次污染历史
    if not parsed["tasks"]:
        return {
            "week_id": None,
            "parsed_count": 0,
            "source": parsed["source"],
            "tasks": [],
            "llm_hint": llm_hint,
            "error": parsed.get("hint") or "未解析到任何任务，请检查文件内容。",
        }
    # 生成周次标签与参考日期：区间结束日作为逾期判定基准
    gen_date = datetime.date.today().isoformat()
    label = derive_label(filename)
    ref = label_ref_date(label) or (datetime.date.today().month, datetime.date.today().day)
    meta = {"label": label, "gen_date": gen_date, "ref_date": ref}
    wid = save_week(meta, parsed)
    wk = load_week(wid)
    briefing = A.analyze(wk["tasks"], parsed.get("people"), wk["meta"])
    briefing = L.enhance_summary(briefing)  # 配置开启时增强，否则原样返回
    return {
        "week_id": wid,
        "briefing": briefing,
        "parsed_count": len(parsed["tasks"]),
        "source": parsed["source"],
        "mode": parsed.get("mode", ""),
        "people": parsed.get("people", []),
        "tasks": _task_brief(parsed["tasks"]),
        "hint": parsed.get("hint", ""),
            "llm_hint": llm_hint,
        }


# ----------------------------------------------------------------------------
# 结构化任务入库（企微回调 / 外部 /api/tasks 通用）
# ----------------------------------------------------------------------------
def ingest_tasks(label, tasks, people=None, source="api", text="", use_llm=False):
    """直接以结构化任务列表入库，不依赖文件解析。返回与 process_upload 同构。"""
    if not tasks:
        return {"week_id": None, "parsed_count": 0, "source": source,
                "tasks": [], "error": "未解析到任何任务，请检查内容。"}
    if use_llm:
        cfg = L.load_config()
        if cfg.get("enabled") and cfg.get("use_for_extract"):
            extracted = L.extract_actions(text or _tasks_to_text(tasks), get_roster())
            if extracted:
                tasks = extracted
    gen_date = datetime.date.today().isoformat()
    ref = label_ref_date(label) or (datetime.date.today().month, datetime.date.today().day)
    parsed = {"source": source, "tasks": tasks, "people": people or [],
              "text": text, "mode": "structured", "heuristic": False, "hint": ""}
    meta = {"label": label, "gen_date": gen_date, "ref_date": ref}
    wid = save_week(meta, parsed)
    wk = load_week(wid)
    briefing = L.enhance_summary(A.analyze(wk["tasks"], parsed.get("people"), wk["meta"]))
    return {
        "week_id": wid, "briefing": briefing, "parsed_count": len(tasks),
        "source": source, "mode": "structured",
        "tasks": _task_brief(tasks), "hint": "", "llm_hint": "",
    }


def _tasks_to_text(tasks):
    lines = []
    for t in tasks:
        s = t.get("title", "")
        if t.get("owners"):
            s += " 负责人：" + "、".join(t["owners"])
        if t.get("due"):
            s += " 截止：" + t["due"]
        if t.get("status"):
            s += " 状态：" + t["status"]
        lines.append(s)
    return "\n".join(lines)


def _wecom_cfg_path():
    return L.config_path()


def load_wecom_config():
    p = _wecom_cfg_path()
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
            return cfg.get("wecom", {}) or {}
        except Exception:
            return {}
    return {}


def save_wecom_config(new):
    p = _wecom_cfg_path()
    cfg = {}
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except Exception:
            cfg = {}
    wc = cfg.get("wecom", {}) or {}
    for k in ("enabled", "mode", "token", "encoding_aes_key", "corp_id", "agent_id",
              "group_webhook", "auto_ingest", "reply_on_receive",
              "bot_id", "bot_secret", "bot_enabled"):
        if k in new and new[k] is not None:
            # 长连接敏感字段：前端未改动（提交空串）时保留原值，避免误清空
            if k in ("bot_id", "bot_secret") and new[k] == "":
                continue
            wc[k] = new[k]
    cfg["wecom"] = wc
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return wc


def public_wecom_config():
    wc = load_wecom_config()
    masked = {}
    for k, v in wc.items():
        if k in ("token", "encoding_aes_key", "group_webhook", "bot_secret") and v:
            s = str(v)
            masked[k] = (s[:3] + "****" + s[-3:]) if len(s) > 8 else "****"
        else:
            masked[k] = v
    mode = wc.get("mode", "callback")
    if mode == "bot":
        masked["configured"] = bool(wc.get("bot_id") and wc.get("bot_secret"))
    else:
        masked["configured"] = bool(wc.get("token") and wc.get("encoding_aes_key") and wc.get("corp_id"))
    masked["enabled"] = bool(wc.get("enabled")) or bool(wc.get("bot_enabled"))
    masked["mode"] = mode
    return masked


# ----------------------------------------------------------------------------
# 智能机器人长连接（Bot）运行管理
# ----------------------------------------------------------------------------
_bot_client = None
_bot_state = {"state": "stopped", "msg": "", "since": None, "recv": 0}


def _bot_on_message(text, chatid, chat_type, from_user, msgid, msgtype,
                    req_id=None, send_reply=None):
    """长连接收到消息后的落地逻辑：抽取任务入库 + 走长连接上行回包（零 token）。"""
    wc = load_wecom_config()
    if not wc.get("auto_ingest", True):
        return
    if msgtype != "text" or not text:
        return
    if msgid and wecom_seen(msgid):
        return
    roster = get_roster()
    cfg = L.load_config()
    tasks = None
    if cfg.get("enabled") and cfg.get("use_for_extract"):
        tasks = L.extract_actions(text, roster)
    if not tasks:
        tasks = W.rule_extract(text, roster)
    if tasks:
        label = "企微长连接·" + datetime.date.today().isoformat()
        res = ingest_tasks(label, tasks, source="wecom_bot", text=text)
        n = res.get("parsed_count", 0)
        note = "已收录 %d 项任务，已生成经营早报看板。" % n
        _bot_state["recv"] += 1
    else:
        note = "收到消息但未识别到任务。格式示例：标题 负责人：张三 截止：周五"
    if msgid:
        wecom_mark_seen(msgid)
    if wc.get("reply_on_receive") and chatid and send_reply:
        try:
            send_reply(note)  # 走 WeComBotClient.send_msg 上行（aibot_send_msg），零凭证
            _bot_state["msg"] = ""
        except Exception as e:
            _bot_state["msg"] = "回复失败：%s" % e


def bot_start():
    global _bot_client, _bot_state
    wc = load_wecom_config()
    bid = (wc.get("bot_id") or "").strip()
    sec = (wc.get("bot_secret") or "").strip()
    if not bid or not sec:
        return {"ok": False, "error": "未配置 BotID / Secret，请先到「智能机器人长连接」页填写并保存"}
    if _bot_client and _bot_client.running:
        return {"ok": True, "state": "running", "msg": "长连接已在运行"}
    _bot_state = {"state": "starting", "msg": "正在连接企微长连接网关…",
                  "since": datetime.datetime.now().isoformat(timespec="seconds"),
                  "recv": _bot_state.get("recv", 0)}

    def _log(m):
        _bot_state["msg"] = m
        if "已订阅" in m or "建立" in m:
            _bot_state["state"] = "running"

    _bot_client = W.WeComBotClient(bid, sec, _bot_on_message, on_log=_log)
    _bot_client.start()
    return {"ok": True, "state": "starting"}


def bot_stop():
    global _bot_client, _bot_state
    if _bot_client:
        _bot_client.stop()
    _bot_client = None
    _bot_state = {"state": "stopped", "msg": "已手动断开",
                  "since": None, "recv": _bot_state.get("recv", 0)}
    return {"ok": True, "state": "stopped"}


def bot_status():
    if _bot_client and _bot_client.running:
        _bot_state["state"] = "running"
    return _bot_state


def bot_test(bot_id, bot_secret):
    """长连通性测试：直接连企微 wss 网关并 aibot_subscribe，返回订阅结果。
    零凭证：不依赖 get_bot_access_token（该路径已返回 404）。"""
    wc = load_wecom_config()
    bid = (bot_id or "").strip() or (wc.get("bot_id") or "").strip()
    sec = (bot_secret or "").strip() or (wc.get("bot_secret") or "").strip()
    if not bid or not sec:
        return {"ok": False, "detail": "BotID / Secret 不能为空，请先填写并保存"}
    try:
        ws = W.MiniWebSocket(W._WS_HOST, W._WS_PORT, "/", tls=True)
        ws.connect()
        rid = W._req_id()
        ws.send_text(json.dumps({"cmd": "aibot_subscribe",
                                  "headers": {"req_id": rid},
                                  "body": {"bot_id": bid, "secret": sec}}))
        resp = ws.wait_response(rid, timeout=10)
        ws.close()
        if resp is None:
            return {"ok": False, "detail": "网关未响应订阅（超时），请检查网络"}
        if resp.get("errcode", 0) != 0:
            detail = "订阅失败：%s" % resp.get("errmsg")
            if not (bot_id or "").strip() or not (bot_secret or "").strip():
                detail += "（凭证来自已保存配置）"
            return {"ok": False, "detail": detail}
        return {"ok": True, "detail": "长连接可达，订阅成功，可建立常驻连接"}
    except Exception as e:
        return {"ok": False, "detail": "连接失败：%s" % e}


def wecom_mark_seen(msg_id):
    conn = db_conn()
    conn.execute("INSERT OR IGNORE INTO wecom_log (msg_id, ts) VALUES (?,?)",
                 (msg_id, datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def wecom_seen(msg_id):
    conn = db_conn()
    row = conn.execute("SELECT 1 FROM wecom_log WHERE msg_id=?", (msg_id,)).fetchone()
    conn.close()
    return row is not None


def wecom_selftest(token, aeskey, corp_id):
    """企微配置自检：在不去麻烦企微后台的前提下，本地跑一遍完整加解密回环。

    返回 {ok, checks:[{name, pass, detail}], summary}。任何一项不过都给出可操作提示。
    """
    checks = []

    def chk(name, ok, detail=""):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        return ok

    token = (token or "").strip()
    aeskey = (aeskey or "").strip()
    corp_id = (corp_id or "").strip()

    chk("Token 已填写", bool(token),
        "企微后台「接收消息 → 设置API接收」里的 Token，两边必须完全一致（区分大小写）" if not token else "ok")
    if not token:
        return {"ok": False, "checks": checks,
                "summary": "Token 为空：企微验证请求会直接被拒（403），请先填写"}

    key_len_ok = len(aeskey) == 43
    chk("EncodingAESKey 为 43 位", key_len_ok,
        "当前 %d 位。企微 EncodingAESKey 固定 43 字符（可点后台「随机生成」），请整串复制勿带空格" % len(aeskey))
    if not key_len_ok:
        return {"ok": False, "checks": checks,
                "summary": "EncodingAESKey 长度必须为 43 位（当前 %d 位）" % len(aeskey)}

    try:
        k = W.decode_aes_key(aeskey)
        len_ok = len(k) == 32
        chk("EncodingAESKey 可解码为 32 字节", len_ok,
            "base64 解码得到 %d 字节，应为 32" % len(k))
        if not len_ok:
            return {"ok": False, "checks": checks, "summary": "EncodingAESKey 解码后不是 32 字节"}
    except Exception as e:
        chk("EncodingAESKey 可解码为 32 字节", False, "base64 解码失败：%s" % e)
        return {"ok": False, "checks": checks,
                "summary": "EncodingAESKey 不是合法 base64（只含 A-Z/a-z/0-9，无空格换行）"}

    # CorpID 格式提醒（不阻断：配错只是 receiveid 校验告警，签名才是安全边界）
    if corp_id:
        cid_ok = corp_id.startswith("ww") and len(corp_id) >= 10
        chk("CorpID 格式", cid_ok,
            "ok" if cid_ok else "CorpID 通常形如 ww + 16位（我的企业 → 企业信息 → CorpID）；配错不影响验证通过，但建议填对")

    # 完整回环：模拟企微 GET 验证（签名 + AES 加解密 + receiveid 回带）
    try:
        import os as _os
        echo = "".join(_os.urandom(8).hex())[:16]
        ts, nonce = "1700000000", "selftestnonce"
        enc = W.encrypt_reply(aeskey, echo, corp_id or "wwselftest")
        sig = W.make_signature(token, ts, nonce, enc)
        sig_ok = W.verify_signature(token, ts, nonce, enc, sig)
        chk("签名校验（sha1）", sig_ok, "Token 参与签名计算，不一致则企微判 403")
        if not sig_ok:
            return {"ok": False, "checks": checks, "summary": "签名自校验异常（代码内部错误）"}
        dec, rid = W.decrypt_message(aeskey, enc, corp_id)
        rt_ok = (dec == echo)
        chk("AES 加解密回环", rt_ok,
            "解密得到 %r" % dec if not rt_ok else "加密→解密一致，echostr 可正确回显")
        if not rt_ok:
            return {"ok": False, "checks": checks, "summary": "加解密回环失败（内部错误）"}
        if corp_id and rid != corp_id:
            chk("receiveid 与 CorpID 一致", False,
                "解密出的 receiveid=%r 与所填 CorpID 不一致，请核对 CorpID" % rid)
            return {"ok": False, "checks": checks, "summary": "CorpID 与密钥不匹配"}
        chk("receiveid 与 CorpID 一致", True, "ok")
    except Exception as e:
        chk("AES 加解密回环", False, "异常：%s" % e)
        return {"ok": False, "checks": checks, "summary": "加解密异常：%s" % e}

    return {"ok": True, "checks": checks,
            "summary": "全部通过：可保存配置，再去企微后台点「保存」即可通过验证"}


def get_briefing(week_id=None):
    weeks = list_weeks()
    if not weeks:
        return None
    ids = {w["id"] for w in weeks}
    # 请求的周次不存在（如已被删除）时回退到最新周次，避免返回 null 造成前端空白
    wid = week_id if week_id in ids else weeks[0]["id"]
    wk = load_week(wid)
    if not wk:
        return None
    b = A.analyze(wk["tasks"], wk.get("people"), wk["meta"])
    b["week_id"] = wid
    b["label"] = wk["meta"].get("label", "")
    b["tasks"] = _task_brief(wk["tasks"])
    return b


def get_consolidated(week_ids=None):
    """week_ids 为 None 时整合全部周次；否则只整合指定的周次集合（按时间升序）。"""
    weeks = list_weeks()
    if week_ids is not None:
        keep = set(week_ids)
        weeks = [w for w in weeks if w["id"] in keep]
    if not weeks:
        return A.consolidate([])
    loaded = []
    for w in reversed(weeks):  # 升序
        wk = load_week(w["id"])
        loaded.append({"meta": wk["meta"], "kpis": A.analyze(wk["tasks"], None, wk["meta"])["kpis"], "tasks": wk["tasks"]})
    return A.consolidate(loaded)


def parse_week_ids(raw_query):
    """从 query string 解析 weeks=1,2,3。缺省返回 None（全部周次）；weeks= 空值返回 []（未选任何周次）。"""
    q = parse_qs(raw_query, keep_blank_values=True)
    if "weeks" not in q:
        return None
    ids = []
    for part in q["weeks"][0].split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def delete_week(wid):
    conn = db_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM weeks WHERE id=?", (wid,)).fetchone()
    if not row:
        conn.close()
        return False
    cur.execute("DELETE FROM tasks WHERE week_id=?", (wid,))
    cur.execute("DELETE FROM weeks WHERE id=?", (wid,))
    conn.commit()
    conn.close()
    return True


# ----------------------------------------------------------------------------
# 周报 / 跨周 可下载 HTML 报告（保存到本地）
# ----------------------------------------------------------------------------
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;") if s is not None else "")


def _lanes_html(board):
    lanes = [("done", "已闭环", "#EAF3DE", "#639922"), ("partial", "收尾中", "#E6F1FB", "#378ADD"),
             ("doing", "进行中", "#FAEEDA", "#BA7517"), ("stuck", "卡住未反馈", "#FCEBEB", "#E24B4A")]
    h = '<div class="lanes">'
    for key, name, bg, fg in lanes:
        items = (board or {}).get(key, [])
        h += f'<div class="lane" style="background:{bg}"><h3>{name} <span class="cnt">{len(items)}</span></h3>'
        for t in items:
            own = "、".join(t.get("owners") or []) or "未指派"
            meta = t.get("due") or t.get("basis")
            h += (f'<div class="tcard"><div class="t">{_esc(t["title"])}</div>'
                  f'<div class="own">{_esc(own)}</div>'
                  + (f'<div class="meta">{_esc(meta)}</div>' if meta else "") + '</div>')
        h += '</div>'
    h += '</div>'
    return h


REPORT_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#F7F8FA;color:#1F2329;font-size:13px;line-height:1.6;padding:20px;max-width:1000px;margin:0 auto}
h1{font-size:18px;font-weight:600}.sub{color:#6B7280;font-size:12px;margin:2px 0 14px}
.summary{background:#0F2A47;color:#EAF2FB;border-radius:10px;padding:12px 14px;margin-bottom:12px}
.summary .lab{color:#9FC3E8;font-size:11px;display:block;margin-bottom:4px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:14px}
.kpi{background:#fff;border:1px solid #E5E7EB;border-radius:10px;padding:10px}
.kpi .v{font-size:20px;font-weight:600}.kpi .l{font-size:11px;color:#6B7280;margin-top:2px}
.kpi.warn .v{color:#854F0B;border-color:#FAC775}.kpi.risk .v{color:#A32D2D;border-color:#F7C1C1}.kpi.good .v{color:#3B6D11;border-color:#BFE0A0}
h2{font-size:14px;font-weight:600;margin:14px 0 8px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{border:1px solid #E5E7EB;padding:6px 8px;text-align:left}th{background:#F1EFE8;color:#5F5E5A;font-weight:500}
td.good{color:#3B6D11}td.risk{color:#A32D2D}
.alert{background:#FAEEDA;border:1px solid #FAC775;border-radius:8px;padding:8px;margin-top:8px}
.alert.r{background:#FCEBEB;border-color:#F7C1C1}
.alert .a-t{font-weight:500;margin-bottom:2px}.alert .a-s{font-size:11px;color:#633806}
.alert.r .a-s{color:#791F1F}
.bar{display:flex;height:8px;border-radius:4px;overflow:hidden;margin-top:6px;background:#E5E7EB}.bar i{display:block;height:100%}
.person{background:#F7F8FA;border-radius:8px;padding:8px;margin-top:8px}.person .p-h{display:flex;justify-content:space-between}
.person .p-n{font-weight:500}.person .p-o{font-size:11px;color:#6B7280}.person .p-note{font-size:11px;color:#6B7280;margin-top:4px}
.lanes{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.lane{border-radius:10px;padding:8px}
.lane h3{font-size:12px;font-weight:600;padding:4px 6px;display:flex;justify-content:space-between}
.tcard{background:#fff;border:1px solid #E5E7EB;border-radius:8px;padding:8px;margin-top:8px}
.tcard .t{font-weight:500}.tcard .own{font-size:11px;color:#378ADD;margin-top:2px}.tcard .meta{font-size:11px;color:#6B7280;margin-top:4px}
.roll{background:#FCEBEB;border:1px solid #F7C1C1;border-radius:8px;padding:8px;margin-top:8px;font-size:12px}
.chip{display:inline-block;font-size:11px;background:#E6F1FB;color:#185FA5;border-radius:6px;padding:1px 6px;margin:2px 4px 0 0}
.tag{display:inline-block;font-size:11px;border-radius:6px;padding:1px 6px;margin-right:4px}
.tag.r{background:#FCEBEB;color:#A32D2D}.tag.w{background:#FAEEDA;color:#854F0B}
footer{margin-top:18px;font-size:11px;color:#6B7280;border-top:1px solid #E5E7EB;padding-top:10px}
"""


def render_report(week_id=None):
    """生成单周可下载 HTML 报告。week_id=None 为最新周。"""
    b = get_briefing(week_id)
    if not b:
        return ("<html><head><meta charset='utf-8'></head><body><h1>暂无数据</h1>"
                "<p>请先上传任务清单。</p></body></html>")
    k = b["kpis"]
    meta = b.get("meta", {})
    head = f"""<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI-CEO 经营早报 · {_esc(meta.get('label',''))}</title>
<style>{REPORT_CSS}</style></head><body>
<h1>天行 · AI-CEO 经营早报</h1>
<div class="sub">{_esc(meta.get('label',''))} ｜ 生成 {_esc(meta.get('gen_date',''))} ｜ 来源 {_esc(meta.get('source',''))}</div>
<div class="summary"><span class="lab">一句话结论</span>{_esc(b['summary'])}</div>
<div class="kpis">
<div class="kpi {'good' if k['health']>=7 else ('warn' if k['health']>=5 else 'risk')}"><div class="v">{k['health']}</div><div class="l">经营健康指数</div></div>
<div class="kpi"><div class="v">{k['closure_rate']}%</div><div class="l">行动闭环率（加权）</div></div>
<div class="kpi {'risk' if k['risk_count'] else ''}"><div class="v">{k['risk_count']}</div><div class="l">风险项</div></div>
<div class="kpi {'risk' if k['overdue'] else ''}"><div class="v">{k['overdue']}</div><div class="l">逾期行动</div></div>
<div class="kpi"><div class="v">{k['total']}</div><div class="l">派单总数</div></div>
</div>
<h2>异常警报 · 今天什么会炸</h2>"""
    alerts_html = "".join(
        f'<div class="alert {"r" if a["type"]=="risk" else ""}"><div class="a-t">'
        f'<span class="tag {"r" if a["type"]=="risk" else "w"}">{"风险" if a["type"]=="risk" else "关注"}</span>{_esc(a["title"])}</div>'
        f'<div class="a-s">{_esc(a["detail"])}</div></div>'
        for a in b.get("alerts", [])) or '<div class="sub">暂无警报</div>'
    people_html = "".join(
        f'<div class="person"><div class="p-h"><span class="p-n">{_esc(p["name"])}</span>'
        f'<span class="p-o">承接 {p["total"]} · 完 {p["done"]} · 部分 {p["partial"]} · 卡 {p["stuck"]} · 进 {p["doing"]}</span></div>'
        f'<div class="bar"><i style="width:{p["done"]/( (p["done"]+p["partial"]+p["stuck"]+p["doing"]) or 1)*100}%;background:#639922"></i>'
        f'<i style="width:{p["partial"]/( (p["done"]+p["partial"]+p["stuck"]+p["doing"]) or 1)*100}%;background:#378ADD"></i>'
        f'<i style="width:{p["stuck"]/( (p["done"]+p["partial"]+p["stuck"]+p["doing"]) or 1)*100}%;background:#E24B4A"></i>'
        f'<i style="width:{p["doing"]/( (p["done"]+p["partial"]+p["stuck"]+p["doing"]) or 1)*100}%;background:#BA7517"></i></div>'
        + (f'<div class="p-note">{_esc(p["note"])}</div>' if p.get("note") else "") + '</div>'
        for p in b.get("people", [])) or '<div class="sub">无人员数据</div>'
    board_html = _lanes_html(b.get("board", {}))
    task_rows = "".join(
        f'<tr><td>{_esc(t["title"])}</td><td>{"、".join(t["owners"]) or "未指派"}</td>'
        f'<td class="{"good" if t["status"]=="done" else ("risk" if t["status"]=="stuck" else "")}">{_esc(t["status"])}</td>'
        f'<td>{_esc(t.get("due",""))}</td><td>{_esc(t.get("note",""))}</td></tr>'
        for t in b.get("tasks", []))
    foot = f"<footer>由 AI-CEO 经营早报生成 · 数据口径：群内回执口径，闭环率=(已闭环+0.5×收尾中)/总数 · 生成于 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</footer>"
    return head + alerts_html + '<h2>人的信号 · 谁好谁差</h2>' + people_html + \
        '<h2>任务闭环看板</h2>' + board_html + \
        '<h2>全量任务清单</h2><table><thead><tr><th>任务</th><th>责任人</th><th>状态</th><th>时限</th><th>备注</th></tr></thead><tbody>' + \
        (task_rows or '<tr><td colspan="5">暂无任务</td></tr>') + '</tbody></table>' + foot + '</body></html>'


def render_consolidated_report(week_ids=None):
    """跨周整合报告（单文件 HTML）。week_ids 指定参与整合的周次。"""
    weeks = list_weeks()
    if week_ids is not None:
        keep = set(week_ids)
        weeks = [w for w in weeks if w["id"] in keep]
    c = get_consolidated(week_ids)
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    scope = "、".join(w["label"].split(" ·")[0] for w in reversed(weeks)) or "无"
    head = f"""<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI-CEO 跨周整合报告</title><style>{REPORT_CSS}</style></head><body>
<h1>天行 · AI-CEO 跨周整合报告</h1><div class="sub">生成于 {now} ｜ 覆盖 {len(weeks)} 个周次（{_esc(scope)}）</div>
<h2>跨周趋势</h2>"""
    trend_rows = "".join(
        f'<tr><td>{_esc(t["label"])}</td><td>{t["total"]}</td><td>{t["closure_rate"]}%</td>'
        f'<td class="good">{t["done"]}</td><td class="risk">{t["stuck"]}</td></tr>'
        for t in c.get("trend", [])) or '<tr><td colspan="5">暂无数据</td></tr>'
    rollover_html = "".join(
        f'<div class="roll">【{_esc(r["from_week"])}】{_esc(r["title"])} — '
        f'{_esc("、".join(r.get("owners") or []) or "未指派")}（{_esc(r["status"])}）｜ {_esc(r.get("note",""))}</div>'
        for r in c.get("rollover", []))
    people_rows = "".join(
        f'<tr><td>{_esc(p["name"])}</td><td>{p["weeks"]}</td><td class="risk">{p["stuck"]}</td><td>{p["total"]}</td></tr>'
        for p in c.get("people_consistency", [])) or '<tr><td colspan="4">无持续承压人员</td></tr>'
    foot = f"<footer>由 AI-CEO 经营早报生成 · 滚存=上周未闭环且本周未再出现的任务 · 生成于 {now}</footer>"
    return head + '<table><thead><tr><th>周次</th><th>总数</th><th>闭环率</th><th>已闭环</th><th>卡住</th></tr></thead><tbody>' + trend_rows + '</tbody></table>' + \
        '<h2>未闭环滚存（跨周仍未收口）</h2>' + (rollover_html or '<div class="sub">暂无滚存</div>') + \
        '<h2>跨周持续承压人员</h2><table><thead><tr><th>姓名</th><th>出现周次</th><th>未闭环累计</th><th>承接累计</th></tr></thead><tbody>' + people_rows + '</tbody></table>' + foot + '</body></html>'


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/health":
            return self._send(200, {"ok": True})
        if u.path == "/api/weeks":
            return self._send(200, list_weeks())
        if u.path == "/api/briefing":
            q = parse_qs(u.query)
            wid = int(q["week"][0]) if q.get("week") else None
            b = get_briefing(wid)
            return self._send(200, b if b else {"error": "no data"})
        if u.path == "/api/consolidated":
            return self._send(200, get_consolidated(parse_week_ids(u.query)))
        if u.path == "/api/config":
            return self._send(200, L.public_config())
        if u.path == "/api/config/providers":
            return self._send(200, [
                {"key": k, "label": v["label"], "base_url": v["base_url"], "model": v["model"]}
                for k, v in L.PRESETS.items()
            ])
        if u.path == "/api/report":
            q = parse_qs(u.query)
            wid = int(q["week"][0]) if q.get("week") else None
            html = render_report(wid)
            body = html.encode("utf-8")
            meta = (get_briefing(wid) or {}).get("meta", {})
            fname = f"AI-CEO早报_{meta.get('label') or 'latest'}.html".replace("/", "-").replace("\\", "-")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{urllib.parse.quote(fname)}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/report/consolidated":
            body = render_consolidated_report(parse_week_ids(u.query)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote("AI-CEO跨周整合报告.html"))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        # 企业微信自建应用「接收消息」回调 URL 验证（GET）
        if u.path == "/wecom/callback":
            q = parse_qs(u.query)
            return self._wecom_verify(q)
        if u.path == "/api/wecom/config":
            return self._send(200, public_wecom_config())
        if u.path == "/api/wecom/bot/status":
            return self._send(200, bot_status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if u.path == "/api/upload":
            try:
                payload = json.loads(raw)
                filename = payload.get("filename", "upload.bin")
                b64 = payload.get("data", "")
                if b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                data = base64.b64decode(b64)
            except Exception as e:
                return self._send(400, {"error": f"bad payload: {e}"})
            if not data:
                return self._send(400, {"error": "empty file"})
            try:
                result = process_upload(filename, data, use_llm=bool(payload.get("use_llm")))
            except Exception as e:
                return self._send(500, {"error": str(e)})
            return self._send(200, result)
        if u.path in ("/api/config", "/api/config/test"):
            try:
                cfg = json.loads(raw)
            except Exception as e:
                return self._send(400, {"error": f"bad payload: {e}"})
            if u.path == "/api/config":
                return self._send(200, L.public_config(L.save_config(cfg)))
            return self._send(200, L.test_connection(cfg))
        # 企业微信自建应用「接收消息」回调（POST，消息体为加密 XML）
        if u.path == "/wecom/callback":
            return self._wecom_receive(raw)
        # 结构化任务推送入口（通用：企微/脚本/第三方均可调用）
        if u.path == "/api/tasks":
            return self._api_tasks(raw)
        # 早报主动推送到企业微信群（群机器人 Webhook）
        if u.path == "/api/wecom/push":
            return self._wecom_push(raw)
        # 企业微信配置保存
        if u.path == "/api/wecom/config":
            try:
                cfg = json.loads(raw)
            except Exception as e:
                return self._send(400, {"error": f"bad payload: {e}"})
            wc = save_wecom_config(cfg)
            return self._send(200, public_wecom_config())
        # 企业微信配置自检（保存前先跑一遍加解密回环，不等企微来撞）
        if u.path == "/api/wecom/selftest":
            try:
                cfg = json.loads(raw)
            except Exception as e:
                return self._send(400, {"error": f"bad payload: {e}"})
            res = wecom_selftest(cfg.get("token"), cfg.get("encoding_aes_key"), cfg.get("corp_id"))
            return self._send(200, res)
        # 智能机器人长连接：启动 / 停止 / 状态 / 连通性测试
        if u.path == "/api/wecom/bot/start":
            return self._send(200, bot_start())
        if u.path == "/api/wecom/bot/stop":
            return self._send(200, bot_stop())
        if u.path == "/api/wecom/bot/status":
            return self._send(200, bot_status())
        if u.path == "/api/wecom/bot/test":
            try:
                cfg = json.loads(raw)
            except Exception as e:
                return self._send(400, {"error": f"bad payload: {e}"})
            return self._send(200, bot_test(cfg.get("bot_id"), cfg.get("bot_secret")))
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path == "/api/week":
            q = parse_qs(u.query)
            wid = int(q["id"][0]) if q.get("id") else None
            if wid is None:
                return self._send(400, {"error": "id required"})
            if delete_week(wid):
                return self._send(200, {"ok": True, "deleted": wid})
            return self._send(404, {"error": "week not found"})
        return self._send(404, {"error": "not found"})

    # ------------------------------------------------------------------
    # 企业微信：回调 URL 验证（GET）+ 消息接收（POST）
    # ------------------------------------------------------------------
    def _send_text(self, code, text):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _wecom_verify(self, q):
        wc = load_wecom_config()
        token = wc.get("token", "")
        # 企业微信自建应用回调验证：参数名为 msg_signature
        signature = (q.get("msg_signature") or q.get("signature") or [None])[0]
        timestamp = (q.get("timestamp") or [""])[0]
        nonce = (q.get("nonce") or [""])[0]
        echostr = (q.get("echostr") or [""])[0]
        if not (token and signature and echostr):
            return self._send_text(403, "missing params")
        if not W.verify_signature(token, timestamp, nonce, echostr, signature):
            return self._send_text(403, "invalid signature")
        try:
            plain, _ = W.decrypt_message(wc.get("encoding_aes_key", ""), echostr, wc.get("corp_id", ""))
        except Exception as e:
            return self._send_text(500, "decrypt fail: %s" % e)
        return self._send_text(200, plain)

    def _wecom_receive(self, raw):
        wc = load_wecom_config()
        if not wc.get("enabled"):
            return self._send_text(200, "wecom disabled")
        q = parse_qs(urlparse(self.path).query)
        msg_signature = (q.get("msg_signature") or [None])[0]
        timestamp = (q.get("timestamp") or [""])[0]
        nonce = (q.get("nonce") or [""])[0]
        try:
            xml = W.parse_wecom_xml(raw.decode("utf-8", "ignore"))
        except Exception:
            return self._send_text(400, "bad xml")
        encrypt = xml.get("Encrypt", "")
        if not (msg_signature and encrypt):
            return self._send_text(400, "missing encrypt")
        if not W.verify_signature(wc.get("token", ""), timestamp, nonce, encrypt, msg_signature):
            return self._send_text(403, "invalid signature")
        try:
            msg_text, rid = W.decrypt_message(wc.get("encoding_aes_key", ""), encrypt, wc.get("corp_id", ""))
            inner = W.parse_wecom_xml(msg_text)
        except Exception as e:
            return self._send_text(500, "decrypt fail: %s" % e)
        msg_id = inner.get("MsgId", "")
        if msg_id and wecom_seen(msg_id):
            return self._send_text(200, "duplicated")
        msg_type = inner.get("MsgType", "")
        ingested = 0
        note = ""
        if msg_type == "text" and wc.get("auto_ingest", True):
            content = inner.get("Content", "")
            roster = get_roster()
            tasks = None
            cfg = L.load_config()
            if cfg.get("enabled") and cfg.get("use_for_extract"):
                tasks = L.extract_actions(content, roster)
            if not tasks:
                tasks = W.rule_extract(content, roster)
            if tasks:
                label = "企微群·" + datetime.date.today().isoformat()
                res = ingest_tasks(label, tasks, source="wecom", text=content)
                ingested = res.get("parsed_count", 0)
                if msg_id:
                    wecom_mark_seen(msg_id)
                note = ("已收录 %d 项任务" % ingested) if wc.get("reply_on_receive") else ""
                if wc.get("reply_on_receive") and note:
                    reply = W.encrypt_reply(wc.get("encoding_aes_key", ""), note, rid or wc.get("corp_id", ""))
                    return self._send_text(200, "<xml><Encrypt><![CDATA[%s]]></Encrypt></xml>" % reply)
        else:
            if msg_id:
                wecom_mark_seen(msg_id)
        return self._send_text(200, "ok")

    # ------------------------------------------------------------------
    # 结构化任务推送入口（通用）
    # ------------------------------------------------------------------
    def _api_tasks(self, raw):
        try:
            payload = json.loads(raw)
        except Exception as e:
            return self._send(400, {"error": f"bad payload: {e}"})
        tasks = payload.get("tasks") or []
        if not isinstance(tasks, list):
            return self._send(400, {"error": "tasks 必须是数组"})
        norm = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            title = (t.get("title") or "").strip()
            if not title:
                continue
            owners = t.get("owners") or []
            if isinstance(owners, str):
                owners = [owners]
            if not owners:
                owners = ["未指派"]
            status = t.get("status")
            if status not in ("done", "partial", "doing", "stuck"):
                status = "doing"
            norm.append({
                "title": title[:80],
                "owners": [str(o) for o in owners][:6],
                "status": status,
                "due": (t.get("due") or "")[:40],
                "basis": (t.get("basis") or "")[:60],
                "note": (t.get("note") or "")[:200],
                "raw_status": "",
            })
        label = (payload.get("label") or "").strip() or ("外部推送 " + datetime.date.today().isoformat())
        source = (payload.get("source") or "api")
        try:
            res = ingest_tasks(label, norm, payload.get("people"), source,
                               payload.get("text", ""), use_llm=bool(payload.get("use_llm")))
        except Exception as e:
            return self._send(500, {"error": str(e)})
        if res.get("error"):
            return self._send(422, res)
        return self._send(200, res)

    # ------------------------------------------------------------------
    # 早报主动推送到企业微信群（群机器人 Webhook）
    # ------------------------------------------------------------------
    def _wecom_push(self, raw):
        wc = load_wecom_config()
        webhook = wc.get("group_webhook", "")
        if not webhook:
            return self._send(400, {"error": "未配置 group_webhook（群机器人 Webhook 地址）"})
        try:
            payload = json.loads(raw)
        except Exception as e:
            return self._send(400, {"error": f"bad payload: {e}"})
        text = (payload.get("text") or "").strip()
        if not text:
            # 没给文本则取最新早报自动拼一段
            b = get_briefing()
            if b:
                k = b.get("kpis", {})
                text = "【AI-CEO 经营早报】%s\n闭环率 %s，进行中 %s，卡住 %s。\n%s" % (
                    b.get("label", ""), k.get("close_rate", "-"), k.get("doing", "-"),
                    k.get("stuck", "-"), b.get("summary", ""))
        if not text:
            return self._send(400, {"error": "无可推送内容"})
        body = json.dumps({"msgtype": "text", "text": {"content": text}}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(webhook, data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
            return self._send(200, {"ok": True, "wecom": obj})
        except Exception as e:
            return self._send(502, {"error": "推送失败：%s" % e})

    def log_message(self, *args):
        pass


def main():
    init_db()
    # 部署环境注入 PORT，本地回退到 argv / 8000
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8000))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"AI-CEO 经营早报已启动： http://0.0.0.0:{port}")
    print("上传接口： POST /api/upload  (filename + data base64，可选 use_llm=true)")
    print("前端页面： GET /   大模型配置： 页面内「大模型配置」面板")
    cfg = L.load_config()
    if cfg["enabled"]:
        print(f"LLM 已启用： {cfg['provider_label']} / {cfg['model']}（抽取:{cfg['use_for_extract']} 增强:{cfg['use_for_summary']}）")
    else:
        print("LLM 未配置，全程规则分析（离线可跑）。进入页面「大模型配置」即可启用。")
    # 长连接：若上次保存时勾选了「保存后自动连接」，进程重启后自动恢复，避免静默断连
    try:
        wc = load_wecom_config()
        if wc.get("bot_enabled") and wc.get("bot_id") and wc.get("bot_secret"):
            threading.Thread(target=bot_start, daemon=True).start()
            print("企微长连接：已按保存的配置自动恢复连接")
    except Exception as e:
        print("企微长连接自动恢复失败：%s" % e)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
