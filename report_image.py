# -*- coding: utf-8 -*-
"""把 PT 日/周/月变动榜渲染成一张图片表格（纯 PIL，不依赖 AstrBot）。

输入 reports = [{"label","baseline","today","changes"}, ...]，
changes 为 report.compute_changes 的 8 元组：
    (aid, name, rank_before, rank_after, before, after, delta, promoted)
涨用红、跌用绿（国内习惯）；↑段位单独标注。
"""
from __future__ import annotations

import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# 候选字体：容器里 wqy-zenhei 一定有，Noto 优先（更清晰）
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/AstrBot/data/plugin_data/steam_status_monitor_V3/fonts/NotoSansHans-Regular.otf",
    "C:/Windows/Fonts/msyh.ttc",
)

_COL_BG = (255, 255, 255)
_COL_HEAD = (38, 50, 64)
_COL_HEAD2 = (52, 68, 88)
_COL_STRIPE = (245, 247, 250)
_COL_TEXT = (33, 41, 54)
_COL_SUB = (110, 122, 138)
_COL_LINE = (223, 229, 236)
_COL_UP = (211, 47, 47)      # 涨/红
_COL_DOWN = (56, 142, 60)    # 跌/绿
_COL_FLAT = (120, 130, 142)
_COL_PROMO_BG = (255, 248, 225)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for p in _FONT_CANDIDATES:
        try:
            if os.path.isfile(p):
                return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_w(draw, text, font) -> int:
    try:
        b = draw.textbbox((0, 0), text, font=font)
        return b[2] - b[0]
    except Exception:
        return len(str(text)) * 12


def _fmt_pt(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "?"
    return str(int(x)) if abs(x - round(x)) < 1e-6 else f"{x:.1f}"


def render_report_png(reports: list, out_path: str, max_rows: int = 30) -> Optional[str]:
    """把多份报告渲染成一张 PNG。成功返回路径，失败返回 None。

    reports 每项: {"label": "日报", "baseline": "2026-11-20",
                   "today": "2026-11-21", "changes": [...]}
    """
    try:
        f_title = _font(24, bold=True)
        f_sub = _font(15)
        f_head = _font(17, bold=True)
        f_cell = _font(17)
        f_foot = _font(13)

        W = 760
        PAD = 22
        COLS = [56, 190, 186, 158, 118]  # # / 玩家 / 段位 / PT / 涨幅
        ROW_H = 34
        HEAD_H = 30
        SEC_GAP = 24

        # 估算一个够用的高度，画完再按实际 y 裁剪
        max_total = PAD + sum(
            40 + 22 + HEAD_H + (len(list((r.get("changes") or []))[:max_rows]) or 1) * ROW_H + 40
            for r in (reports or [])
        ) + SEC_GAP * len(reports or []) + PAD * 2

        img = Image.new("RGB", (W, max_total), _COL_BG)
        draw = ImageDraw.Draw(img)
        y = PAD // 2

        for report in (reports or []):
            changes = list(report.get("changes") or [])[:max_rows]
            label = report.get("label") or "报告"
            today = report.get("today") or ""
            baseline = report.get("baseline")

            # 分区标题条
            draw.rounded_rectangle([PAD, y, W - PAD, y + 34], radius=7, fill=_COL_HEAD)
            draw.text((PAD + 12, y + 4), f"【PT{label}】{today}", font=f_title, fill=(255, 255, 255))
            y += 40

            # 副标题（基线）
            sub = f"对比: {baseline} → {today}" if baseline else "暂无更早的快照作对比"
            draw.text((PAD + 4, y), sub, font=f_sub, fill=_COL_SUB)
            y += 22

            # 表头
            draw.rectangle([PAD, y, W - PAD, y + HEAD_H], fill=_COL_HEAD2)
            x = PAD
            headers = ["#", "玩家", "段位", "PT 变动", "涨幅"]
            for i, htxt in enumerate(headers):
                tw = _text_w(draw, htxt, f_head)
                draw.text((x + (COLS[i] - tw) // 2, y + 5), htxt, font=f_head, fill=(255, 255, 255))
                x += COLS[i]
            y += HEAD_H

            if not changes:
                draw.rectangle([PAD, y, W - PAD, y + ROW_H], fill=_COL_STRIPE)
                msg = "暂无可比数据（订阅玩家不足两期快照，或两期都没拿到 PT）"
                tw = _text_w(draw, msg, f_cell)
                draw.text((PAD + (sum(COLS) - tw) // 2, y + 7), msg, font=f_cell, fill=_COL_SUB)
                y += ROW_H
            else:
                for idx, row in enumerate(changes, 1):
                    if len(row) >= 8:
                        _aid, name, rbefore, rafter, before, after, delta, promoted = row[:8]
                    else:
                        _aid, name, rank, before, after, delta = row[:6]
                        rbefore = rafter = rank
                        promoted = False
                    bg = _COL_PROMO_BG if (promoted and rbefore != rafter) else (
                        _COL_STRIPE if idx % 2 == 0 else _COL_BG)
                    draw.rectangle([PAD, y, W - PAD, y + ROW_H], fill=bg)
                    draw.line([PAD, y + ROW_H, W - PAD, y + ROW_H], fill=_COL_LINE)

                    name = str(name)
                    seg = (f"{rbefore}→{rafter}" if (promoted and rbefore != rafter)
                           else str(rafter))
                    pt = f"{_fmt_pt(before)}→{_fmt_pt(after)}"
                    sign = "+" if delta > 0 else ""
                    gain = f"{sign}{_fmt_pt(delta)}pt"
                    if promoted and rbefore != rafter:
                        gain += " ↑"

                    col_delta = _COL_UP if delta > 0 else (_COL_DOWN if delta < 0 else _COL_FLAT)
                    cells = [str(idx), name, seg, pt, gain]
                    x = PAD
                    for ci, val in enumerate(cells):
                        font = f_head if ci == 4 else f_cell
                        color = col_delta if ci == 4 else _COL_TEXT
                        if ci == 1:
                            draw.text((x + 8, y + 7), val, font=font, fill=color)
                        else:
                            # 段位列可能较长，左对齐并允许略微溢出前先缩字号
                            tw = _text_w(draw, val, font)
                            if tw > COLS[ci] - 8 and ci in (1, 2, 3):
                                small = _font(14)
                                tw = _text_w(draw, val, small)
                                draw.text((x + (COLS[ci] - tw) // 2, y + 9), val, font=small, fill=color)
                            else:
                                draw.text((x + (COLS[ci] - tw) // 2, y + 7), val, font=font, fill=color)
                        x += COLS[ci]
                    y += ROW_H

            draw.text((PAD + 4, y + 2), "▲ 涨 / ▼ 跌    ↑段位 = 升段后按段位起点重新累计的净涨幅",
                      font=f_foot, fill=_COL_SUB)
            y += 20 + SEC_GAP

        img = img.crop((0, 0, W, min(y + PAD // 2, max_total)))
        img.save(out_path, "PNG")
        return out_path
    except Exception:
        return None
