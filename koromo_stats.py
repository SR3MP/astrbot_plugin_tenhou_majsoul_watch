# -*- coding: utf-8 -*-
"""牌谱屋安定段位算法 + 档位/点数规则匹配（纯函数，无网络依赖）。

安定段位复刻牌谱屋前端 amae-koromo（src/data/types/metadata.ts）：
  RANK_DELTA_4 = [15, 5, -5, -15]      四人
  RANK_DELTA_3 = [15, 0, -15]          三人（只有 3 个顺位）
  expected = Σ ( ceil((rank_avg_score[i]-基准点)/1000 + RANK_DELTA[i])
                 + MODE_DELTA[mode][i] ) * rank_rates[i]      # includePenalty=false
  result   = (expected / rank_rates[末位] - 末位罚点截距) / 末位罚点斜率

末位罚点（level.ts LEVEL_PENALTY*）在雀豪一~雀圣三之间是线性的，用两端相减得到
斜率、再回推截距，等价于牌谱屋对玉/王座用的 `expected/(rate*15)-10`：
  四人南 165→240 斜率 15 截距 150      四人东 80→130 斜率 10 截距 70
  三人南 165→290 斜率 25 截距 140      三人东 80→175 斜率 15 截距 65
基准点四人 25000、三人 35000（三麻三家和了收支不同）。

本插件统一换算成同一个数值口径，逐级试算的连续解与闭式一致（整位部分就是牌谱屋显示的那个段位）。
数值口径固定为 雀豪一=1 / 雀圣一=4 / 雀圣三=6，显示沿用牌谱屋：
>=4 写 "圣"+(v-3)，<4 写 "豪"+v；加 "安" 前缀、截取两位。
**算出来多少写多少，不钳制、不取整**。

王座场的魂天玩家是例外：牌谱屋按 KONTEN_DELTA 算预期点数，正收益显示
「魂天+x.xx」、持平显示「魂天」，为负才退化到雀圣口径的安圣/安豪值；
用普通 MODE_DELTA 会把魂天玩家算成完全错误的安定值（见 stable_level_display）。
"""
from __future__ import annotations

import math
from typing import Optional

# 牌谱屋 GameMode（gameMode.ts）→ (顺位增减, MODE_DELTA, 基准点, 罚点斜率, 罚点截距)
# 只列牌谱屋前端会显示安定段位的场：金之间不在这套口径里。
_MODE_PARAMS = {
    # 四人半庄
    9: ([15, 5, -5, -15], [80, 40, 0, 0], 25000, 15, 150),
    12: ([15, 5, -5, -15], [110, 55, 0, 0], 25000, 15, 150),
    16: ([15, 5, -5, -15], [120, 60, 0, 0], 25000, 15, 150),
    # 四人东风
    8: ([15, 5, -5, -15], [40, 20, 0, 0], 25000, 10, 70),
    11: ([15, 5, -5, -15], [55, 30, 0, 0], 25000, 10, 70),
    15: ([15, 5, -5, -15], [60, 30, 0, 0], 25000, 10, 70),
    # 三人半庄
    22: ([15, 0, -15], [105, 0, 0], 35000, 25, 140),
    24: ([15, 0, -15], [160, 0, 0], 35000, 25, 140),
    26: ([15, 0, -15], [240, 0, 0], 35000, 25, 140),
    # 三人东风
    21: ([15, 0, -15], [55, 0, 0], 35000, 15, 65),
    23: ([15, 0, -15], [75, 0, 0], 35000, 15, 65),
    25: ([15, 0, -15], [120, 0, 0], 35000, 15, 65),
}
# 魂天（Konten）专用顺位增减：牌谱屋 metadata.ts 的 KONTEN_DELTA。
# 魂天玩家在王座场不再用 MODE_DELTA + 顺位分，而是直接按这几个数算预期点数，
# 安定显示成「魂天+4.69」或退化到雀圣口径的安圣值。之前插件无脑套 MODE_DELTA
# （王座 [120,60,0,0]），魂天玩家会显示成完全错误的「安圣x.xx」。
_KONTEN_DELTA = {
    15: [30, 10, -10, -30],      # 王座东
    16: [50, 20, -20, -50],      # 王座
    25: [30, 0, -30],            # 三王座东
    26: [50, 0, -50],            # 三王座
}
# 魂天预期点数为负时，牌谱屋用它退化到雀圣三的口径再算安定值
_KONTEN_FALLBACK_LEVEL_ID = 503

# 牌谱屋会显示安定段位的场：前端 EstimatedStableLevel 的 ENABLED_MODES，只有
# 玉/王座（含东风版）与三麻玉/王座。金之间虽然有 delta 常量，前端不显示安定。
STABLE_MODES = frozenset({11, 12, 15, 16, 23, 24, 25, 26})


def _level_is_konten(stats: dict) -> bool:
    """stats.level.id 是否魂天（level.ts: major>=6 即 107xx，含旧魂天 106xx）。"""
    lv = stats.get("level")
    if not isinstance(lv, dict):
        return False
    try:
        lid = int(lv.get("id") or 0)
    except (TypeError, ValueError):
        return False
    return (lid % 10000) // 100 >= 6


def _stable_fields(stats: dict, n: int):
    """取顺位率与顺位平均得点；缺项/脏数据返回 None（保持"算不出就不写"）。"""
    # 牌谱屋对没打过/数据不全的 mode 会把 rank_rates、rank_avg_score 里填 null，
    # 直接 float() 会抛 TypeError 一路冒到轮询线程，把整轮播报全打断（实测每小时
    # 偶发一次）。
    try:
        rates = [float(x) for x in (stats.get("rank_rates") or [])]
        scores = [float(x) for x in (stats.get("rank_avg_score") or [])]
    except (TypeError, ValueError):
        return None
    if len(rates) < n or len(scores) < n or not rates[n - 1]:
        return None
    return rates, scores


def stable_level_display(stats: dict, mode: int = 12) -> tuple[Optional[float], str]:
    """牌谱屋 estimateStableLevel2 的完整口径 → (数值档位用, 播报显示文本)。

    四人玉/王座走闭式 `expected / (末位率*15) - 10`；王座场遇到魂天玩家时，
    预期点数要按 KONTEN_DELTA 算（牌谱屋显示「魂天+x.xx」），为负才退化回
    雀圣口径的安圣值。数值口径仍固定 雀豪一=1 / 雀圣一=4 / 雀圣三=6。
    """
    params = _MODE_PARAMS.get(mode)
    if not params:
        return None, ""
    rank_delta, mode_delta, base_point, slope, intercept = params
    n = len(rank_delta)
    fields = _stable_fields(stats, n)
    if not fields:
        return None, ""
    rates, scores = fields

    def expected(delta: list) -> float:
        # calculateDeltaPoint(..., includePenalty=false, trimNumber=true)
        return sum((math.ceil((scores[i] - base_point) / 1000 + rank_delta[i])
                    + delta[i]) * rates[i] for i in range(n))

    kd = _KONTEN_DELTA.get(mode)
    if kd and len(kd) == n and _level_is_konten(stats):
        # calculateDeltaPoint 对魂天直接返回 KONTEN_DELTA[rank]：不吃基准点、
        # 不吃 ceil、也不吃顺位分，这是和王座普通玩家最关键的差别。
        pts = sum(kd[i] * rates[i] for i in range(n))
        if abs(pts) < 0.001:
            return None, "魂天"
        if pts > 0:
            # 牌谱屋这里用 toFixed(2)（四舍五入），不是 formatNumber 的截断
            return None, f"魂天+{pts:.2f}"
        # 预期点数为负：牌谱屋退化到 KONTEN_FALLBACK_LEVEL_ID(503，雀圣三)
        # 重算，仍走普通 MODE_DELTA 闭式。
        pts = expected(mode_delta)
    else:
        pts = expected(mode_delta)

    value = (pts / rates[n - 1] - intercept) / slope
    return value, format_stable_level(value)


def estimate_stable_level(stats: dict, mode: int = 12) -> Optional[float]:
    """按牌谱屋口径算安定段位的数值（如 5.409 / -3.099）；算不出返回 None。

    stats 需要 rank_rates 与 rank_avg_score（player_stats 直接给），四人 4 项、
    三人 3 项。mode 取牌谱屋 GameMode（玉 12 / 王座 16 / 三玉 24 / 三王座 26 /
    玉东 11 / 王东 15 / 三玉东 23 / 三王东 25 等）；金之间与比赛场返回 None。
    """
    return stable_level_display(stats, mode)[0]


def _format_number2(x: float) -> str:
    """复刻牌谱屋前端 formatNumber：截取到小数点后 2 位（非四舍五入），补足两位。

    -1.8373 → "-1.83"、-3.099 → "-3.09"、2.5 → "2.50"。负数按字符串截断，
    等价于向零截断，和牌谱屋 slice 的行为一致。
    """
    s = repr(float(x))
    if "e" in s.lower():          # 极端值退化成定点表示，安定值正常遇不到
        s = f"{float(x):.10f}"
    if "." not in s:
        s += ".00"
    head, frac = s.split(".", 1)
    return f"{head}.{(frac + '00')[:2]}"


def format_stable_level(value: Optional[float]) -> str:
    """安定数值 → `安圣2.54` / `安豪-1.83`；截取两位，不钳制。

    >=4 走 "圣"+(v-3)，<4 走 "豪"+v，和牌谱屋 formatStableLevel2 完全一致。
    """
    if value is None:
        return ""
    v = float(value)
    if v >= 4:
        return f"安圣{_format_number2(v - 3)}"
    return f"安豪{_format_number2(v)}"


def match_tier(value: Optional[float], tiers: list) -> str:
    """安定数值命中档位 → 文案标签；按顺序取第一个命中的，未命中返回空串。

    tiers 是 JSON 数组，每项 {"min":数值, "max":数值, "label":"文案"}，
    min/max 可省略表示不限；区间按 [min, max) 半开处理，边界不重复命中。
    """
    if value is None:
        return ""
    v = float(value)
    for t in tiers or []:
        if not isinstance(t, dict):
            continue
        label = str(t.get("label") or "").strip()
        if not label:
            continue
        lo, hi = t.get("min"), t.get("max")
        # 空字符串按不限处理（WebUI template_list 留空即空串）
        lo = None if lo is None or lo == "" else lo
        hi = None if hi is None or hi == "" else hi
        try:
            if lo is not None and v < float(lo):
                continue
            if hi is not None and v >= float(hi):
                continue
        except (TypeError, ValueError):
            continue
        return label
    return ""


def match_point_rules(kind: str, points: float, rules: list) -> list[str]:
    """和出/点出的点数命中规则 → 文案列表（可多条）。

    kind 为 "和出" 或 "点出"；points 为这一局该玩家的点数变化（和出为正收益、
    点出为放铳损失的绝对值，调用方保证取正数）。rules 每项：
      {"type":"和出|点出|两者", "min":数值, "max":数值, "text":"文案"}
    min/max 可省略表示不限，区间按 [min, max) 半开。
    """
    out: list[str] = []
    p = float(points or 0)
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        rtype = str(r.get("type") or "两者").strip()
        if rtype not in ("两者", kind):
            continue
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        lo, hi = r.get("min"), r.get("max")
        # 空字符串按不限处理（WebUI template_list 留空即空串）
        lo = None if lo is None or lo == "" else lo
        hi = None if hi is None or hi == "" else hi
        try:
            if lo is not None and p < float(lo):
                continue
            if hi is not None and p >= float(hi):
                continue
        except (TypeError, ValueError):
            continue
        out.append(text)
    return out
