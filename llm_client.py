"""
llm_client.py — 可插拔大模型客户端（零第三方依赖，仅用标准库 urllib）

为什么要这层：上传「结构化任务表单」规则驱动已够用；但要从工作群聊天、
会议纪要、纯文本周报这些非结构化源里抽取行动项，必须用大模型。本模块
把大模型做成「可插拔、默认关闭、按需开启」，并统一以下能力：
  1. extract_actions(text, roster) 非结构化文本 -> 结构化行动项
  2. enhance_summary(briefing)     规则结论 -> 自然语言经营综述 + 督办焦点
  3. test_connection()             前端「连接测试」用

协议：所有国内主流大模型（DeepSeek / 通义 / 智谱 / 豆包 / Kimi）都走
OpenAI 兼容的 /chat/completions；Agnes 等未知端点用 custom 档位填 base_url
即可，不强依赖任何第三方 SDK。

安全红线（反幻觉三禁令，写进每个系统提示）：
  1. 责任人(owners)只能取「花名册」已有姓名，无法归属填 ["未指派"]，严禁编造。
  2. 每条判定（状态/时限/依据）必须有原文证据，evidence 字段逐字引用。
  3. 原文未出现的状态/时限/结论/数字一律不输出；宁可少抽，不可臆造。
"""

import os
import json
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))

ANTI_HALLUCINATION = """【反幻觉三禁令】（所有抽取与生成必须严格遵守）
1. 责任人(owners)只能取「花名册」中已有姓名；无法归属的必须填 ["未指派"]，严禁编造新姓名，也不得拆分或合并姓名。
2. 每条任务的判定（状态/时限/依据）都必须有原文证据支撑：在 evidence 字段逐字引用原文片段（不超过 40 字），不得凭印象补全。
3. 原文未出现的状态、时限、结论、量化数字，一律不输出；宁可少抽几条，不可臆造。"""

# 国内通用大模型预设（base_url 已是 /chat/completions 的公共前缀）
PRESETS = {
    "deepseek": {"label": "DeepSeek（深度求索）", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "qwen":     {"label": "通义千问（阿里）",     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "glm":      {"label": "智谱 GLM（清华）",      "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "doubao":   {"label": "豆包（字节火山引擎）",   "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-seed-1.6-250615"},
    "moonshot": {"label": "Kimi（月之暗面）",      "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "custom":   {"label": "自定义（如 Agnes 等 OpenAI 兼容端点）", "base_url": "", "model": ""},
}


# ----------------------------------------------------------------------------
# 配置读写：config.json（页面保存，优先） > 环境变量（兜底，不落盘）
# ----------------------------------------------------------------------------
def config_path():
    return os.path.join(BASE, "config.json")


def load_config():
    cfg = {}
    p = config_path()
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except Exception:
            cfg = {}
    # 环境变量兜底（不写盘）
    env_base = os.environ.get("CEO_LLM_BASE")
    env_key = os.environ.get("CEO_LLM_KEY")
    env_model = os.environ.get("CEO_LLM_MODEL")
    if env_key and not cfg.get("api_key"):
        cfg["api_key"] = env_key
    if env_base and not cfg.get("base_url"):
        cfg["base_url"] = env_base
    if env_model and not cfg.get("model"):
        cfg["model"] = env_model

    provider = cfg.get("provider", "custom")
    preset = PRESETS.get(provider, PRESETS["custom"])
    base_url = (cfg.get("base_url") or "").strip() or preset["base_url"]
    model = (cfg.get("model") or "").strip() or preset["model"]
    enabled = bool(cfg.get("enabled", bool(base_url and cfg.get("api_key"))))
    return {
        "provider": provider,
        "provider_label": preset["label"],
        "enabled": enabled,
        "base_url": base_url,
        "model": model,
        "api_key": cfg.get("api_key", ""),
        "use_for_extract": bool(cfg.get("use_for_extract", True)),
        "use_for_summary": bool(cfg.get("use_for_summary", True)),
    }


def save_config(new):
    cfg = {}
    p = config_path()
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except Exception:
            cfg = {}
    for k in ("provider", "api_key", "model", "base_url", "enabled", "use_for_extract", "use_for_summary"):
        if k in new and new[k] is not None:
            cfg[k] = new[k]
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return load_config()


def public_config(cfg=None):
    """对外返回配置但脱敏 api_key。"""
    c = dict(cfg or load_config())
    if c.get("api_key"):
        k = c["api_key"]
        c["api_key_masked"] = (k[:4] + "****" + k[-4:]) if len(k) > 8 else "****"
        c["api_key"] = ""
    return c


# ----------------------------------------------------------------------------
# 底层请求：OpenAI 兼容协议，零依赖
# ----------------------------------------------------------------------------
def _chat(system, user, cfg, temperature=0.1, timeout=30):
    if not (cfg.get("base_url") and cfg.get("api_key")):
        return None
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"] or "deepseek-chat",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        return obj["choices"][0]["message"]["content"]
    except Exception:
        return None


def _extract_json(text):
    if not text:
        return "{}"
    s = text.find("{")
    e = text.rfind("}")
    if s >= 0 and e >= 0 and e > s:
        return text[s:e + 1]
    return text


# ----------------------------------------------------------------------------
# 能力 1：非结构化文本 -> 行动项（JSON 强约束 + 反幻觉）
# ----------------------------------------------------------------------------
def extract_actions(text, roster=None, cfg=None):
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("use_for_extract")):
        return None
    roster = roster or []
    roster_txt = "、".join(roster) if roster else "（暂无可参照花名册，无法归属的一律填「未指派」）"
    system = (
        "你是企业任务抽取引擎，从工作群聊天记录、会议纪要或周报纯文本中抽取「行动项」。\n"
        + ANTI_HALLUCINATION
        + "\n输出严格 JSON，格式：{\"tasks\":[{\"title\":\"任务名(简短)\",\"owners\":[\"姓名\"],"
        "\"status\":\"done|partial|doing|stuck\",\"due\":\"时限原文或空串\",\"basis\":\"依据/来源\","
        "\"evidence\":\"判定该任务的原文片段(逐字,<=40字)\"}]}。"
        "status 取值：done=已闭环/已交付/已完成；partial=收尾中/已开会待回收/部分完成；"
        "doing=进行中/推进中；stuck=未反馈/仅收到/未答复。"
    )
    user = f"已知花名册（owners 只能取这些姓名）：{roster_txt}\n\n待抽取文本：\n{text[:12000]}"
    content = _chat(system, user, cfg, temperature=0.1, timeout=60)
    if not content:
        return None
    try:
        obj = json.loads(_extract_json(content))
        items = obj.get("tasks", []) or []
    except Exception:
        return None
    out = []
    tid = 0
    for t in items:
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or "").strip()
        if not title:
            continue
        raw_owners = t.get("owners") or []
        if isinstance(raw_owners, str):
            raw_owners = [raw_owners]
        owners = [o for o in raw_owners if o in roster] if roster else []
        if not owners:
            owners = ["未指派"]
        status = t.get("status")
        if status not in ("done", "partial", "doing", "stuck"):
            status = "doing"
        tid += 1
        out.append({
            "id": tid,
            "title": title[:80],
            "owners": owners[:6],
            "status": status,
            "due": (t.get("due") or "").strip()[:40],
            "basis": (t.get("basis") or "").strip()[:60],
            "note": "AI抽取·" + (t.get("evidence") or "").strip()[:60],
            "raw_status": "",
            "llm_extracted": True,
        })
    return out


# ----------------------------------------------------------------------------
# 能力 2：规则结论 -> 自然语言经营综述 + 督办焦点
# ----------------------------------------------------------------------------
def enhance_summary(briefing, cfg=None):
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("use_for_summary")):
        return briefing
    k = briefing.get("kpis", {})
    alerts = [a["title"] for a in briefing.get("alerts", [])]
    system = (
        "你是CEO经营分析助手。基于结构化任务数据，输出：\n"
        "1) summary：一句中文经营结论（<=80字），只陈述数据、不臆造、不献策；\n"
        "2) focus：今日最该督办的 2-3 件事（每条<=30字，动词开头）。\n"
        "严格 JSON：{\"summary\":\"...\",\"focus\":[\"...\"]}。\n"
        + ANTI_HALLUCINATION
    )
    user = json.dumps(
        {"kpis": k, "alerts": alerts, "summary_rule": briefing.get("summary", "")},
        ensure_ascii=False,
    )
    content = _chat(system, user, cfg, temperature=0.1, timeout=20)
    if not content:
        return briefing
    try:
        obj = json.loads(_extract_json(content))
        if obj.get("summary"):
            briefing["summary"] = obj["summary"][:200]
            briefing["llm_enhanced"] = True
        if isinstance(obj.get("focus"), list):
            briefing["focus"] = [str(x) for x in obj["focus"][:3]]
    except Exception:
        pass
    return briefing


# ----------------------------------------------------------------------------
# 能力 3：连接测试
# ----------------------------------------------------------------------------
def test_connection(cfg=None):
    cfg = cfg or load_config()
    # 用服务商预设补全 base_url / model（前端测试时未必传全）
    preset = PRESETS.get(cfg.get("provider"), PRESETS["custom"])
    base_url = (cfg.get("base_url") or "").strip() or preset["base_url"]
    model = (cfg.get("model") or "").strip() or preset["model"]
    cfg = dict(cfg)
    cfg["base_url"] = base_url
    cfg["model"] = model
    if not (cfg.get("base_url") and cfg.get("api_key")):
        return {"ok": False, "error": "未配置 base_url 或 api_key"}
    content = _chat("你是连接测试助手，请只回复 ok 两个字", "ping", cfg, temperature=0, timeout=15)
    if content is None:
        return {"ok": False, "error": "请求失败或超时（检查 base_url / api_key / 网络连通性）"}
    return {"ok": True, "echo": content[:60]}


if __name__ == "__main__":
    import sys
    c = load_config()
    print(json.dumps(public_config(c), ensure_ascii=False, indent=2))
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("连接测试：", test_connection())
