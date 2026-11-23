# -*- coding: utf-8 -*-
"""订阅玩家的 PT 快照与日/周/月变动排行（雀魂）。

snapshot 结构（history.json）：
  {"YYYY-MM-DD": {"<aid>": {"name":昵称, "rank":段位, "pt":四人pt,
                              "rank3":三人段位, "pt3":三人pt}}}
每天固定时刻抓一次，落盘。报告按"今天 - N 天前"取差，按 pt 变动排序。
pt 是累计段位分，只有同一账号跨天做差才有意义；不做跨账号比较。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Optional

HISTORY_FILE_NAME = "history.json"
# 只看最近 40 天：月报要回看 30 天，留 10 天余量，再旧的快照没用还占地方
KEEP_DAYS = 40

# ---- 段位 PT 是「段内点数」，升段会重置到新段位起点，不能直接相减 ----
# 牌谱屋 LEVEL_MAX_POINTS + getStartingPoint(=max/2)；四人/三人同一套。
# (tier, star) -> (起点, 升段线)。魂天单独处理（见下）。
_LEVEL_SPAN = (
    (0, 20),    # 初心一
    (40, 80),   # 初心二
    (100, 200), # 初心三
    (300, 600), (400, 800), (500, 1000),                    # 雀士一/二/三
    (600, 1200), (700, 1400), (1000, 2000),                 # 雀杰一/二/三
    (1400, 2800), (1600, 3200), (1800, 3600),               # 雀豪一/二/三
    (2000, 4000), (3000, 6000), (4500, 9000),               # 雀圣一/二/三
)
_KONTEN_START = 1000   # 魂天N 段内起点（牌谱屋 show 10.0）
_KONTEN_MAX = 2000     # 魂天N 升段线（show 20.0）；魂天20 封顶无上限
_KONTEN_LAST = 20
_TIER_BASE = {1: "初心", 2: "雀士", 3: "雀杰", 4: "雀豪", 5: "雀圣"}
_CN_NUM = {"一": 1, "二": 2, "三": 3}


def _level_to_key(level_id) -> Optional[tuple]:
    """level.id → (tier, star)；10701=魂天1、10503=雀圣三。无效返回 None。"""
    try:
        real = int(level_id) % 10000
    except (TypeError, ValueError):
        return None
    tier, star = real // 100, real % 100
    if tier in _TIER_BASE and star in (1, 2, 3):
        return (tier, star)
    if tier == 6:
        return (6, 0)          # 旧版魂天（无 N）
    if tier == 7 and 1 <= star <= _KONTEN_LAST:
        return (7, star)
    return None


def _rank_to_key(rank) -> Optional[tuple]:
    """段位中文名 → (tier, star)；兼容历史快照（只存了 rank 没存 level）。"""
    s = str(rank or "").strip()
    for tier, base in _TIER_BASE.items():
        if s.startswith(base):
            star = _CN_NUM.get(s[len(base):], 0)
            if star:
                return (tier, star)
    if s == "魂天":
        return (6, 0)
    if s.startswith("魂天") and s[2:].isdigit():
        star = int(s[2:])
        return (7, star) if 1 <= star <= _KONTEN_LAST else None
    return None


def _entry_of(key: tuple) -> Optional[float]:
    """升入该段位起点时，之前所有段位累计贡献的 PT 跨度。"""
    tier, star = key
    if tier in _TIER_BASE:
        idx = (tier - 1) * 3 + (star - 1)
        return float(sum(mx - st for st, mx in _LEVEL_SPAN[:idx]))
    base = float(sum(mx - st for st, mx in _LEVEL_SPAN))
    if tier == 6:
        return base
    if tier == 7:
        return base + (star - 1) * (_KONTEN_MAX - _KONTEN_START)
    return None


def global_pt(rank, pt, level_id=None) -> Optional[float]:
    """(段位, 段内PT) → 全局累计 PT，使跨段位也能直接相减算涨幅。

    段内起点/升段线见 _LEVEL_SPAN；全局值 = 之前各段跨度之和 + (本段PT − 本段起点)。
    例：雀圣三 8851 → 魂天1 1040，全局 17811 → 18000，涨 +189（而非 −7811）。
    段位名认不出或 PT 非数时返回 None（调用方回退到裸 PT 差）。
    """
    key = _level_to_key(level_id) if level_id is not None else None
    if key is None:
        key = _rank_to_key(rank)
    if key is None:
        return None
    try:
        p = float(pt)
    except (TypeError, ValueError):
        return None
    tier, star = key
    entry = _entry_of(key)
    if entry is None:
        return None
    if tier in _TIER_BASE:
        start, _mx = _LEVEL_SPAN[(tier - 1) * 3 + (star - 1)]
    else:
        start = _KONTEN_START
    return entry + (p - start)


def _key(d: _dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def today_key(now: Optional[_dt.datetime] = None) -> str:
    return _key((now or _dt.datetime.now()).date())


def shift_key(day_key: str, days: int) -> str:
    """把 YYYY-MM-DD 往前推 days 天。"""
    try:
        d = _dt.datetime.strptime(day_key, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return day_key
    return _key(d - _dt.timedelta(days=days))


def load_history(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_history(path: str, history: dict) -> bool:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def prune(history: dict, today: str) -> dict:
    """丢掉超过 KEEP_DAYS 的快照，避免文件无限增长。"""
    for k in list(history.keys()):
        try:
            age = (_dt.datetime.strptime(today, "%Y-%m-%d")
                   - _dt.datetime.strptime(k, "%Y-%m-%d")).days
            if age > KEEP_DAYS:
                history.pop(k, None)
        except (ValueError, TypeError):
            history.pop(k, None)
    return history


def record_snapshot(history: dict, day_key: str, players: dict) -> dict:
    """把当天快照写进 history；同一天重抓覆盖当天那条。"""
    snap = {}
    for aid, info in (players or {}).items():
        if not isinstance(info, dict):
            continue
        entry = {
            "name": str(info.get("name") or aid),
            "rank": str(info.get("rank") or ""),
            "pt": info.get("pt"),
            "rank3": str(info.get("rank3") or ""),
            "pt3": info.get("pt3"),
            "level": info.get("level"),
            "level3": info.get("level3"),
        }
        snap[str(aid)] = entry
    history[day_key] = snap
    return history


def player_from_stats(aid, stats: dict) -> Optional[dict]:
    """把 lookup_player 的返回压成快照条目；pt 取不到就返回 None。"""
    if not isinstance(stats, dict) or not stats:
        return None
    pt = stats.get("pt")
    try:
        pt = float(pt) if pt is not None else None
    except (TypeError, ValueError):
        pt = None
    pt3 = stats.get("pt3")
    try:
        pt3 = float(pt3) if pt3 is not None else None
    except (TypeError, ValueError):
        pt3 = None
    if pt is None and pt3 is None:
        return None
    return {
        "name": str(stats.get("nickname") or aid),
        "rank": str(stats.get("rank") or ""),
        "pt": pt,
        "rank3": str(stats.get("rank3") or ""),
        "pt3": pt3,
        "level": stats.get("level"),
        "level3": stats.get("level3"),
    }


def pick_baseline(history: dict, today: str, days: int) -> Optional[str]:
    """找距 today-days 最近的快照日；找不到比今天更早的就返回 None。"""
    try:
        target = _dt.datetime.strptime(today, "%Y-%m-%d").date() - _dt.timedelta(days=days)
    except (ValueError, TypeError):
        return None
    cands = []
    for k, snap in history.items():
        if k == today or not isinstance(snap, dict) or not snap:
            continue
        try:
            d = _dt.datetime.strptime(k, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if d > _dt.datetime.strptime(today, "%Y-%m-%d").date():
            continue
        cands.append((abs((d - target).days), d, k))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], -x[1].toordinal()))
    return cands[0][2]


def compute_changes(history: dict, today: str, days: int,
                    aids) -> list:
    """返回按全局 PT 变动降序的 [(aid, name, rank_before, rank_after,
    before, after, delta, promoted)]。

    以四人 PT 为准；只比较两期都有四人 PT 的账号。PT 是段内点数、升段会
    重置，所以跨段位时用 global_pt() 折算成全局累计再相减，段位没变时
    全局差恰好等于裸 PT 差（完全兼容旧行为）。pt3 仅附加展示不参与排序。
    """
    base = pick_baseline(history, today, days)
    if not base:
        return []
    now_snap = history.get(today) or {}
    old_snap = history.get(base) or {}
    out = []
    seen = set()
    for aid in (aids or []):
        aid = str(aid)
        if aid in seen:
            continue
        seen.add(aid)
        n, o = now_snap.get(aid), old_snap.get(aid)
        if not n or not o:
            continue
        np, op = n.get("pt"), o.get("pt")
        if np is None or op is None:
            continue
        try:
            np_f, op_f = float(np), float(op)
        except (TypeError, ValueError):
            continue
        # 段位没变：直接裸 PT 差；跨段位：折算全局累计再相减
        n_key = _rank_to_key(n.get("rank")) or _level_to_key(n.get("level"))
        o_key = _rank_to_key(o.get("rank")) or _level_to_key(o.get("level"))
        rank_before = str(o.get("rank") or "")
        rank_after = str(n.get("rank") or "")
        if n_key and o_key and n_key != o_key:
            gn = global_pt(n.get("rank"), np, n.get("level"))
            go = global_pt(o.get("rank"), op, o.get("level"))
            delta = (gn - go) if (gn is not None and go is not None) else (np_f - op_f)
            promoted = True
        else:
            delta = np_f - op_f
            promoted = False
        out.append((aid, n.get("name") or aid,
                    rank_before, rank_after, op, np, delta, promoted))
    out.sort(key=lambda r: -r[6])
    return out


def _fmt_pt(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "?"
    return str(int(x)) if abs(x - round(x)) < 1e-6 else f"{x:.1f}"


def format_report(label: str, changes: list, baseline: Optional[str],
                  today: str) -> str:
    """把变动榜排成播报文本。"""
    head = f"【PT{label}】{today}"
    if not changes:
        return head + "\n暂无可比数据（订阅玩家不足两期快照，或两期都没拿到 PT）"
    lines = [head, f"对比: {baseline} → {today}", ""]
    for i, row in enumerate(changes, 1):
        # 兼容旧 6 元组（无段位晋升信息）
        if len(row) >= 8:
            _aid, name, rank_before, rank_after, before, after, delta, promoted = row[:8]
        else:
            _aid, name, rank, before, after, delta = row[:6]
            rank_before = rank_after = rank
            promoted = False
        sign = "+" if delta > 0 else ""
        if promoted and rank_before != rank_after:
            lines.append(f"{i}. {name} {rank_before}→{rank_after} "
                         f"{_fmt_pt(before)}→{_fmt_pt(after)}（{sign}{_fmt_pt(delta)}pt ↑段位）")
        else:
            lines.append(f"{i}. {name} {rank_after} "
                         f"{_fmt_pt(before)}→{_fmt_pt(after)}（{sign}{_fmt_pt(delta)}pt）")
    return "\n".join(lines)
