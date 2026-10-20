"""
analyzer.py — 任务闭环分析层（规则驱动，可离线运行）

输入：parse_file() 返回的结构（tasks / people / text）
输出：一份完整的「AI-CEO 经营早报」JSON，包含
  - kpis      经营健康指数 / 行动闭环率 / 风险项 / 逾期行动 ...
  - alerts    异常警报（风险红 / 关注黄），每条带处置建议与关联任务
  - people    人的信号（承接分布 + 零回应/单点依赖标注）
  - board     四态泳道（已闭环 / 收尾中 / 进行中 / 卡住未反馈）
  - summary   一句话结论（规则生成，可经 LLM 增强）
  - tasks     全量任务（带回执状态，供追问交互）

设计原则（沿用前期复刻方案）：
  1. 排序交给规则，模型只负责抽取与措辞 —— 本层完全是规则。
  2. 反幻觉：所有结论必须来自可点回的任务/依据字段，不臆造。
  3. 闭环率 = (已闭环 + 0.5*收尾中) / 总数，验收人≠执行人由录入约束保证。
"""

from parser import normalize_status, parse_due_md

# AI 类任务判定关键词（用于单点依赖告警）
AI_KEYWORDS = ["ai", "智能体", "agent", "合同审查", "知识库", "核图", "横道图", "机器人"]


def _is_ai_task(t):
    s = (t["title"] + " " + t.get("note", "") + " " + t.get("raw_status", "")).lower()
    return any(k in s for k in AI_KEYWORDS)


def owner_cnt_pct(ratio):
    return f"{round(ratio * 100)}%"


def analyze(tasks, people=None, meta=None):
    meta = meta or {}
    total = len(tasks)
    if total == 0:
        return _empty_briefing(meta)

    # ---- 计数 ----
    cnt = {"done": 0, "partial": 0, "doing": 0, "stuck": 0}
    for t in tasks:
        cnt[t["status"]] = cnt.get(t["status"], 0) + 1

    closure_rate = round((cnt["done"] + 0.5 * cnt["partial"]) / total * 100, 1)
    done_rate = round(cnt["done"] / total * 100, 1)
    stuck_ratio = cnt["stuck"] / total

    # ---- 逾期判定 ----
    # 参考日期：优先用早报生成日期，否则用今天
    ref = meta.get("ref_date")  # (月,日)
    overdue = []
    for t in tasks:
        if t["status"] == "done":
            continue
        dm = parse_due_md(t.get("due", ""))
        if dm and ref and (dm[0] < ref[0] or (dm[0] == ref[0] and dm[1] < ref[1])):
            overdue.append(t["id"])

    # ---- 单点依赖 / 承接过度集中 ----
    ai_tasks = [t for t in tasks if _is_ai_task(t)]
    ai_owners = set()
    for t in ai_tasks:
        ai_owners.update(t["owners"])
    ai_concentrated = len(ai_tasks) >= 3 and len(ai_owners) <= 2
    # 通用集中度：单人承接占比过高
    owner_cnt = {}
    for t in tasks:
        for o in t["owners"]:
            if o in ("全员",):
                continue
            owner_cnt[o] = owner_cnt.get(o, 0) + 1
    top_owner = max(owner_cnt, key=owner_cnt.get) if owner_cnt else ""
    top_ratio = (owner_cnt[top_owner] / total) if owner_cnt else 0
    concentrated = ai_concentrated or top_ratio >= 0.33

    # ---- 人的信号（优先用文件自带责任分布表，否则按任务聚合）----
    people = _build_people(tasks, people, ai_owners)

    # ---- 异常警报 ----
    alerts = _build_alerts(tasks, cnt, stuck_ratio, concentrated, ai_tasks, ai_owners, overdue, top_owner, top_ratio, total)

    # ---- 经营健康指数 ----
    health = _health_index(closure_rate, stuck_ratio, concentrated, bool(overdue), meta)

    kpis = {
        "health": health,
        "closure_rate": closure_rate,
        "done_rate": done_rate,
        "risk_count": sum(1 for a in alerts if a["type"] == "risk"),
        "overdue": len(overdue),
        "total": total,
        "done": cnt["done"],
        "partial": cnt["partial"],
        "doing": cnt["doing"],
        "stuck": cnt["stuck"],
        "ai_single_point": len(ai_owners) if ai_concentrated else (1 if concentrated else 0),
    }

    # ---- 四态泳道 ----
    board = {
        k: [
            {
                "id": t["id"],
                "title": t["title"],
                "owners": t["owners"],
                "due": t.get("due", ""),
                "basis": t.get("basis", ""),
                "note": t.get("note", ""),
                "asks": 0,
            }
            for t in tasks
            if t["status"] == k
        ]
        for k in ("done", "partial", "doing", "stuck")
    }

    # ---- 一句话结论 ----
    summary = _summary_sentence(kpis, alerts, cnt)

    return {
        "meta": meta,
        "kpis": kpis,
        "alerts": alerts,
        "people": people,
        "board": board,
        "summary": summary,
        "tasks": tasks,
        "overdue_ids": overdue,
    }


# ----------------------------------------------------------------------------
# 子模块
# ----------------------------------------------------------------------------
def _build_people(tasks, provided_people, ai_owners):
    agg = {}
    for t in tasks:
        for o in t["owners"]:
            if o in ("全员",):
                continue
            d = agg.setdefault(o, {"name": o, "total": 0, "done": 0, "partial": 0, "doing": 0, "stuck": 0, "note": ""})
            d["total"] += 1
            d[t["status"]] += 1
    # 用文件自带责任分布表的备注补充
    if provided_people:
        note_map = {p["name"]: p.get("note", "") for p in provided_people}
        for d in agg.values():
            if d["name"] in note_map and note_map[d["name"]]:
                d["note"] = note_map[d["name"]]
    # 规则补充备注
    for d in agg.values():
        if d["done"] == 0 and d["stuck"] > 0:
            d["note"] = (d["note"] + "；" if d["note"] else "") + "零回应（派单无回执）"
        if d["name"] in ai_owners:
            d["note"] = (d["note"] + "；" if d["note"] else "") + "AI 类任务承接人（单点依赖风险）"
    return sorted(agg.values(), key=lambda x: (-x["total"], x["name"]))


def _build_alerts(tasks, cnt, stuck_ratio, concentrated, ai_tasks, ai_owners, overdue, top_owner, top_ratio, total):
    alerts = []

    # 1. 零回执任务（已读未闭环）
    stuck = [t for t in tasks if t["status"] == "stuck"]
    if stuck:
        names = "、".join(f"{t['title'][:14]}（{('/'.join(t['owners']) or '未指派')}）" for t in stuck[:6])
        more = f"等共 {len(stuck)} 项" if len(stuck) > 6 else ""
        alerts.append({
            "type": "risk",
            "title": f"{len(stuck)} 项任务零回执（已读未闭环）",
            "detail": f"{names}{more}。多数止于「收到」，无交付物、无反馈、无时限，追责时无法举证，也无法区分「已做未报」与「未做」。建议本周会议第一议题清欠，逐项给出口径。",
            "related": [t["id"] for t in stuck],
        })

    # 2. 成果无载体（收尾中但结论未回收）
    no_carrier = [
        t for t in tasks
        if t["status"] == "partial"
        and any(k in (t["title"] + t.get("note", "") + t.get("raw_status", "")) for k in ["讨论", "会议", "结论", "召开", "回收未见", "无结论"])
    ]
    if no_carrier:
        names = "、".join(t["title"][:14] for t in no_carrier[:5])
        alerts.append({
            "type": "risk",
            "title": f"{len(no_carrier)} 项会议/讨论成果无载体",
            "detail": f"{names}。会已召开但结论表/纪要未进群，前序投入无法转化为制度条款。责任在流程（模板已下发未回收），建议指定汇总人点对点催办并留痕。",
            "related": [t["id"] for t in no_carrier],
        })

    # 3. 单点依赖 / 承接过度集中
    if concentrated:
        if ai_single_point := (len(ai_tasks) >= 3 and len(ai_owners) <= 2):
            alerts.append({
                "type": "watch",
                "title": f"单点依赖即知识风险（AI 类 {len(ai_tasks)} 项集中在 {len(ai_owners)} 人）",
                "detail": f"AI 类任务承接人仅 {('/'.join(sorted(ai_owners)))}，既是产能瓶颈也是知识风险——一旦这几人忙别的事，「AI 应用」整条线停摆。建议承接面扩到 5~6 人。",
                "related": [t["id"] for t in ai_tasks],
            })
        else:
            alerts.append({
                "type": "watch",
                "title": f"承接过度集中（{top_owner} 一人扛 {owner_cnt_pct(top_ratio)} 的任务）",
                "detail": f"{top_owner} 单人承接 {round(top_ratio*total)} 项任务，占比 {owner_cnt_pct(top_ratio)}，存在单点依赖与休假/离职风险。建议同类任务增设备份承接人。",
                "related": [t["id"] for t in tasks if top_owner in t["owners"]],
            })

    # 4. 派单/回收错配
    if stuck_ratio >= 0.3:
        alerts.append({
            "type": "watch",
            "title": f"派单密度与回收率不匹配（卡住占比 {round(stuck_ratio*100)}%）",
            "detail": "一次派发多份材料、涉及多人次，但有效回执少，多项派单止于「收到」。若不改「群内派单即视为已交办」的做法，后续仍会重复该结果。建议以看板状态为准。",
            "related": [t["id"] for t in stuck],
        })

    # 5. 逾期
    if overdue:
        names = "、".join(str(t["id"]) for t in tasks if t["id"] in overdue)
        alerts.append({
            "type": "risk",
            "title": f"{len(overdue)} 项行动已逾期（超时限未闭环）",
            "detail": f"任务编号 {names} 已超过承诺时限且未闭环，建议升级催办并要求在 T+1 内给出明确口径或交付物。",
            "related": list(overdue),
        })

    return alerts


def _health_index(closure_rate, stuck_ratio, ai_single_point, has_overdue, meta):
    score = (
        closure_rate / 100 * 0.60
        + (1 - stuck_ratio) * 0.25
        + (1 - (0.4 if ai_single_point else 0) - (0.3 if has_overdue else 0)) * 0.15
    )
    return round(score * 10, 1)


def _summary_sentence(kpis, alerts, cnt):
    parts = [
        f"本周派发 {kpis['total']} 项任务，",
        f"闭环 {kpis['done']} 项（加权闭环率 {kpis['closure_rate']}%），",
        f"收尾中 {kpis['partial']} 项、进行中 {kpis['doing']} 项、卡在未反馈 {kpis['stuck']} 项。",
    ]
    risk_titles = [a["title"] for a in alerts if a["type"] == "risk"]
    if risk_titles:
        parts.append("主要风险：" + "；".join(risk_titles[:3]) + "。")
    else:
        parts.append("暂无高风险项，重点维持闭环节奏。")
    return "".join(parts)


def _empty_briefing(meta):
    return {
        "meta": meta,
        "kpis": {"health": 0, "closure_rate": 0, "risk_count": 0, "overdue": 0,
                 "total": 0, "done": 0, "partial": 0, "doing": 0, "stuck": 0, "ai_single_point": 0},
        "alerts": [{"type": "watch", "title": "暂无任务数据", "detail": "上传包含任务清单的文档后自动生成。", "related": []}],
        "people": [],
        "board": {"done": [], "partial": [], "doing": [], "stuck": []},
        "summary": "尚未解析到任务，请上传周报/任务清单。",
        "tasks": [],
        "overdue_ids": [],
    }


# ----------------------------------------------------------------------------
# 跨周整合
# ----------------------------------------------------------------------------
def consolidate(weeks):
    """
    weeks: [{meta, kpis, tasks}] 按时间升序
    返回跨周趋势 + 未闭环滚存 + 人员一致性
    """
    if not weeks:
        return {"trend": [], "rollover": [], "people_consistency": []}

    trend = []
    for w in weeks:
        k = w["kpis"]
        trend.append({
            "label": w["meta"].get("label", ""),
            "total": k["total"],
            "closure_rate": k["closure_rate"],
            "done": k["done"],
            "stuck": k["stuck"],
        })

    # 滚存：上一周未闭环（非 done）的任务，若本周无同标题出现，则视为延续
    rollover = []
    for i in range(1, len(weeks)):
        prev = weeks[i - 1]["tasks"]
        curr_titles = {t["title"] for t in weeks[i]["tasks"]}
        for t in prev:
            if t["status"] != "done" and t["title"] not in curr_titles:
                rollover.append({
                    "from_week": weeks[i - 1]["meta"].get("label", ""),
                    "title": t["title"],
                    "owners": t["owners"],
                    "status": t["status"],
                    "note": "跨周仍未闭环，建议本周优先收口或正式关闭",
                })

    # 人员一致性：跨周都出现且都有未闭环的人
    name_stats = {}
    for w in weeks:
        for t in w["tasks"]:
            for o in t["owners"]:
                if o in ("全员",):
                    continue
                d = name_stats.setdefault(o, {"weeks": 0, "stuck": 0, "total": 0})
                d["weeks"] += 1
                d["total"] += 1
                if t["status"] == "stuck":
                    d["stuck"] += 1
    people_consistency = [
        {"name": n, "weeks": d["weeks"], "stuck": d["stuck"], "total": d["total"]}
        for n, d in name_stats.items()
        if d["weeks"] >= 2 and d["stuck"] > 0
    ]
    people_consistency.sort(key=lambda x: (-x["stuck"], -x["weeks"]))

    return {"trend": trend, "rollover": rollover, "people_consistency": people_consistency}
