# -*- coding: utf-8 -*-
"""天凤和雀魂共用的播报文案：四类消息一个骨架。

  #码 【对局开始】 天凤|雀魂 <场名> HH:MM     ← 订阅开局那条前面多一行"谁正在乱杀"
                                             ← 四家那一块上下各一条 ───── 分隔线
  #码【小局结算】HH:MM / #码【流局】HH:MM      ← 局名一行，下面逐家点数
  【对局完成】#码                              ← 先一句顺位点评，再逐家点数和点位
  #码【牌谱回放】…                             ← 雀魂没接上实时流时的兜底

点数一律是"变动前 -> 变动后（±变动）"，前后都取自服务端；括号里那列只印服务端
自己给的数（天凤 AGARI 的 owari 精算点位、雀魂 result 的 grading_score），取不到
就整列省掉，绝不按公式反推——三麻/魂天/比赛的点位口径不一样，猜出来的数像真的。
"""
from __future__ import annotations

import re
import time
import unicodedata

_MAX_HEADLINES = 2


def now_hm() -> str:
    """播报标题上的时间：服务端推这条消息的那一刻。"""
    return time.strftime("%H:%M")


def event_head(mid: str, tag: str, hm: str) -> str:
    """消息第一行：#码【小局结算】HH:MM。"""
    return f"#{mid}【{tag}】{hm}"


def score_rows(names, before, after) -> list:
    """逐家点数行：`名字 24000 -> 27000（+3000）`；名字或点数缺一律不写。"""
    out = []
    for i, nm in enumerate(names or []):
        if not nm or i >= len(before or []) or i >= len(after or []):
            continue
        b, a = int(before[i]), int(after[i])
        delta = a - b
        sign = "+" if delta > 0 else ""
        out.append(f"{nm} {b} -> {a}（{sign}{delta}）")
    return out


_SEAT_SEP = "─────"


def _seat_lines(names, bits=(), rates=(), suffixes=()) -> list:
    """四家那几行：一家一行，`选手A 八段R1796`。

    雀魂的五个率另起一行紧跟在段位之后，一家两行；只要有一家有率，座位之间就空一行。
    没有率（天凤和牌谱回放）还是一个座位一行，也不空行。

    bits / rates 与 names 一一对位（段位、PT、五个率），查不到就留空只写有的那几列，
    不编数。suffixes 是牌谱屋档位标记（如 `|高手`），紧贴名字之后写，其他平台不传。
    整行都没有名字就一行都不发。
    """
    rows = []
    for i, nm in enumerate(names or []):
        if not nm:
            continue
        bit = bits[i] if bits and i < len(bits) else ""
        rate = rates[i] if rates and i < len(rates) else ""
        suffix = suffixes[i] if suffixes and i < len(suffixes) else ""
        head = " ".join(p for p in (f"{nm}{suffix}", bit) if p)
        rows.append(f"{head}\n{rate}" if rate else head)
    if not any("\n" in r for r in rows):
        return rows
    out = []
    for r in rows:
        if out:
            out.append("")
        out.append(r)
    return out


def _start_head(mid: str, game_label: str, hm: str) -> str:
    """【对局开始】那行：牌谱回放没有真实的时间戳，留空就整段省掉。"""
    return "#%s 【对局开始】 %s" % (mid, " ".join(p for p in (game_label, hm) if p))


def _seat_block(names, bits, rates, suffixes=()) -> list:
    """四家那一整块，上下各一条分隔线；一家都不知道就不画线。"""
    seats = _seat_lines(names, bits, rates, suffixes)
    return [_SEAT_SEP] + seats + [_SEAT_SEP] if seats else []


def start_text(mid: str, game_label: str, names, hm: str,
               focus=(), lines=(), bits=(), rates=(), seats=True,
               suffixes=()) -> str:
    """【对局开始】：wg 那条回执和牌谱回放共用一个骨架。

    回放要把四家列出来，后面逐局才有主语；wg 回执只确认开没开成，`seats=False`
    把四家和分隔线一起省掉，主角仍按 names 找。
    """
    who = focus_name(names, focus)
    head = f"正在围观{who}乱杀: \n" if who else ""
    body = [_start_head(mid, game_label, hm)]
    body += [ln for ln in lines if ln]
    if seats:
        body += _seat_block(names, bits, rates, suffixes)
    return head + "\n".join(body)


def room_start_text(mid: str, game_label: str, names, hm: str, hits,
                    bits=(), rates=(), suffixes=()) -> str:
    """订阅玩家开打那一条：报谁在打、场名、四家，再告诉群里怎么开逐局播报。"""
    hits = [h for h in (hits or ()) if h]
    who = "、".join(sorted(hits))
    body = [f"{who or '有人'}正在乱杀，快来围观: ",
            _start_head(mid, game_label, hm)]
    body += _seat_block(names, bits, rates, suffixes)
    body.append(f"实时观战输入 /wg {mid}")
    return "\n".join(body)


def end_headline(nm: str, place: int, total: int) -> str:
    """顺位点评：1 位夸、末位喊苦、2 位和四人场 3 位各一句；其余名次不写点评。"""
    if place == 1:
        return f"{nm}太强了轻松获得了1位"
    if place == total:
        return f"{nm}被安排了一个{place}位，实在是太苦了"
    if place == 2:
        return f"{nm}悄然拿下2位，深藏功与名"
    if total == 4 and place == 3:
        return f"{nm}精通避4，避免了重大损失"
    return ""


def name_hit(nm: str, focus) -> bool:
    """订阅名和座位名互相包含就算同一个人（天凤列表里常见前后缀差异）。"""
    n = (nm or "").strip()
    if not n:
        return False
    for f in focus:
        f = (f or "").strip()
        if f and (f in n or n in f):
            return True
    return False


def focus_name(names, focus) -> str:
    """从座位名单里挑出所有订阅玩家当主角，用顿号连起来；一个都没有就返回空串。

    同一场里可能有两个以上订阅账号在打（开局播报 room_start_text 会把 hits
    全部列出），wg 回执也要一并报出来，不能只写座位顺序里的第一个。
    按座位顺序返回、去重，显示的是座位名单里的实际昵称。
    """
    hits = []
    for nm in names or []:
        if nm and name_hit(nm, focus) and nm not in hits:
            hits.append(nm)
    return "、".join(hits)


def fmt_num(v) -> str:
    """44.1 / -38 / +52800：数字去掉多余的 .0，已经排好符号的原样保留。"""
    if isinstance(v, str):
        return v
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(x)) if abs(x - round(x)) < 1e-6 else f"{x:.1f}"


def rank_rows(items) -> list:
    """[(名字, 终局绝对点数, 服务端点位)] → 【对局完成】那几行的 [(名字, 点数, 括号里那列)]。

    点数只准用服务端给的绝对终局点数，不做任何加减；括号那列是 grading_score，
    实测比赛场整场都是 0，那就整列省掉。
    """
    rows = [(int(final), float(pt), nm) for nm, final, pt in (items or []) if nm]
    if not rows:
        return []
    rows.sort(key=lambda r: -r[0])
    has_pt = any(abs(p) > 1e-6 for _, p, _ in rows)
    return [(nm, final, fmt_num(pt) if has_pt else None)
            for final, pt, nm in rows]


def finish_text(mid: str, rows, focus=(), notes=()) -> str:
    """rows 按名次排好：[(名字, 点数, 括号里的真实数值或 None)]。

    focus 是订阅玩家名；一个都没匹配上时用 1 位当主角（这场本来就有人在打）。
    """
    rows = [r for r in (rows or []) if r and r[0]]
    extra_notes = [n for n in (notes or []) if n]
    if not rows:
        return "\n".join([f"【对局完成】#{mid}"] + extra_notes)
    heads = [
        end_headline(r[0], place, len(rows))
        for place, r in enumerate(rows, 1)
        if name_hit(r[0], focus)
    ]
    heads = [h for h in heads if h]
    if not heads:
        heads.append(end_headline(rows[0][0], 1, len(rows)))
    lines = heads[:_MAX_HEADLINES] + [f"【对局完成】#{mid}"]
    for r in rows:
        pt = r[2] if len(r) > 2 else None
        tail = fmt_num(pt)
        pts = fmt_num(r[1])
        lines.append(f"{r[0]} {pts}({tail})" if tail else f"{r[0]} {pts}")
    return "\n".join(lines + extra_notes)


# ---- 个人定制文案（四类节点，整条模板重写）---------------------------
# custom 形如 {"玩家名": {"start": 模板, "round": 模板, "big": 模板, "finish": 模板}}
# 旧格式 {"玩家名": "模板"} 视为 finish 的点评句（保持旧的"只换点评行"行为）。
# 命中规则：一条播报里第一个出现在正文的订阅玩家，套用其在对应节点的模板。

class _CustomDict(dict):
    """str.format_map 用的字典：没定义的占位符原样留着，不抛 KeyError。"""

    def __missing__(self, key):
        return "{" + key + "}"


_NODES = ("start", "round", "big", "finish")
# 大牌通报的哨兵前缀：文本层编码「和出/点出 + 本局点数」，推送前剥掉换回 meta
POINT_MARK = "\x00POINT\x00"
# finish 模板里出现这些块占位符就算"整条重写"，否则按旧式"只换点评行"
_BLOCK_TOKENS = ("{raw}", "{body}", "{lines}", "{head}", "{heads}",
                 "{scores}", "{seats}", "{hule}", "{detail}")


def split_point_mark(text: str, mark: str = POINT_MARK):
    """剥掉大牌通报的哨兵前缀，返回 ``(正文, {"kind","gain"} 或 None)``。

    文本形如 ``\\x00POINT\\x00和出|26000|#mc361【大牌通报】12:06\\n东4局 …``。
    解析不出就原样返回正文 + None（当普通播报处理，别把消息丢了）。
    """
    src = str(text or "")
    if not src.startswith(mark):
        return src, None
    rest = src[len(mark):]
    head, sep, tail = rest.partition("\n")
    bits = head.split("|", 2)
    if len(bits) != 3 or bits[0] not in ("和出", "点出"):
        return rest, None
    body = bits[2] + (("\n" + tail) if sep else "")
    return body, {"kind": bits[0], "gain": bits[1]}


def _tpl_of(entry, node):
    """从 {玩家: 值} 的值里取某节点模板；旧字符串只映射到 finish。

    节点值为 dict（big 规则 / finish 名次档）时不在这取，返回空串。
    """
    if isinstance(entry, dict):
        v = entry.get(node)
        if isinstance(v, dict):
            return ""
        return str(v or "").strip()
    if node == "finish":
        return str(entry or "").strip()
    return ""


def _big_tpl_of(entry, meta):
    """大牌通报：按「和出/点出 × 本局点数」从定制值里选一条模板。

    entry 为该玩家的定制值（dict；旧字符串只映射到 finish，不涉及 big）。
    meta = {"kind": "和出|点出", "gain": 点数}。

    支持的 big 值形态（兼容由简到繁）：
      1) 字符串            → 两种情况通用、不分点数（旧用法）
      2) [{min,max,text}]   → 两种情况通用，按点数区间选
      3) {"和出":…,"点出":…} → 先按情况取分支，分支值可为字符串或规则列表；
                               键还可用「两者」兜底。
    点数区间 [min, max) 半开，留空不限；永远返回第一条命中的 text。
    """
    if not isinstance(entry, dict):
        return ""
    bv = entry.get("big")
    if bv in (None, ""):
        return ""
    meta = meta or {}
    kind = str(meta.get("kind") or "").strip()
    try:
        gain = float(meta.get("gain") or 0)
    except (TypeError, ValueError):
        gain = 0.0

    def pick(v):
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, list):
            for r in v:
                if not isinstance(r, dict):
                    continue
                txt = str(r.get("text") or "").strip()
                if not txt:
                    continue
                lo, hi = r.get("min"), r.get("max")
                lo = None if lo in (None, "") else lo
                hi = None if hi in (None, "") else hi
                try:
                    if lo is not None and gain < float(lo):
                        continue
                    if hi is not None and gain >= float(hi):
                        continue
                except (TypeError, ValueError):
                    continue
                return txt
        return ""

    if isinstance(bv, dict):
        chosen = bv.get(kind)
        if chosen is None:
            chosen = bv.get("两者") or bv.get("通用")
        return pick(chosen)
    return pick(bv)


def _finish_tpl_of(entry, ctx):
    """对局完成：定制值为 {1/2/3/4/*: 模板} 时按本局名次选一套。

    名次取 ctx['place']（finish 解析点数行得出）；该名次没配时依次回退
    「*」「通用」。返回空串表示没有结构化分档，调用方退回旧模板逻辑。
    """
    if not isinstance(entry, dict):
        return ""
    fv = entry.get("finish")
    if not isinstance(fv, dict) or not fv:
        return ""
    place = str(ctx.get("place") or "")
    for k in (place, "*", "通用"):
        v = fv.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _render_custom(tpl: str, ctx: dict) -> str:
    """渲染一条定制模板；格式串写错时退回原文，绝不打断整段播报。"""
    try:
        return str(tpl).format_map(ctx)
    except (KeyError, ValueError, IndexError, AttributeError):
        return str(tpl)


def _detect_node(text):
    if "【对局开始】" in text:
        return "start"
    if "【大牌通报】" in text:
        return "big"
    if "【对局完成】" in text:
        return "finish"
    if ("【小局结算】" in text or "【流局】" in text
            or "【牌谱回放" in text):
        return "round"
    return ""


def _hit_name(text, custom):
    """返回第一个在播报正文出现的订阅玩家名（兼容前后缀差异）。"""
    for nm in (custom or {}).keys():
        nm = str(nm or "").strip()
        if nm and nm in text:
            return nm
    return ""


def _node_ctx(kind, text, name) -> dict:
    """把一条播报拆成语义块，供整条模板引用。未知占位符原样保留。"""
    lines = str(text or "").split("\n")
    ctx = _CustomDict(
        name=name, player=name,
        raw=text, lines="\n".join(lines),
        head=lines[0] if lines else "",
        body="\n".join(lines[1:]),
        mid="", time="", game="", who="", lead="", seats="",
        hule="", detail="", scores="", desc="", mood="",
        place="", total="", score="", pt="", rank="", point="", delta="",
        heads="", name_line="",
    )
    m = re.search(r"#([A-Za-z0-9]+)", text)
    if m:
        ctx["mid"] = m.group(1)
    m = re.search(r"(\d{2}:\d{2})", text)
    if m:
        ctx["time"] = m.group(1)

    if kind == "start":
        ctx["lead"] = lines[0] if lines else ""
        for ln in lines:
            if "【对局开始】" in ln:
                ctx["head"] = ln
                tail = ln.split("【对局开始】", 1)[-1].strip()
                ctx["game"] = re.sub(r"\s*\d{2}:\d{2}\s*$", "", tail).strip()
                break
        seps = [i for i, ln in enumerate(lines) if ln.strip().startswith("────")]
        if len(seps) >= 2:
            ctx["seats"] = "\n".join(lines[seps[0]:seps[-1] + 1])
        ctx["who"] = name

    elif kind == "round":
        body_lines = lines[1:] if lines else []
        score_start = len(body_lines)
        for i, ln in enumerate(body_lines):
            if re.match(r"^.+?\s+-?[\d,]+\s*->\s*-?[\d,]+", ln.strip()):
                score_start = i
                break
        ctx["hule"] = body_lines[0] if body_lines else ""
        ctx["detail"] = "\n".join(body_lines[1:score_start]).strip()
        ctx["scores"] = "\n".join(body_lines[score_start:]).strip()

    elif kind == "big":
        body_lines = [ln for ln in lines[1:] if ln.strip()] if lines else []
        if body_lines:
            d = body_lines[0]
            ctx["desc"] = d
            mm = re.search(r"[，,]([^，,。]+)[。.]?\s*$", d)
            if mm:
                ctx["mood"] = mm.group(1).strip()
                ctx["desc"] = d[:mm.start()].strip()
        ctx["hule"] = ctx["desc"]

    elif kind == "finish":
        marker = "【对局完成】#"
        pos = text.find(marker)
        if pos >= 0:
            head_part, tail_part = text[:pos], text[pos:]
        else:
            head_part, tail_part = "", text
        heads = [ln for ln in head_part.split("\n") if ln.strip()]
        ctx["heads"] = "\n".join(heads)
        tlines = tail_part.split("\n")
        ctx["head"] = tlines[0] if tlines else "【对局完成】"
        score_lines = []
        for ln in tlines[1:]:
            s = ln.strip()
            if not s or s.startswith("注:") or s.startswith("注："):
                continue
            score_lines.append(s)
        ctx["scores"] = "\n".join(score_lines)
        ctx["body"] = tail_part.strip()
        rows, place = {}, 0
        for ln in score_lines:
            mm = re.match(r"^(.+?)\s+(-?[\d,]+)(?:\(([^)]*)\))?$", ln)
            if not mm:
                continue
            place += 1
            rows[mm.group(1).strip()] = (mm.group(2), (mm.group(3) or "").strip(), place)
        score, pt, pl = rows.get(name, ("", "", 0))
        ctx.update(place=str(pl), rank=str(pl), total=str(place),
                   score=score, point=score, pt=pt, delta=pt)
        for ln in heads:
            if ln.strip().startswith(name):
                ctx["name_line"] = ln.strip()
                break
    return ctx


def _apply_finish_headline(src, name, tpl, ctx):
    """旧式：只替换【对局完成】上方命中玩家的点评行，其余原样。"""
    marker = "【对局完成】#"
    pos = src.find(marker)
    if pos < 0:
        return src
    head, tail = src[:pos], src[pos:]
    heads = [ln for ln in head.split("\n") if ln.strip()]
    changed = False
    for i, ln in enumerate(heads):
        if ln.strip().startswith(name):
            heads[i] = _render_custom(tpl, ctx)
            changed = True
            break
    if not changed:
        heads.append(_render_custom(tpl, ctx))
    lead_end = len(head) - len(head.lstrip("\n"))
    return head[:lead_end] + "\n".join(heads) + "\n" + tail.lstrip("\n")


def apply_custom_broadcast(text: str, custom, meta=None) -> str:
    """按 session 定制：命中订阅玩家 + 节点后，用整条模板重写该节点播报。

    兼容旧数据：{玩家: "点评句"} 只作用于 finish，且只换点评行，不重写整条。
    meta 仅为 big 节点携带 {"kind": "和出|点出", "gain": 点数}，用于分情况/分档。
    """
    src = str(text or "")
    if not custom or not isinstance(custom, dict) or not src:
        return src
    kind = _detect_node(src)
    if not kind:
        return src
    name = _hit_name(src, custom)
    if not name:
        return src
    entry = custom.get(name)
    ctx = None
    if kind == "big":
        tpl = _big_tpl_of(entry, meta)
    else:
        tpl = _tpl_of(entry, kind)
    if kind == "finish" and isinstance(entry, dict):
        # 结构化名次分档：{finish: {1/2/3/4/*: 模板}}，按本局名次选一套，
        # 命中即整条重写（不沿用旧式点评行替换，避免残留别家点评）
        c0 = _node_ctx("finish", src, name)
        rank_tpl = _finish_tpl_of(entry, c0)
        if rank_tpl:
            return _render_custom(rank_tpl, c0)
    if not tpl:
        return src
    ctx = _node_ctx(kind, src, name)
    if kind == "big":
        m = meta or {}
        ctx["kind"] = str(m.get("kind") or "")
        ctx["gain"] = str(m.get("gain") or "")
    if kind == "finish" and not any(t in tpl for t in _BLOCK_TOKENS):
        return _apply_finish_headline(src, name, tpl, ctx)
    return _render_custom(tpl, ctx)


def apply_custom_finish(text: str, custom, meta=None) -> str:
    """兼容别名：等价于 apply_custom_broadcast。"""
    return apply_custom_broadcast(text, custom, meta)


# ---- QQ 官方 Markdown 适配 -------------------------------------------
# AstrBot 的 qqofficial 适配器把 Plain 原样塞进 markdown.content（msg_type=2），
# 纯文本排版在 QQ 上会被 Markdown 重新解释：行首 # 变标题、单换行被合并、
# ───── 不是分割线。下面这组转换只在 QQ 官方会话启用，其他平台保持原文。

_QQ_PLATFORMS = {"qqofficial", "qq_official", "qq_official_webhook"}
_NOTE_MARK = ("注:", "注：")
# <...> 在 QQ Markdown 里是自动链接/HTML 标记，参数占位符会被吞（$AID→AID）。
# 换成全角尖括号即可保留原意；真实 URL 与 <@提及> 不动。
_ARG_TAG = re.compile(r"<([^<>\n]+)>")
# 零宽空格：QQ 把它当正文字符保留（文档用它撑开空行），且完全不可见。
# 插在行首 # 前，# 就不是行首标记，于是井号原样显示、字号正常。
_ZWSP = "\u200b"

# 播报（开局/终局/流局/小局结算/回放）带事件标题或座位分隔线，和命令回复
# 走两套排版：播报做页眉页脚引用 + 统计行引用 + 订阅玩家加粗；命令回复
# 只把"标题行后紧跟的缩进行"转引用，其余保持基础转义。
_BROADCAST_TAG = re.compile(
    r"【(?:对局开始|对局完成|流局|小局结算|牌谱回放|大牌通报)】")
_STAT = re.compile(r"^顺[\d.]+")          # 五率辅助行：顺2.52 和21.66% …
_TITLE_LINE = re.compile(
    r"^(?:天凤订阅|雀魂订阅|天凤跟踪|天凤进行中对局|雀魂跟踪|雀魂进行中对局).*:$"
    r"|^.+ \(\d+\):$"
)
_BOLD_SCORE = re.compile(r"^(.*?) (-?\d+) -> (-?\d+)(（[+-]?\d+）)$")
_BOLD_RANK = re.compile(r"^(.*?) (\d+(?:\([^)]*\))?)$")
_BOLD_SEAT = re.compile(r"^(.*?) (\S+ \d+/\d+)$")
# 名字加粗与结果加粗会在名字和段位之间留下两个相邻的 ** 加粗段；
# QQ Markdown 会把相邻加粗段之间那个空格吃掉，看着像"名字段位"黏在一起。
# 合并成一个加粗段即可保住空格（**名** **段** → **名 段**）。
_ADJ_BOLD = re.compile(r"\*\* \*\*")


def is_qq_platform(platform_name: str) -> bool:
    """判断平台**类型**（event.get_platform_name() 的返回值，如 qq_official）。

    AstrBot 的 unified_msg_origin 前缀是平台**实例名**（用户自定义，如「虎皮A梦」），
    不是类型名，所以不能靠会话前缀判断是不是 QQ 官方。这里按类型判断才可靠。
    """
    return str(platform_name or "").strip().lower() in _QQ_PLATFORMS


def is_qq_session(session: str) -> bool:
    """按 unified_msg_origin 前缀判断（仅在拿不到平台类型时的兜底）。

    能匹配标准平台 ID（qqofficial/qq_official/...）；自定义实例名匹配不到，
    这时调用方应改用 is_qq_platform(event.get_platform_name())。
    """
    return str(session or "").split(":", 1)[0].strip().lower() in _QQ_PLATFORMS


def _esc_line(line: str) -> str:
    """参数占位符和 Markdown 敏感字符全角化，不改数字和名字。

    <...> 里的真实 URL / <@提及> 不动；[] ~~ * _ 会被 QQ 当链接/删除线/斜体，
    本插件发的都是纯文本参数说明，全角化后原样显示。
    """
    line = _ARG_TAG.sub(
        lambda m: m.group(0) if m.group(1).startswith(
            ("http://", "https://", "@")) else f"＜{m.group(1)}＞", line)
    return (line.replace("[", "［").replace("]", "］")
                .replace("~~", "～～").replace("*", "＊").replace("_", "＿"))


def md_text(text: str, focus=()) -> str:
    """把本插件的纯文本排版转成 QQ 原生 Markdown 友好排版。

    播报与命令回复分两套：播报做页眉/页脚引用、统计行引用，并只加粗 focus
    里的订阅玩家；命令回复把"标题行后紧跟的缩进行"转引用。两者都不改文字。

    focus 由调用方按会话订阅名单传入（名字或 AID），其他平台不调用本函数。
    """
    src = str(text or "")
    if not src:
        return src
    if _BROADCAST_TAG.search(src) or _SEAT_SEP in src:
        return _md_broadcast(src, focus)
    return _md_command(src)


def _protect_hash(raw: str) -> str:
    """行首 #码前插零宽空格，井号原样显示、字号正常；缩进行同样保护。"""
    stripped = raw.strip()
    if stripped.startswith("#"):
        lead = len(raw) - len(raw.lstrip())
        return raw[:lead] + _ZWSP + raw[lead:]
    return raw


def _is_punct(ch: str) -> bool:
    """标点/符号判定，用于 Markdown 加粗边界的通用处理。

    QQ 官方 Markdown 按 CommonMark 的分隔符规则解析 `**`：闭合 `**` 前面若是
    标点、后面紧跟正文，就不算合法右分隔符，两个星号会被当成字面字符显示。
    这里按 Unicode 类别判定（P 标点 / S 符号），对任何语言的任何标点一视同仁，
    不针对 `【】` 之类的具体字符开特例。
    """
    return bool(ch) and unicodedata.category(ch)[0] in ("P", "S")


def _bold_name(name: str) -> tuple[str, str]:
    """返回 (转义后的纯名, 加粗写法)：只粗名字里连续的正文，首尾标点留在外。

    例如 `【豆沙团子】` → 纯名 `【豆沙团子】`、加粗 `【**豆沙团子**】`，
    这样闭合 `**` 前面是「子」（正文）而不是「】」（标点），无论后面跟什么正文
    都能合法闭合。名字全是标点时无法加粗，返回原样、纯名相同以便跳过。
    """
    esc = _esc_line(name)
    lo, hi = 0, len(esc)
    while lo < hi and _is_punct(esc[lo]):
        lo += 1
    while hi > lo and _is_punct(esc[hi - 1]):
        hi -= 1
    if lo == hi:
        return esc, esc
    return esc, esc[:lo] + "**" + esc[lo:hi] + "**" + esc[hi:]


def _bold_focus(line: str, focus) -> str:
    """给订阅玩家的名字加粗（只加第一次，避免结果列重复加粗）。

    正文已经过 `_esc_line`，所以这里也用转义后的名字去匹配，否则像
    `冬马_かずさ` 这种含 Markdown 敏感字符的名字会匹配不上、整行不加粗。
    """
    for n in focus:
        raw = str(n or "").strip()
        if not raw:
            continue
        plain, bold = _bold_name(raw)
        if plain == bold or plain not in line or bold in line:
            continue
        line = line.replace(plain, bold, 1)
    return line


def _focus_hit(line: str, focus) -> bool:
    """这一行是否提到订阅玩家；兼容加粗前（纯名）与加粗后（含 **）两种形态。"""
    for n in focus:
        raw = str(n or "").strip()
        if not raw:
            continue
        plain, bold = _bold_name(raw)
        if plain in line or (plain != bold and bold in line):
            return True
    return False


def _bold_result(line: str, focus) -> str:
    """订阅玩家所在行，加粗其结果数字；其他人的数字保持普通文本。"""
    if not _focus_hit(line, focus):
        return line
    m = _BOLD_SCORE.match(line)          # 名字 24000 -> 27000（+3000）
    if m:
        return f"{m.group(1)} {m.group(2)} -> **{m.group(3)}{m.group(4)}**"
    m = _BOLD_RANK.match(line)           # 名字 30600(131) / 雀圣三 3099/9000
    if m:
        return f"{m.group(1)} **{m.group(2)}**"
    m = _BOLD_SEAT.match(line)           # 名字 雀豪一 1496/2800
    if m:
        return f"{m.group(1)} **{m.group(2)}**"
    return line


def _md_broadcast(text: str, focus) -> str:
    """播报专用排版：页眉/页脚引用、统计行引用、只加粗订阅玩家。"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    # 文案位置：把标题行提到最前（终局的点评在标题前、开局的"正在乱杀"在标题前）
    for i, ln in enumerate(lines):
        if "【对局完成】" in ln and i > 0:
            lines[0], lines[i] = lines[i], lines[0]
            break
    for i, ln in enumerate(lines):
        if "【对局开始】" in ln and i > 0:
            lines[0], lines[i] = lines[i], lines[0]
            break
    # 观战指令抽出来，最后放到末尾页脚
    instr = [ln for ln in lines if ln.startswith("实时观战输入 ")]
    lines = [ln for ln in lines if not ln.startswith("实时观战输入 ")]

    parts: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped == _SEAT_SEP:
            parts.append("***")
            continue
        parts.append(_esc_line(_protect_hash(raw)))

    out: list[str] = []
    for i, part in enumerate(parts):
        if i == 0:
            out.extend(["> " + part, "***"])          # 页眉：引用 + 横线
            continue
        if part.startswith(_NOTE_MARK):
            out.extend(["***", "> " + part])          # 注页脚
            continue
        part = _bold_result(_bold_focus(part, focus), focus)
        part = _ADJ_BOLD.sub(" ", part)                  # 合并相邻加粗段，保住名字后的空格
        if _STAT.match(part):
            out.append("> " + part)                   # 五率行：引用降权
        else:
            out.append(part)
    if instr:
        while out and out[-1] == "***":
            out.pop()
        out.extend(["***", "> " + _esc_line(instr[0])])  # 观战指令页脚
    return "\n\n".join(out)


def _md_command(text: str) -> str:
    """命令回复排版：标题行后紧跟的缩进行转引用，其余保持基础转义。"""
    tagged: list[tuple[str, str]] = []
    in_quote = False
    for raw in text.split("\n"):
        stripped = raw.strip()
        is_title = bool(_TITLE_LINE.match(stripped))
        is_indent = raw.startswith("  ") and bool(stripped)
        if is_title:
            tagged.append(("title", raw))
            in_quote = True
            continue
        if in_quote and is_indent:
            tagged.append(("quote", raw))
            continue
        if not stripped:
            tagged.append(("blank", raw))
            continue
        in_quote = False
        tagged.append(("text", raw))

    out: list[str] = []
    for kind, raw in tagged:
        if kind == "blank":
            out.append("")
            continue
        conv = _esc_line(_protect_hash(raw))
        out.append("> " + conv.lstrip() if kind == "quote" else conv)

    parts: list[str] = []
    prev = ""
    for line in out:
        cur = "quote" if line.startswith("> ") else "text"
        if parts:
            parts.append("\n" if cur == prev == "quote" else "\n\n")
        parts.append(line)
        prev = cur
    return "".join(parts)
