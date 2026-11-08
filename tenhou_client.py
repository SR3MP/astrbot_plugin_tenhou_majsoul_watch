"""
天凤牌谱（已结束对局）解析

推送格式与云观战一致：对局开始 → 每局结算 → 对局完成。
牌谱元素与实时观战流的语义相同，所以役名/副露/手牌的解码统一放在 th_live，
文案骨架统一放在 broadcast；这里只做 XML → 同一套渲染器的搬运。
"""
from __future__ import annotations

import re
import hashlib
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import unquote

from .broadcast import event_head, finish_text, start_text
from .th_live import (
    _to_int,
    end_rows,
    format_agari_detail,
    format_ryuukyoku_detail,
    game_type_name,
)


def short_id(code: str) -> str:
    return hashlib.md5(code.encode()).hexdigest()[:4]


# =====================================================================
#  牌谱 XML 解析（与实时观战流同一套文案）
# =====================================================================
LOG_URL = "https://tenhou.net/0/log/?{code}"


class LogReplayParser:
    """拉取天凤牌谱 XML，按 wg 推送格式输出对局详情。"""

    def __init__(self, code_or_url: str):
        self.code = self._extract_code(code_or_url)
        self.xml: Optional[str] = None

    @staticmethod
    def _extract_code(s: str) -> str:
        s = s.strip()
        m = re.search(r"([0-9]{10}gm-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{8})", s)
        if m:
            return m.group(1)
        return s

    def fetch(self) -> str:
        if not self.xml:
            url = LOG_URL.format(code=self.code)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                self.xml = r.read().decode("utf-8", "replace")
        return self.xml

    def parse(self) -> str:
        try:
            xml = self.fetch()
        except Exception as e:
            return f"牌谱拉取失败: {e}\n码: {self.code}"
        if "<mjloggm" not in xml:
            return f"找不到牌谱（码无效或已归档）: {self.code}"
        try:
            return self._render(xml)
        except Exception as e:
            return f"牌谱解析失败: {type(e).__name__}: {e}\n码: {self.code}"

    def _render(self, xml: str) -> str:
        root = ET.fromstring(xml)
        players = ["", "", "", ""]
        go_type = ""
        rounds: list[dict] = []
        cur: Optional[dict] = None
        scores = [25000] * 4
        seed = "0,0"
        owari = ""

        def new_round() -> dict:
            return {"seed": seed, "ev": None,
                    "before": list(scores), "after": list(scores)}

        for el in root.iter():
            tag = el.tag
            if tag == "GO":
                go_type = el.get("type", "")
            elif tag == "UN":
                for i in range(4):
                    nm = unquote(el.get(f"n{i}", ""))
                    if nm:
                        players[i] = nm
            elif tag == "INIT":
                if cur and cur.get("ev") is not None:
                    rounds.append(cur)
                seed = el.get("seed", seed)
                cur = new_round()
            elif tag in ("AGARI", "RYUUKYOKU"):
                if cur is None:
                    cur = new_round()
                # AGARI/RYUUKYOKU 的属性名与实时观战流一致，直接交给同一套渲染器
                cur["ev"] = (tag, dict(el.attrib))
                # 牌谱把结算挂在 AGARI/RYUUKYOKU 的 sc 上（百点单位）
                vals = (el.get("sc") or "").split(",")
                if len(vals) >= 8:
                    cur["before"] = [_to_int(vals[i * 2]) * 100 for i in range(4)]
                    scores = [
                        (_to_int(vals[i * 2]) + _to_int(vals[i * 2 + 1])) * 100
                        for i in range(4)
                    ]
                    cur["after"] = list(scores)
                if el.get("owari"):
                    owari = el.get("owari")

        if cur and cur.get("ev") is not None:
            rounds.append(cur)

        mid = short_id(self.code)
        names = [p for p in players if p] or ["?"]
        link = f"https://tenhou.net/0/?log={self.code}"
        # 牌谱是事后翻的，时间戳只属于当时，不印在标题上
        lines = [start_text(mid, f"天凤 {game_type_name(go_type)}", names, "",
                            (), [link, f"共 {len(rounds)} 局已结算"]), ""]

        for r in rounds:
            tag, ev = r["ev"]
            if tag == "AGARI":
                lines.append(event_head(mid, "小局结算", "") + "\n" +
                             format_agari_detail(ev, players, r["seed"]))
            else:
                lines.append(event_head(mid, "流局", "") + "\n" +
                             format_ryuukyoku_detail(ev, players, r["seed"]))
            lines.append("")

        if not rounds:
            lines.append("本牌谱没有已结算的小局（可能刚开局或已被清理）。")
            return "\n".join(lines)

        rows, notes = end_rows(owari, players, scores)
        lines.append(finish_text(mid, rows, (), notes))
        return "\n".join(lines)
