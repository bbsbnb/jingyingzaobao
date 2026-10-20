"""
parser.py — 任务清单文件解析层（零第三方依赖）

支持格式（按文件真实内容判型，不单看扩展名）：
  - OOXML ZIP 容器：.docx / .xlsx（.xlsm 亦可）
  - OLE2 复合文档：.xls 走自带零依赖 xls_reader（BIFF8）
  - 纯文本：.csv / .tsv / .txt（支持 | 或 \\t 分隔的表格写法）

  ⚠ 魔数判型：真实业务中常见把 .xls 直接改名为 .xlsx 的做法，
    此时若按扩展名走 OOXML 解析必然失败；本层先读文件头，
    按 OLE2 / ZIP / TEXT 分流，扩展名只用于选择文本子类型与提示。

输出统一结构：
  {
    "source": "docx" | "xlsx" | "xls" | "csv" | "txt",
    "tasks":  [ {title, owners:[], status, due, basis, note}, ... ],
    "people": [ {name, total, done, partial, stuck, doing, note}, ... ],  # 仅当源文件含责任分布表
    "text":   "全文（用于 LLM 抽取兜底）"
  }

状态归一化枚举：done(已闭环) / partial(收尾中) / doing(进行中) / stuck(卡住未反馈)
"""

import io
import re
import csv
import zipfile
import json
from xml.etree import ElementTree as ET

try:
    import xls_reader          # 自带零依赖 .xls 读取器
except ImportError:            # 模块缺失时 .xls 走降级提示，不影响其它格式
    xls_reader = None

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ----------------------------------------------------------------------------
# 低层抽取：OOXML 表格 / 文本
# ----------------------------------------------------------------------------
def _extract_docx(path):
    paragraphs = []
    tables = []
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    for tbl in root.iter(W + "tbl"):
        rows = []
        for tr in tbl.findall(W + "tr"):
            cells = [
                "".join(t.text or "" for t in tc.iter(W + "t"))
                for tc in tr.findall(W + "tc")
            ]
            rows.append([c.strip() for c in cells])
        if rows:
            tables.append(rows)
    paragraphs = [
        "".join(t.text or "" for t in p.iter(W + "t")) for p in root.iter(W + "p")
    ]
    return paragraphs, tables


def _extract_xlsx(path):
    tables = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            r = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in r.findall(S + "si"):
                txt = "".join(
                    t.text or ""
                    for t in si.iter(
                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
                    )
                )
                shared.append(txt)
        sheet_files = sorted(
            n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        for sf in sheet_files:
            r = ET.fromstring(z.read(sf))
            rows = []
            for row in r.iter(S + "row"):
                cells = []
                for c in row.findall(S + "c"):
                    t = c.get("t")
                    v = c.find(S + "v")
                    if t == "s" and v is not None:
                        idx = int(v.text)
                        cells.append(shared[idx] if idx < len(shared) else "")
                    elif v is not None:
                        cells.append(v.text or "")
                    else:
                        cells.append("")
                rows.append([c.strip() for c in cells])
            if rows:
                tables.append(rows)
    return tables


def _extract_xls(data):
    """.xls 是 OLE2 复合文档 + BIFF8 记录流，交给自带的零依赖读取器。

    返回结构与 _extract_xlsx 一致（sheets -> rows -> cells），读取失败返回空列表，
    由调用方给出「另存为 .xlsx」的明确提示，不静默吞掉问题。
    """
    if xls_reader is None:
        return []
    try:
        return xls_reader.read_xls(data)
    except Exception:
        return []


def _extract_text_tables(text):
    """把纯文本（或 | 、\\t 分隔的表格写法）转成表格列表。"""
    tables = []
    lines = [ln.rstrip() for ln in text.splitlines()]
    # 先尝试按 | 或 \t 识别连续表格块
    buf = []
    sep_re = re.compile(r"[\|\t]")
    for ln in lines:
        if ln.strip() == "":
            if len(buf) >= 2:
                tables.append(_split_rows(buf))
            buf = []
            continue
        if sep_re.search(ln):
            buf.append(ln)
        else:
            if len(buf) >= 2:
                tables.append(_split_rows(buf))
            buf = []
    if len(buf) >= 2:
        tables.append(_split_rows(buf))
    # 若没有任何表格，退回按行兜底（供 LLM 抽取）
    return tables


def _split_rows(buf):
    rows = []
    for ln in buf:
        if "\t" in ln:
            cells = ln.split("\t")
        else:
            cells = [c.strip() for c in ln.split("|")]
            # 去掉首尾因 | 产生的空单元格
            if cells and cells[0] == "":
                cells = cells[1:]
            if cells and cells[-1] == "":
                cells = cells[:-1]
        rows.append([c.strip() for c in cells])
    return rows


# ----------------------------------------------------------------------------
# 字段归一化
# ----------------------------------------------------------------------------
def normalize_status(text):
    s = (text or "").lower()
    if any(k in s for k in ["已按期收齐", "已完成", "已归档", "已交付", "已答", "收齐", "完成"]):
        return "done"
    if any(k in s for k in ["部分完成", "部分", "已召开", "召开", "讨论会", "回收未见", "无结论", "已开会", "开会", "收尾"]):
        return "partial"
    if any(k in s for k in ["进行中", "进行", "推进", "上报中", "起草中"]):
        return "doing"
    if any(k in s for k in ["未反馈", "未答复", "未见", "仅收到", "零回应", "未做", "未完成", "未提交"]):
        return "stuck"
    return "doing"  # 默认视为进行中，避免误报警


def split_owners(text):
    if not text:
        return []
    t = re.sub(r"[（(][^）)]*[）)]", "", text)  # 去括号注释
    # 仅按中文/英文标点切分，避免按空格拆碎中文姓名
    parts = re.split(r"[、，,；;/和]+", t)
    return [p.strip() for p in parts if p.strip()]


def parse_due_md(due):
    """从时限文本提取 (月,日)，用于逾期判定。"""
    m = re.search(r"(\d{1,2})[月/](\d{1,2})", due or "")
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


# ----------------------------------------------------------------------------
# 表格识别
# ----------------------------------------------------------------------------
def _col_index(header_cells, *keywords):
    """返回第一个命中任一关键词的列下标，否则 None。"""
    for i, c in enumerate(header_cells):
        for kw in keywords:
            if kw in (c or ""):
                return i
    return None


# 表头行未必在第 1 行：周计划表常在首行放「公司名 + 周次 + 日期」的标题行。
_HEADER_SCAN_ROWS = 6
_HEADER_KWS = (
    "计划工作内容", "工作内容", "任务内容", "任务", "事项", "工作",
    "责任人", "负责", "承接", "执行", "跟进人", "归属",
    "完成状态", "状态", "进度",
    "计划完成时间", "时限", "截止", "时间", "期限",
    "依据", "来源", "出处", "为什么",
    "验收标准", "验收", "怎么做", "备注", "说明", "观察",
)
# 计数与汇总类表头不能当任务列，否则「承接任务数」会被误当成任务描述
_JOB_EXCLUDE_KWS = ("涉及任务", "任务数", "数量", "计数", "合计", "占比", "比率", "人次")


def _find_header_index(table):
    """在前若干行里挑出最像表头的一行，返回下标；挑不出返回 None。"""
    best, best_score = None, 0
    for i, row in enumerate(table[:_HEADER_SCAN_ROWS]):
        if sum(1 for c in row if (c or "").strip()) < 2:
            continue          # 标题行常只有 1 个有效格（跨列合并），不作为表头候选
        score = 0
        for c in row:
            c = c or ""
            if any(kw in c for kw in _HEADER_KWS):
                score += 1
        if score > best_score:
            best, best_score = i, score
    # 阈值取 2：既能选中「任务 + 责任人」这类极简表头，又不会误选数据行
    return best if best_score >= 2 else None


def _job_col(header):
    """定位「逐条任务」文本列，排除计数类与汇总类表头。"""
    for i, c in enumerate(header):
        c = c or ""
        if any(k in c for k in _JOB_EXCLUDE_KWS):
            continue
        if any(k in c for k in ("计划工作内容", "工作内容", "任务内容", "任务", "事项", "工作")):
            return i
    return None


def _owner_col(header):
    return _col_index(header, "责任人", "负责", "承接", "执行", "跟进人", "归属")


def _is_task_table(table):
    """判定是否为「逐条任务 + 责任人」的任务表。

    完成状态列不是必需项：周计划安排这类派单表天然没有状态列，责任到人才是关键，
    状态缺失时统一回落「进行中」；再用任务列的文本长度拦住责任分布表这类计数表。
    """
    hi = _find_header_index(table)
    if hi is None:
        return False
    header = table[hi]
    ti = _job_col(header)
    if ti is None or _owner_col(header) is None:
        return False
    good = 0
    for row in table[hi + 1:]:
        if not any(row):
            continue
        if len(((row[ti] if ti < len(row) else "") or "").strip()) >= 4:
            good += 1
    return good >= 1


def _is_people_table(table):
    hi = _find_header_index(table)
    if hi is None:
        return False
    header = table[hi]
    return (
        _col_index(header, "责任人", "姓名", "成员") is not None
        and _col_index(header, "承接任务数", "承接", "任务数") is not None
    )


_NOTE_LIMIT = 120


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def parse_tasks_from_tables(tables):
    tasks = []
    tid = 0
    for table in tables:
        if not _is_task_table(table):
            continue
        hi = _find_header_index(table)
        header = table[hi]
        ti = _job_col(header)
        oi = _owner_col(header)
        si = _col_index(header, "完成状态", "状态", "进度")
        di = _col_index(header, "计划完成时间", "时限", "截止", "时间", "期限", "要求")
        bi = _col_index(header, "为什么", "依据", "来源", "出处")
        ni = _col_index(header, "说明", "备注", "观察", "关键观察", "注")
        vi = _col_index(header, "验收标准", "验收")
        wi = _col_index(header, "怎么做", "执行方式")
        for row in table[hi + 1:]:
            title = _cell(row, ti)
            if not title or title.isdigit():
                continue  # 跳过序号行、空行与表尾说明
            status_raw = _cell(row, si)
            owners_raw = _cell(row, oi)
            # 备注聚合：验收标准优先，其次备注，做法垫底；总长封顶避免撑爆看板
            bits = []
            verify = _cell(row, vi)
            if verify:
                bits.append("验收：" + verify)
            note_raw = _cell(row, ni)
            if note_raw:
                bits.append(note_raw)
            howto = _cell(row, wi)
            if howto:
                bits.append("做法：" + howto)
            tid += 1
            tasks.append(
                {
                    "id": tid,
                    "title": title,
                    "owners": split_owners(owners_raw),
                    "status": normalize_status(status_raw),
                    "due": _cell(row, di),
                    "basis": _cell(row, bi),
                    "note": " ｜ ".join(bits)[:_NOTE_LIMIT],
                    "raw_status": status_raw,
                }
            )
    return tasks


def parse_people_from_tables(tables):
    people = []
    for table in tables:
        if not _is_people_table(table):
            continue
        hi = _find_header_index(table)
        header = table[hi]
        ni = _col_index(header, "责任人", "姓名", "成员")
        ti = _col_index(header, "承接任务数", "承接", "任务数")
        oki = _col_index(header, "已完成")
        pi = _col_index(header, "部分")
        sti = _col_index(header, "未反馈", "卡住", "未完成")
        doi = _col_index(header, "进行中")
        notei = _col_index(header, "关键观察", "观察", "备注", "说明")

        def num(v, default=0):
            try:
                return int(float(v))
            except Exception:
                # 处理 "1 / 1" 这类合并列
                if v and "/" in v:
                    parts = [p for p in re.split(r"[/\s]+", v) if p.replace(".", "").isdigit()]
                    if parts:
                        return sum(int(float(p)) for p in parts)
                return default

        for row in table[hi + 1:]:
            if not any(row):
                continue
            name = row[ni].strip() if ni is not None and ni < len(row) else ""
            if not name or name in ("责任人", "姓名", "合计", "总计", "小计"):
                continue
            # 「部分 / 未反馈」这类合并列：按斜杠拆成两个口径，避免同一数字被重复计入
            if pi is not None and sti is not None and pi == sti and pi < len(row):
                cell = row[pi] or ""
                parts = [x for x in re.split(r"[/\s]+", cell) if x != ""]
                partial_v = num(parts[0]) if len(parts) > 0 else 0
                stuck_v = num(parts[1]) if len(parts) > 1 else 0
            else:
                partial_v = num(row[pi]) if pi is not None and pi < len(row) else 0
                stuck_v = num(row[sti]) if sti is not None and sti < len(row) else 0
            p = {
                "name": name,
                "total": num(row[ti]) if ti is not None and ti < len(row) else 0,
                "done": num(row[oki]) if oki is not None and oki < len(row) else 0,
                "partial": partial_v,
                "stuck": stuck_v,
                "doing": num(row[doi]) if doi is not None and doi < len(row) else 0,
                "note": (row[notei].strip() if notei is not None and notei < len(row) else ""),
            }
            people.append(p)
    return people


# ----------------------------------------------------------------------------
# 变体结构识别：汇总式任务表 / 5W2H 派单块
# ----------------------------------------------------------------------------
_ROLE_TAIL = re.compile(
    r"(已答|已收|已提交|已完成|已回复|已跟进|未反馈|未答复|未跟进|未答|"
    r"零回应|无回应|无回|无输出|无记录|缺位|已报|报送)$"
)
_NAME_NOISE = re.compile(r"(各|等|全部|全员|相关|指定|部门|小组|团队)")


def _split_items(text):
    """按顿号/逗号/分号切分，忽略括号内的分隔符。"""
    out, buf, depth = [], [], 0
    for ch in text or "":
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if ch in "、，,；;/" and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [x for x in out if x]


def _clean_owners(raw):
    """从 5W2H「Who」文本抽取责任人：去角色后缀、去「报/报送」前缀、滤掉非人名。"""
    res = []
    for o in split_owners(raw):
        o = re.sub(r"[。．\.、,，;；：:！!？?\s]+$", "", o).strip()
        o = re.sub(r"^(报|报送|并报|同步|报备|上报|抄送)", "", o).strip()
        o = re.sub(r"[。．\.、,，;；：:！!？?\s]+$", "", o).strip()
        if not o:
            continue
        if len(o) > 4 or _NAME_NOISE.search(o):
            continue
        if not re.fullmatch(r"[一-龥·]{2,4}", o):
            continue
        if o not in res:
            res.append(o)
    return res


def _names_in_annotation(ann):
    """从括注里抽人名，如「朱旭光已答、王武缺位」→ [朱旭光, 王武]。"""
    res = []
    for seg in re.split(r"[、，,；;/和与]+", ann or ""):
        seg = _ROLE_TAIL.sub("", seg.strip()).strip()
        if 2 <= len(seg) <= 4 and re.fullmatch(r"[一-龥·]+", seg) and seg not in res:
            res.append(seg)
    return res


def _parse_status_summary_tasks(tables):
    """解析「状态 × 涉及任务」汇总表：把拼接在一格里的任务名拆成逐条任务。"""
    tasks = []
    tid = 0
    for table in tables:
        if len(table) < 2:
            continue
        header = table[0]
        si = _col_index(header, "状态")
        ti = None
        for i, c in enumerate(header or []):
            c = c or ""
            if "涉及任务" in c or ("任务" in c and "数" not in c and "状态" not in c):
                ti = i
                break
        if si is None or ti is None or si == ti:
            continue
        # 仅当任务列确实是「多个任务拼在一格」才启用，避免误吃普通任务表
        if not any(
            len(_split_items(r[ti])) > 1
            for r in table[1:]
            if ti < len(r) and r[ti].strip()
        ):
            continue
        for row in table[1:]:
            if not any(row) or ti >= len(row) or si >= len(row):
                continue
            status_raw = (row[si] or "").strip()
            if not status_raw:
                continue
            for item in _split_items(row[ti]):
                m = re.match(r"^([^（(]+)[（(]([^）)]*)[）)]\s*$", item.strip())
                title, ann = (m.group(1).strip(), m.group(2).strip()) if m else (item.strip(), "")
                if len(title) < 2 or title.isdigit():
                    continue
                tid += 1
                tasks.append(
                    {
                        "id": tid,
                        "title": title[:60],
                        "owners": _names_in_annotation(ann),
                        "status": normalize_status(status_raw),
                        "due": "",
                        "basis": "汇总表·涉及任务",
                        "note": "",
                        "raw_status": status_raw,
                    }
                )
    return tasks


_TASK_HEAD = re.compile(r"^任务\s*[0-9０-９一二三四五六七八九十]+\s*[：:]\s*(.+)$")
_FIELD_HEAD = re.compile(
    r"^[•·∙▪●◦○*\-–—>]?\s*"
    r"(How much|What|Why|Who|When|Where|How)"
    r"\s*(?:[（(][^）)]*[）)])?\s*[：:]\s*(.*)$"
)


def _parse_5w2h_tasks(text):
    """解析 5W2H 派单块：「任务 N：标题」+ 后续 • What/Who/When/How much 条目。"""
    blocks = []
    cur = None
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = _TASK_HEAD.match(ln)
        if m:
            cur = {"title": m.group(1).strip(), "fields": {}}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        f = _FIELD_HEAD.match(ln)
        if f:
            key, val = f.group(1), f.group(2).strip()
            prev = cur["fields"].get(key, "")
            cur["fields"][key] = (prev + (" " if prev and val else "") + val).strip()
    tasks = []
    for i, b in enumerate(blocks):
        fld = b["fields"]
        owners = _clean_owners(fld.get("Who", ""))
        accept = fld.get("How much", "")
        tasks.append(
            {
                "id": i + 1,
                "title": re.sub(r"\s+", " ", b["title"])[:60],
                "owners": owners,
                "status": "doing",
                "due": fld.get("When", "").strip(),
                "basis": "5W2H 派单",
                "note": ("验收：" + accept[:80]) if accept else "",
                "raw_status": "本周拟定",
            }
        )
    return tasks


# ----------------------------------------------------------------------------
# 容器判型：按文件魔数识别真实格式，扩展名只作参考
#   （真实业务里常见「把 .xls 直接改名为 .xlsx」这类改名操作，
#     若只信扩展名，二进制内容会被送去 OOXML/ZIP 解析而必然失败）
# ----------------------------------------------------------------------------
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # Excel 97-2003(.xls) / 老版 Word(.doc)
_ZIP_MAGIC = b"PK\x03\x04"                            # OOXML(.docx/.xlsx/.pptx) 本质是 ZIP

_KNOWN_EXTS = (".docx", ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt")


def _ext_of(filename):
    lower = (filename or "").lower()
    for e in _KNOWN_EXTS:
        if lower.endswith(e):
            return e.lstrip(".")
    return ""


def _sniff_container(data: bytes):
    """按魔数判断真实容器：ole2 / zip / text。

    OLE2 只校验前 4 字节（d0cf11e0），后 4 字节为版本标记，个别工具会写非标准值。
    """
    if data[:8] == _OLE2_MAGIC or data[:4] == _OLE2_MAGIC[:4]:
        return "ole2"
    if data[:4] == _ZIP_MAGIC:
        return "zip"
    return "text"


def _zip_kind(data: bytes):
    """OOXML ZIP 内部判型：docx / xlsx / pptx / ''（未知）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
    except Exception:
        return ""
    for n in names:
        if n.startswith("word/"):
            return "docx"
        if n.startswith("xl/"):
            return "xlsx"
        if n.startswith("ppt/"):
            return "pptx"
    return ""


# ----------------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------------
def parse_file(filename, data: bytes):
    ext = _ext_of(filename)
    container = _sniff_container(data)
    text = ""
    tables = []
    source = ""
    ext_mismatch = ""   # 扩展名与真实格式不一致时给出的透明提示

    if container == "ole2":
        # OLE2 容器：Excel 97-2003(.xls) 或老版 Word(.doc)。先按 Excel 工作簿试读
        tables = _extract_xls(data) if xls_reader else []
        source = "xls"
        if tables and ext and ext not in ("xls",):
            ext_mismatch = (
                "该文件扩展名为 .%s，但内部实为 Excel 97-2003（.xls）二进制格式，"
                "已按 .xls 正常解析。" % ext
            )
    elif container == "zip":
        kind = _zip_kind(data)
        if kind == "docx":
            paras, tables = _extract_docx(io.BytesIO(data))
            text = "\n".join(paras)
            source = "docx"
        elif kind == "xlsx":
            tables = _extract_xlsx(io.BytesIO(data))
            source = "xlsx"
        else:
            # 未知 OOXML（如 pptx）：交给下方结构识别，识别不到即如实返回
            tables = _extract_xlsx(io.BytesIO(data))
            source = "xlsx"
        if tables and kind and ext and ext != kind:
            ext_mismatch = (
                "该文件扩展名为 .%s，但内部实为 %s 格式，已按 %s 正常解析。"
                % (ext, kind, kind)
            )
    else:
        # 纯文本：csv 走表格化，其余按 txt 处理
        if ext == "csv":
            text = data.decode("utf-8-sig", errors="ignore")
            rows = [r for r in csv.reader(io.StringIO(text)) if any((c or "").strip() for c in r)]
            # 整个 CSV 视为「一个表格」（表头 + 数据行），与 docx/xlsx 的 tables 结构对齐
            tables = [rows] if rows else []
            source = "csv"
        else:  # txt / 其他当作纯文本
            text = data.decode("utf-8-sig", errors="ignore")
            raw_lines = [ln for ln in text.splitlines() if ln.strip()]
            head_line = raw_lines[0] if raw_lines else ""
            if ext == "tsv" or (head_line and "\t" in head_line):
                # 制表符分隔：整份文件视为一张表（首行表头）
                rows = [ln.split("\t") for ln in raw_lines]
                tables = [rows] if rows else []
                source = "csv"
            else:
                tables = _extract_text_tables(text)
                source = "txt"

    tasks = parse_tasks_from_tables(tables)
    people = parse_people_from_tables(tables)
    mode = "table" if tasks else ""

    # 变体结构（报告式文档）：先试 5W2H 派单块，再试「状态 × 涉及任务」汇总表
    if not tasks and source in ("docx", "xlsx") and text.strip():
        tasks = _parse_5w2h_tasks(text)
        if tasks:
            mode = "5w2h"
        else:
            tasks = _parse_status_summary_tasks(tables)
            if tasks:
                mode = "summary"

    # 兜底仅对纯文本生效：docx / xlsx / csv 属结构化格式，识别不到任何任务结构时如实返回 0，
    # 由「AI 智能抽取」处理，避免把正文段落、建议清单误当任务灌入库
    heuristic = False
    hint = ""
    if not tasks:
        if source == "xls" and not tables:
            hint = (
                "该文件是 Excel 97-2003（.xls）二进制或老版 Word（.doc）格式，本次未能读出表格。"
                "若原文件是 .doc，请在 Word 中另存为 .docx；若是 Excel 文件，请在 Excel 中「另存为」"
                "（不是改扩展名）为 .xlsx 后重试。"
            )
        elif source == "txt" and text.strip():
            tasks = _heuristic_text_tasks(text)
            heuristic = bool(tasks)
            if tasks:
                mode = "text"
            else:
                hint = "未识别到含责任人的任务行。可勾选「AI 智能抽取」后重试，或改用表格清单。"
        else:
            hint = (
                "未识别到任务结构。本系统支持三类：①含「任务 / 责任人」列的任务表（完成状态列可选）；"
                "②5W2H 派单块（「任务 N：」+ Who/When）；③「状态 × 涉及任务」汇总表。"
                "若文件是纯文本纪要或以上均不匹配，请勾选「AI 智能抽取」后重试。"
            )

    # 派单计划表天然没有完成状态列：不算错误，但要让用户知道状态是按「进行中」登记的
    if tasks and not hint and not heuristic and all(
        not (t.get("raw_status") or "").strip() for t in tasks
    ):
        hint = (
            "本表未含完成状态列（属派单计划表），全部任务已按「进行中」登记；"
            "拿到完成反馈后再上传一次，即可体现闭环情况。"
        )

    # 扩展名与真实格式不一致（如 .xls 改名成 .xlsx）：透明告知，避免用户以为读错文件
    if ext_mismatch:
        hint = (ext_mismatch + (" " + hint if hint else "")).strip()

    return {
        "source": source,
        "tasks": tasks,
        "people": people,
        "text": text,
        "heuristic": heuristic,
        "mode": mode,
        "hint": hint,
    }


def _heuristic_text_tasks(text):
    """自由文本兜底：识别含责任人/状态关键词的行作为候选任务。"""
    tasks = []
    tid = 0
    owner_kw = re.compile(r"(责任人|负责[:：]|跟进|@|牵头|主责|执行)")
    status_kw = re.compile(r"(完成|反馈|进行|未反馈|收到|截止|时限|逾期)")
    # 报告式条目（项目符号、编号、5W 建议、小标题）不是派单，直接排除
    bullet_re = re.compile(r"^\s*(?:[•·∙▪●◦○*\-–—>]|\d{1,2}\s*[.、)])\s*")
    report_re = re.compile(
        r"(Who|What|When|Where|Why|How)\s*[（(]|验收标准|建议[:：]|【建议】|整改措施[:：]"
    )
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) < 4:
            continue
        if bullet_re.match(ln) or report_re.search(ln):
            continue
        if ln.endswith(("：", ":")):
            continue
        if owner_kw.search(ln) or status_kw.search(ln):
            # 尝试从行内抽取责任人与状态
            m = re.search(r"([一-龥]{2,4})[（(]?[^）)]*[）)]?\s*(负责|牵头|主责|执行|跟进)", ln)
            owners = split_owners(m.group(1)) if m else []
            if not owners:
                continue  # 责任到人是闭环前提：抽不到责任人的候选行直接丢弃，避免正文/汇总段污染
            st = "doing"
            if "未反馈" in ln or "仅收到" in ln:
                st = "stuck"
            elif "完成" in ln or "已反馈" in ln:
                st = "done"
            elif "进行" in ln:
                st = "doing"
            title = (ln[:40] + "…") if len(ln) > 40 else ln
            tid += 1
            tasks.append(
                {
                    "id": tid,
                    "title": title,
                    "owners": owners,
                    "status": st,
                    "due": "",
                    "basis": "",
                    "note": "自由文本兜底识别，建议用结构化表格上传以提高准确率",
                    "raw_status": "",
                }
            )
    return tasks


if __name__ == "__main__":
    import sys

    with open(sys.argv[1], "rb") as f:
        d = f.read()
    res = parse_file(sys.argv[1], d)
    print(json.dumps(res, ensure_ascii=False, indent=2))
