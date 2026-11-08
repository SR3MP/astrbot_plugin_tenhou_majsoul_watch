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

import time

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


def _seat_lines(names, bits=(), rates=()) -> list:
    """四家那几行：一家一行，`选手A 八段R1796`。

    雀魂的五个率另起一行紧跟在段位之后，一家两行；只要有一家有率，座位之间就空一行。
    没有率（天凤和牌谱回放）还是一个座位一行，也不空行。

    bits / rates 与 names 一一对位（段位、PT、五个率），查不到就留空只写有的那几列，
    不编数。整行都没有名字就一行都不发。
    """
    rows = []
    for i, nm in enumerate(names or []):
        if not nm:
            continue
        bit = bits[i] if bits and i < len(bits) else ""
        rate = rates[i] if rates and i < len(rates) else ""
        head = " ".join(p for p in (str(nm), bit) if p)
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


def _seat_block(names, bits, rates) -> list:
    """四家那一整块，上下各一条分隔线；一家都不知道就不画线。"""
    seats = _seat_lines(names, bits, rates)
    return [_SEAT_SEP] + seats + [_SEAT_SEP] if seats else []


def start_text(mid: str, game_label: str, names, hm: str,
               focus=(), lines=(), bits=(), rates=(), seats=True) -> str:
    """【对局开始】：wg 那条回执和牌谱回放共用一个骨架。

    回放要把四家列出来，后面逐局才有主语；wg 回执只确认开没开成，`seats=False`
    把四家和分隔线一起省掉，主角仍按 names 找。
    """
    who = focus_name(names, focus)
    head = f"正在围观{who}乱杀: \n" if who else ""
    body = [_start_head(mid, game_label, hm)]
    body += [ln for ln in lines if ln]
    if seats:
        body += _seat_block(names, bits, rates)
    return head + "\n".join(body)


def room_start_text(mid: str, game_label: str, names, hm: str, hits,
                    bits=(), rates=()) -> str:
    """订阅玩家开打那一条：报谁在打、场名、四家，再告诉群里怎么开逐局播报。"""
    hits = [h for h in (hits or ()) if h]
    who = "、".join(sorted(hits))
    body = [f"{who or '有人'}正在乱杀，快来围观: ",
            _start_head(mid, game_label, hm)]
    body += _seat_block(names, bits, rates)
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
    """从座位名单里挑一个订阅玩家当主角；一个都没订阅就返回空串。"""
    for nm in names or []:
        if name_hit(nm, focus):
            return nm
    return ""


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
