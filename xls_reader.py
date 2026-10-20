# -*- coding: utf-8 -*-
"""
xls_reader.py — 零第三方依赖读取 Excel 97-2003 工作簿（.xls）

.xls 是 OLE2 复合文档，正文为 BIFF8 记录流，与 .xlsx 的 ZIP + XML 完全不同。
本模块只做一件事：把每个工作表的单元格文本读出来，返回结构与
parser._extract_xlsx 完全一致，便于 parse_file 直接复用下游任务识别逻辑。

对外接口：
    read_xls(data: bytes) -> list[list[list[str]]]     # sheets -> rows -> cells

异常：
    XlsError   非 OLE2 容器、缺 Workbook 流、BIFF 版本不受支持等

设计约束：
  - 纯标准库（struct），不引入 xlrd / pandas / openpyxl
  - 只取值，不动格式、不做公式计算、不展开合并单元格
  - 遇到不认识的 record 直接跳过，绝不因此中断整份文件
"""

import struct

__all__ = ["read_xls", "XlsError"]

_OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF


class XlsError(Exception):
    """读取 .xls 失败。调用方应捕获后降级到其它解析路径。"""


# ---------------------------------------------------------------------------
# 第一层：OLE2 复合文档容器
# ---------------------------------------------------------------------------
class _Ole2(object):
    """够用即止的 OLE2 实现：定位并读出指定的流。"""

    def __init__(self, data):
        if not data or data[:8] != _OLE_SIG:
            raise XlsError("不是 OLE2 复合文档")
        if len(data) < 512:
            raise XlsError("文件过短，缺少 OLE2 头")
        self.data = data
        self.sector_size = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self.mini_size = 1 << struct.unpack_from("<H", data, 0x20)[0]
        self.fat_count = struct.unpack_from("<I", data, 0x2C)[0]
        self.dir_first = struct.unpack_from("<I", data, 0x30)[0]
        self.mini_cutoff = struct.unpack_from("<I", data, 0x38)[0] or 4096
        self.minifat_first = struct.unpack_from("<I", data, 0x3C)[0]
        self.difat_first = struct.unpack_from("<I", data, 0x44)[0]
        if not (512 <= self.sector_size <= 65536) or not (8 <= self.mini_size <= 4096):
            raise XlsError("扇区尺寸异常")
        self._read_fat()
        self._read_dir()
        self._read_minifat()

    # -- 扇区读取 ----------------------------------------------------------
    def _sector(self, idx):
        off = 512 + idx * self.sector_size
        if idx < 0 or off < 512 or off >= len(self.data):
            return b""
        return self.data[off:off + self.sector_size]

    def _mini_sector(self, idx):
        off = idx * self.mini_size
        if idx < 0 or off < 0 or off >= len(self.ministream):
            return b""
        return self.ministream[off:off + self.mini_size]

    def _chain(self, start, table):
        """按 FAT / MiniFAT 走链，返回扇区号列表。"""
        out = []
        sec = start
        guard = 0
        limit = len(table) + 16
        while sec < _ENDOFCHAIN and guard < limit:
            out.append(sec)
            if sec >= len(table):
                break
            sec = table[sec]
            guard += 1
        return out

    # -- FAT / 目录 / MiniFAT ---------------------------------------------
    def _read_fat(self):
        per = self.sector_size // 4
        difat = list(struct.unpack_from("<109I", self.data, 0x4C))
        sec = self.difat_first
        guard = 0
        while sec < _ENDOFCHAIN and guard < 65536:
            blk = self._sector(sec)
            if len(blk) < self.sector_size:
                break
            vals = struct.unpack_from("<%dI" % per, blk, 0)
            difat.extend(vals[:per - 1])
            sec = vals[per - 1]
            guard += 1
        fat = []
        got = 0
        for fs in difat:
            if fs >= _ENDOFCHAIN:
                continue
            blk = self._sector(fs)
            if len(blk) < self.sector_size:
                continue
            fat.extend(struct.unpack_from("<%dI" % per, blk, 0))
            got += 1
            if self.fat_count and got >= self.fat_count:
                break
        self.fat = fat

    def _read_dir(self):
        self.dirents = []
        self.root = None
        if self.dir_first >= _ENDOFCHAIN:
            return
        for si in self._chain(self.dir_first, self.fat):
            blk = self._sector(si)
            for k in range(0, len(blk) - 127, 128):
                e = blk[k:k + 128]
                try:
                    nlen = struct.unpack_from("<H", e, 0x40)[0]
                    etype = e[0x42]
                    start = struct.unpack_from("<I", e, 0x74)[0]
                    size = struct.unpack_from("<I", e, 0x78)[0]
                except struct.error:
                    continue
                if etype == 0 or nlen < 2 or nlen > 64:
                    continue
                name = e[:nlen - 2].decode("utf-16-le", "ignore")
                ent = {"name": name, "type": etype, "start": start, "size": size}
                self.dirents.append(ent)
                if etype == 5 and self.root is None:
                    self.root = ent

    def _read_minifat(self):
        self.minifat = []
        self.ministream = b""
        if self.minifat_first < _ENDOFCHAIN:
            per = self.sector_size // 4
            for si in self._chain(self.minifat_first, self.fat):
                blk = self._sector(si)
                if len(blk) < self.sector_size:
                    continue
                self.minifat.extend(struct.unpack_from("<%dI" % per, blk, 0))
        if self.root and self.root["size"] > 0 and self.root["start"] < _ENDOFCHAIN:
            self.ministream = b"".join(
                self._sector(s) for s in self._chain(self.root["start"], self.fat)
            )

    # -- 读流 --------------------------------------------------------------
    def read_stream(self, name):
        ent = None
        for d in self.dirents:
            if d["type"] == 2 and d["name"] == name:
                ent = d
                break
        if ent is None or ent["size"] <= 0:
            return None
        if ent["size"] < self.mini_cutoff and self.minifat:
            buf = b"".join(
                self._mini_sector(s) for s in self._chain(ent["start"], self.minifat)
            )
        else:
            buf = b"".join(self._sector(s) for s in self._chain(ent["start"], self.fat))
        return buf[:ent["size"]]


# ---------------------------------------------------------------------------
# 第二层：BIFF8 记录流
# ---------------------------------------------------------------------------
_RT_EOF = 0x000A
_RT_CONTINUE = 0x003C
_RT_BOF = 0x0809
_RT_BOUNDSHEET = 0x0085
_RT_SST = 0x00FC
_RT_LABELSST = 0x00FD
_RT_LABEL = 0x0204
_RT_RSTRING = 0x00D6
_RT_NUMBER = 0x0203
_RT_RK = 0x027E
_RT_MULRK = 0x00BD
_RT_FORMULA = 0x0006
_RT_STRING = 0x0207
_RT_BOOLERR = 0x0205

_SUBSTREAM_WORKSHEET = 0x0010
_STR_PENDING = object()


def _iter_records(stream):
    """顺序切分 BIFF 记录，产出 (pos, rectype, payload)。"""
    pos = 0
    n = len(stream)
    while pos + 4 <= n:
        rt, rl = struct.unpack_from("<HH", stream, pos)
        yield pos, rt, stream[pos + 4:pos + 4 + rl]
        pos += 4 + rl


def _unpack_rk(v):
    """RK 编码：位 0 表示除以 100，位 1 表示 30 位整数，否则是 double 的高 30 位。"""
    if v & 0x02:
        n = v >> 2
        if v & 0x80000000:                 # 30 位补码，符号扩展到负数
            n -= 0x40000000
        num = float(n)
    else:
        buf = struct.pack("<I", v & 0xFFFFFFFC) + b"\x00\x00\x00\x00"
        num = struct.unpack("<d", buf)[0]
    if v & 0x01:
        num /= 100.0
    return num


def _fmt_number(num):
    """数值转文本：整数不留小数点，避免把 2026 显示成 2026.0。"""
    try:
        if num == int(num) and abs(num) < 1e15:
            return str(int(num))
    except (ValueError, OverflowError):
        pass
    return "%.10g" % num


def _parse_biff_string(data, off=0):
    """解析单条记录内的 BIFF8 Unicode 字符串，返回 (文本, 新偏移)。"""
    if off + 3 > len(data):
        return "", len(data)
    cch = struct.unpack_from("<H", data, off)[0]
    grbit = data[off + 2]
    off += 3
    c_run = 0
    cb_ext = 0
    if grbit & 0x08:
        if off + 2 > len(data):
            return "", len(data)
        c_run = struct.unpack_from("<H", data, off)[0]
        off += 2
    if grbit & 0x04:
        if off + 4 > len(data):
            return "", len(data)
        cb_ext = struct.unpack_from("<i", data, off)[0]
        off += 4
    if grbit & 0x01:
        s = data[off:off + cch * 2].decode("utf-16-le", "ignore")
        off += cch * 2
    else:
        s = data[off:off + cch].decode("latin-1")
        off += cch
    off += c_run * 4
    if cb_ext > 0:
        off += cb_ext
    return s, off


def _parse_sst(payloads):
    """解析 SST 及紧随其后的 CONTINUE 记录，返回共享字符串列表。

    难点：字符数据可以跨记录边界。断在字符数据中间时，下一条 CONTINUE
    的首字节换作新的 grbit（只表示该续段是压缩还是 UTF-16）；断在
    RichText / ExtRst 内则直接续读，没有额外字节。
    """
    if not payloads or len(payloads[0]) < 8:
        return []
    unique = struct.unpack_from("<I", payloads[0], 4)[0]
    recs = list(payloads)
    state = [0, 8]                     # [记录下标, 记录内偏移]

    def norm():
        while state[0] < len(recs) and state[1] >= len(recs[state[0]]):
            state[0] += 1
            state[1] = 0

    def read_raw(n):
        """读 n 字节，跨记录直接续接（无 grbit），用于定长头与 RichText / ExtRst。"""
        out = bytearray()
        while n > 0:
            norm()
            if state[0] >= len(recs):
                break
            cur = recs[state[0]]
            take = min(n, len(cur) - state[1])
            out += cur[state[1]:state[1] + take]
            state[1] += take
            n -= take
        return bytes(out)

    def read_u16():
        b = read_raw(2)
        return struct.unpack("<H", b)[0] if len(b) == 2 else 0

    def read_i32():
        b = read_raw(4)
        return struct.unpack("<i", b)[0] if len(b) == 4 else 0

    def read_chars(cch, compressed):
        """读 cch 个字符；每次进入字符续段时，消费新记录首字节作 grbit。"""
        chunks = []
        left = cch
        guard = 0
        while left > 0 and guard < 200000:
            guard += 1
            if state[0] >= len(recs):
                break
            cur = recs[state[0]]
            width = 1 if compressed else 2
            if (len(cur) - state[1]) // width <= 0:
                state[0] += 1
                state[1] = 1
                if state[0] < len(recs):
                    compressed = not (recs[state[0]][0] & 0x01)
                continue
            room = (len(cur) - state[1]) // width
            take = min(left, room)
            seg = cur[state[1]:state[1] + take * width]
            chunks.append(
                seg.decode("latin-1") if compressed else seg.decode("utf-16-le", "ignore")
            )
            state[1] += take * width
            left -= take
            if left > 0:
                state[0] += 1
                state[1] = 1
                if state[0] < len(recs):
                    compressed = not (recs[state[0]][0] & 0x01)
        return "".join(chunks)

    strings = []
    guard = 0
    while len(strings) < unique and guard < 1000000:
        guard += 1
        norm()
        if state[0] >= len(recs):
            break
        cur = recs[state[0]]
        if state[1] + 3 > len(cur):
            state[0] += 1
            state[1] = 0
            continue
        cch = struct.unpack_from("<H", cur, state[1])[0]
        grbit = cur[state[1] + 2]
        state[1] += 3
        c_run = read_u16() if grbit & 0x08 else 0
        cb_ext = read_i32() if grbit & 0x04 else 0
        strings.append(read_chars(cch, not (grbit & 0x01)))
        if c_run:
            read_raw(c_run * 4)
        if cb_ext > 0:
            read_raw(cb_ext)
    return strings


def _boundsheet_name(data):
    if len(data) < 8:
        return ""
    cch = data[6]
    grbit = data[7]
    raw = data[8:]
    if grbit & 0x01:
        return raw[:cch * 2].decode("utf-16-le", "ignore")
    return raw[:cch].decode("latin-1")


def _formula_value(data):
    """返回 (row, col, value)；value 为 str / _STR_PENDING / None。"""
    if len(data) < 20:
        return None
    row, col = struct.unpack_from("<HH", data, 0)
    res = data[6:14]
    head = res[0]
    if head == 0x00:
        return row, col, _fmt_number(struct.unpack("<d", res)[0])
    if head == 0x01:
        return row, col, "TRUE" if res[2] else "FALSE"
    if head in (0x02, 0x03):
        return row, col, ""            # 错误值 / 空串，忽略
    return row, col, _STR_PENDING      # 文本结果在下一条 STRING 记录里


def _parse_biff(stream):
    recs = list(_iter_records(stream))

    sst = []
    for idx, (_, rt, data) in enumerate(recs):
        if rt == _RT_SST:
            conts = [data]
            j = idx + 1
            while j < len(recs) and recs[j][1] == _RT_CONTINUE:
                conts.append(recs[j][2])
                j += 1
            sst = _parse_sst(conts)
            break

    sheets = []                        # 每项：{(row, col): text}
    cur = None
    pending_str_cell = None

    def put(row, col, val):
        if cur is None or val is None or val == "":
            return
        cur[(row, col)] = val

    for _, rt, data in recs:
        if rt == _RT_BOF:
            subtype = struct.unpack_from("<H", data, 2)[0] if len(data) >= 4 else 0
            if subtype == _SUBSTREAM_WORKSHEET:
                cur = {}
                sheets.append(cur)
            else:
                cur = None
            pending_str_cell = None
            continue
        if rt == _RT_EOF:
            cur = None
            pending_str_cell = None
            continue
        if cur is None:
            continue

        try:
            if rt == _RT_LABELSST and len(data) >= 10:
                row, col = struct.unpack_from("<HH", data, 0)
                isst = struct.unpack_from("<I", data, 6)[0]
                put(row, col, sst[isst] if isst < len(sst) else "")
            elif rt in (_RT_LABEL, _RT_RSTRING) and len(data) >= 8:
                row, col = struct.unpack_from("<HH", data, 0)
                s, _ = _parse_biff_string(data, 6)
                put(row, col, s.strip())
            elif rt == _RT_NUMBER and len(data) >= 14:
                row, col = struct.unpack_from("<HH", data, 0)
                put(row, col, _fmt_number(struct.unpack_from("<d", data, 6)[0]))
            elif rt == _RT_RK and len(data) >= 10:
                row, col = struct.unpack_from("<HH", data, 0)
                put(row, col, _fmt_number(_unpack_rk(struct.unpack_from("<I", data, 6)[0])))
            elif rt == _RT_MULRK and len(data) >= 12:
                row, col_first = struct.unpack_from("<HH", data, 0)
                body = data[4:len(data) - 2]
                for k in range(0, len(body) - 5, 6):
                    rk = struct.unpack_from("<I", body, k + 2)[0]
                    put(row, col_first + k // 6, _fmt_number(_unpack_rk(rk)))
            elif rt == _RT_BOOLERR and len(data) >= 8:
                row, col = struct.unpack_from("<HH", data, 0)
                put(row, col, "" if data[7] else ("TRUE" if data[6] else "FALSE"))
            elif rt == _RT_FORMULA:
                got = _formula_value(data)
                if got:
                    row, col, val = got
                    if val is _STR_PENDING:
                        pending_str_cell = (row, col)
                    else:
                        put(row, col, val)
                        pending_str_cell = None
            elif rt == _RT_STRING:
                if pending_str_cell:
                    s, _ = _parse_biff_string(data, 0)
                    put(pending_str_cell[0], pending_str_cell[1], s.strip())
                    pending_str_cell = None
        except (struct.error, IndexError, ValueError):
            continue

    out = []
    for cells in sheets:
        if not cells:
            continue
        max_row = max(r for r, _ in cells)
        max_col = max(c for _, c in cells)
        rows = [[cells.get((r, c), "") for c in range(max_col + 1)] for r in range(max_row + 1)]
        while rows and not any((x or "").strip() for x in rows[-1]):
            rows.pop()
        if rows:
            out.append(rows)
    return out


def read_xls(data):
    """读取 .xls 字节流，返回 sheets -> rows -> cells（字符串）。"""
    if not isinstance(data, (bytes, bytearray)):
        raise XlsError("read_xls 只接受 bytes")
    raw = bytes(data)
    ole = _Ole2(raw)
    stream = ole.read_stream("Workbook") or ole.read_stream("Book")
    if not stream:
        raise XlsError("未找到 Workbook 流")
    if len(stream) < 8:
        raise XlsError("Workbook 流过短")
    rt, rl = struct.unpack_from("<HH", stream, 0)
    if rt != _RT_BOF or rl < 4:
        raise XlsError("Workbook 流未以 BOF 记录开头")
    ver = struct.unpack_from("<H", stream, 4)[0]
    if ver not in (0x0600, 0x0500, 0x0400):
        raise XlsError("不支持的 BIFF 版本 0x%04X" % ver)
    return _parse_biff(stream)
