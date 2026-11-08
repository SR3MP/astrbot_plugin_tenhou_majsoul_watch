# -*- coding: utf-8 -*-
"""天凤对局中(实时)信息跟踪 - 纯 WebSocket 实现，不依赖 Playwright。

协议(已验证):
  1. GET https://tenhou.net/0/wg/0.js  -> 进行中对局列表(含玩家名/wg token)
  2. wss://b-ww.mjv.jp 发 {"tag":"HELO","name":..,"sx":"M"}
  3. 发 {"tag":"WG","id":<wg>,"tw":0}  -> 收 GO/UN/INITBYLOG/WGC
  4. INITBYLOG.childNodes = 当前局完整历史; WGC.childNodes = 之后增量
     流延迟约 5 分钟，且补发完当前局服务器就断开，所以要反复重订阅；
     局间空档和码彻底失效都是"干净空响应"，靠段位房列表区分两者。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import ssl
import threading
import time
import logging
import base64
import urllib.request
from urllib.parse import unquote

import websockets

from .broadcast import (event_head, finish_text, now_hm, room_start_text,
                        score_rows)

_LOG = logging.getLogger("astrbot")

WS_URL = "wss://b-ww.mjv.jp"
WG_LIST_TPL = "https://tenhou.net/0/wg/{lobby}.js"

# =====================================================================
#  牌名
# =====================================================================
TILE_NAMES = (
    [f"{i}m" for i in range(1, 10)]
    + [f"{i}p" for i in range(1, 10)]
    + [f"{i}s" for i in range(1, 10)]
    + ["1z", "2z", "3z", "4z", "5z", "6z", "7z"]
)
RED_FIVES = {16: "0m", 52: "0p", 88: "0s"}


def tile_name(tid: int) -> str:
    if tid in RED_FIVES:
        return RED_FIVES[tid]
    if 0 <= tid < 136:
        return TILE_NAMES[tid // 4]
    return f"?{tid}"


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# =====================================================================
#  对局列表
# =====================================================================
def parse_lobby(code: str) -> str:
    """大厅码 → 列表文件名。

    取法照天凤观战页 /0/wg/ 自己的脚本：L0000→0、C0001→10001。
    对战房码形如 C00014237 = C(大厅 0001) + 房间规则 4237，房号只取 C 后 4 位。
    """
    c = (code or "").strip().upper()
    m = re.match(r"^L(\d{1,4})$", c)
    if m:
        return str(int(m.group(1)))
    m = re.match(r"^C(\d{4})(\d*)$", c)
    if m:
        return str(10000 + int(m.group(1)))
    if c.isdigit() and len(c) <= 5:
        return c
    return "0"


def fetch_games(lobby: str = "0") -> list[dict]:
    """拉取某大厅的进行中对局列表。每条: wg, room, time, rule, names, dans, rates"""
    url = WG_LIST_TPL.format(lobby=parse_lobby(lobby))
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://tenhou.net/0/wg/"}
    )
    raw = urllib.request.urlopen(req, timeout=12, context=_ssl_ctx()).read().decode(
        "utf-8", "replace"
    )
    m = re.search(r"sw\((\[.*\])\)", raw, re.S)
    if not m:
        return []
    arr = json.loads(m.group(1))
    games = []
    for row in arr:
        f = row.split(",")
        if len(f) < 16:
            continue
        names = []
        for idx in (4, 7, 10, 13):
            try:
                names.append(base64.b64decode(f[idx]).decode("utf-8", "replace"))
            except Exception:
                names.append("?")
        games.append(
            {
                "wg": f[0],
                "room": f[1],
                "time": f[2],
                "rule": f[3],
                "names": names,
                "dans": [f[5], f[8], f[11], f[14]],
                "rates": [f[6], f[9], f[12], f[15]],
            }
        )
    return games


# =====================================================================
#  WS 订阅
# =====================================================================
async def watch_once(wg_id: str, timeout: float = 15.0) -> dict:
    """订阅一次观战流，返回 {names, events, error}。

    实测：天凤的观战流是"补发当前局历史(INITBYLOG) + 一小段增量(WGC)"
    之后服务器就主动断开，并不是一直推。所以这里把收到的所有节点合并成
    一条事件流交给调用方去重。局间空档（上一局打完、下一局还没开）订阅
    也是干净的空响应，不等于对局结束；对局收尾后码还会活着一小会儿，
    这期间补出来的是最后一手，那一手的 AGARI 带着 owari（全场精算点位），
    想要最终名次就得在这个窗口里订阅到它，窗口一过就只剩空响应。
    """
    events: list[dict] = []
    names = ["", "", "", ""]
    error = ""
    got_state = False
    try:
        async with websockets.connect(
            WS_URL,
            ssl=_ssl_ctx(),
            origin="https://tenhou.net",
            additional_headers={"User-Agent": "Mozilla/5.0"},
            open_timeout=10,
        ) as ws:
            await ws.send(json.dumps({"tag": "HELO", "name": "NoName", "sx": "M"}))
            await asyncio.sleep(0.4)
            await ws.send(json.dumps({"tag": "WG", "id": wg_id, "tw": 0}))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    d = await asyncio.wait_for(ws.recv(), timeout=3)
                except asyncio.TimeoutError:
                    if got_state:
                        break
                    continue
                except Exception:
                    break
                try:
                    msg = json.loads(d)
                except Exception:
                    continue
                tag = msg.get("tag")
                if tag == "UN":
                    for i in range(4):
                        v = msg.get(f"n{i}")
                        if v:
                            try:
                                names[i] = unquote(v)
                            except Exception:
                                names[i] = str(v)
                elif tag == "INITBYLOG":
                    got_state = True
                    for k in msg.get("childNodes") or []:
                        if isinstance(k, dict):
                            events.append(k)
                elif tag == "WGC":
                    for k in msg.get("childNodes") or []:
                        if isinstance(k, dict):
                            events.append(k)
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:120]}"
    return {"names": names, "events": events, "error": error}


# =====================================================================
#  天凤 yaku ID → 中文名
#
#  ID 表取自三份互相印证的牌谱实现（NegativeMjark/tenhou-log 的
#  TenhouDecoder.YAKU、dnovikoff/tenhou 的 log/format.txt、
#  tenhou-paifu-to-json 譯碼表），并用真实牌谱的 符/点数/限役 校验过
#  （例：yaku="8,1,52,1" 断幺+宝牌 2番，30符亲家荣和 2900 点，吻合）。
# =====================================================================
YAKU = {
    0: "门前清自摸和", 1: "立直", 2: "一发", 3: "枪杠", 4: "岭上开花",
    5: "海底摸月", 6: "河底捞鱼", 7: "平和", 8: "断幺九", 9: "一盃口",
    10: "役牌:自风东", 11: "役牌:自风南", 12: "役牌:自风西", 13: "役牌:自风北",
    14: "役牌:场风东", 15: "役牌:场风南", 16: "役牌:场风西", 17: "役牌:场风北",
    18: "役牌:白", 19: "役牌:发", 20: "役牌:中",
    21: "双立直", 22: "七对子", 23: "混全带幺九", 24: "一气通贯", 25: "三色同顺",
    26: "三色同刻", 27: "三杠子", 28: "对对和", 29: "三暗刻", 30: "小三元",
    31: "混老头", 32: "二盃口", 33: "纯全带幺九", 34: "混一色", 35: "清一色",
    36: "人和",
    37: "天和", 38: "地和", 39: "大三元", 40: "四暗刻", 41: "四暗刻单骑",
    42: "字一色", 43: "绿一色", 44: "清老头", 45: "九莲宝灯", 46: "纯正九莲宝灯",
    47: "国士无双", 48: "国士无双十三面", 49: "大四喜", 50: "小四喜", 51: "四杠子",
    52: "宝牌", 53: "里宝牌", 54: "赤宝牌",
}

# AGARI.ten 的第三段是限役等级
_LIMIT_NAMES = {0: "", 1: "满贯", 2: "跳满", 3: "倍满", 4: "三倍满", 5: "役满"}

# RYUUKYOKU.type
_RYUUKYOKU_TYPE = {
    "yao9": "九种九牌", "reach4": "四家立直", "ron3": "三家和了",
    "kan4": "四杠散了", "kaze4": "四风连打", "nm": "流局满贯",
}


def yaku_lines(yaku_str: str, yakuman_str: str = "") -> str:
    """'1,1,52,2' → ' 1 立直\\n 2 宝牌'；役满列表单独一行。

    牌谱会给没命中的里宝牌/宝牌补 0 番占位（如 '53,0'），这类不显示。
    """
    out = []
    parts = [p for p in (yaku_str or "").split(",") if p != ""]
    for i in range(0, len(parts) - 1, 2):
        try:
            yid, han = int(parts[i]), int(parts[i + 1])
        except ValueError:
            continue
        if han <= 0:
            continue
        out.append(f" {han} {YAKU.get(yid, f'役{yid}')}")
    for y in [p for p in (yakuman_str or "").split(",") if p.strip()]:
        try:
            out.append(f" 役满 {YAKU.get(int(y), f'役{y}')}")
        except ValueError:
            continue
    return "\n".join(out)


def round_name(seed: str) -> str:
    """seed/ba 'r,honba,...' → '東3局 2本場'

    seed 形如 '2,2,0,1,0,0'（依次是 局数、本場、供托、骰子、骰子、宝牌指示牌）；
    观战流里当前局的历史用 INITBYLOG 补发、不带 seed，所以空值要能退化显示。
    """
    parts = (seed or "").split(",")
    if len(parts) < 2:
        return "当前局"
    try:
        r, honba = int(parts[0]), int(parts[1])
    except ValueError:
        return "当前局"
    wind = ["東", "南", "西", "北"][min(r // 4, 3)]
    return f"{wind}{r % 4 + 1}局 {honba}本場"


# =====================================================================
#  天凤官方规则名（取自 /0/wg/ update() 的位运算）
# =====================================================================
_GT_NAN = 0x0008
_GT_SANMA = 0x0010
_GT_TOKU = 0x0020
_GT_HIGH = 0x0080
_GT_CHIP = 0x0200
_GT_JANS = 0x0400
_TAKU_NAMES = "般上特鳳若銀琥孔"
# 天凤段位表（取自 /0/wg/ 页面 T_DAN 数组，索引即段位号）
_T_DAN = [
    "新人", "９級", "８級", "７級", "６級", "５級", "４級", "３級", "２級", "１級",
    "初段", "二段", "三段", "四段", "五段", "六段", "七段", "八段", "九段", "十段",
    "天鳳位",
]


def dan_name(d: str) -> str:
    """段位号字符串 → 段位名，如 '17' → '八段'。"""
    try:
        i = int(d)
        if 0 <= i < len(_T_DAN):
            return _T_DAN[i]
    except (TypeError, ValueError):
        pass
    return f"?段({d})"


def _rating_text(v) -> str:
    """列表里的 rating 是 '1880.01' 这种字符串；原样给播报，只去掉整数多余的 .0。

    '0' 是列表没给这一列（新人/特殊房），不是真的 R0，空着。
    """
    s = str(v if v is not None else "").strip()
    if not s:
        return ""
    try:
        x = float(s)
    except ValueError:
        return s
    if x == 0:
        return ""
    return str(int(x)) if abs(x - int(x)) < 1e-9 else s


def seat_bits(g: dict) -> list:
    """四家的 `段位Rrating`，与 g["names"] 下标对位，查不到的座位留空串。

    特荘房（lobby=1）实测 dans 全是 '0'：那是列表没下发段位，不是"新人"，
    这种行只写 R。
    """
    names = g.get("names") or []
    dans = g.get("dans") or []
    rates = g.get("rates") or []
    out = []
    for i in range(len(names)):
        text = ""
        d = str(dans[i]).strip() if i < len(dans) else ""
        if d and d != "0":
            text += dan_name(d)
        r = _rating_text(rates[i]) if i < len(rates) else ""
        if r:
            text += f"R{r}"
        out.append(text)
    return out


def seats(g: dict) -> tuple[list, list]:
    """(有名字的座位, 对位的段位+rating)。

    必须成对过滤：列表里三麻/空位会把 names 掏出一个洞，只过滤 names 的话
    段位会串到隔壁家头上。
    """
    names = g.get("names") or []
    bits = seat_bits(g)
    pairs = [(n, b) for n, b in zip(names, bits) if n]
    return [n for n, _ in pairs], [b for _, b in pairs]


def format_players(g: dict) -> str:
    """把对局 g 的四家格式化为 '段位Rrating/名字' 串（空格分隔）。

    g 需含 names(4)、dans(4)、rates(4)。字段取自 /0/wg/0.js 每行：
      aa[4,7,10,13]=名字(baee64)  aa[5,8,11,14]=段位  aa[6,9,12,15]=rating
    没名字的座位整家不印，和官方 `if(!un[who]) continue`、seats() 同口径。
    """
    names = g.get("names", []) or []
    dans = g.get("dans", []) or []
    rates = g.get("rates", []) or []
    parts = []
    for i in range(4):
        nm = names[i] if i < len(names) else ""
        if not nm:
            continue
        dn = dan_name(str(dans[i])) if i < len(dans) else "?"
        rt = rates[i] if i < len(rates) else "0"
        parts.append(f"{dn}R{rt}/{nm}")
    return " ".join(parts)


def _gt_taku(w: int) -> int:
    return ((w & _GT_TOKU) >> 4) | ((w & _GT_HIGH) >> 7)


def _gt_isjans(w: int) -> bool:
    return (w & (_GT_CHIP | _GT_JANS)) != 0


def game_type_name(rule: str) -> str:
    """天凤 GO type/rule（十进制字符串）→ 规则名，如 '四鳳南喰赤'。

    位定义（来自 wg 页面 update()）：
      bit1 赤（0=有赤→显示赤），bit2 喰（0=有食断→显示喰），
      bit3 南/東，bit4 三/四，bit5/7 段位，bit6 速，bit9/10 雀荘。
    """
    try:
        w = int(rule) & 0xFFFF
    except (TypeError, ValueError):
        return str(rule)
    s = ""
    s += "三" if w & _GT_SANMA else "四"
    s += _TAKU_NAMES[(4 if _gt_isjans(w) else 0) + _gt_taku(w)]
    s += "南" if w & _GT_NAN else "東"
    if _gt_isjans(w):
        s += "祝"
        s += "０" if not (w & _GT_CHIP) else ("５" if w & _GT_JANS else "２")
    else:
        s += "" if w & 0x0004 else "喰"
        s += "" if w & 0x0002 else "赤"
        s += "速" if w & 0x0040 else ""
    return s


def _format_hand(hai_str: str) -> str:
    """天凤 hai '28,29,...' → mspz 报法，如 '888m 887777p 675s 11z'。

    mspz 标准：万子数字+m，筒子数字+p，索子数字+s，
    字牌用 z 加 1-7（1东 2南 3西 4北 5白 6发 7中），赤5 记为 0。
    同一花色内按牌面升序，不同花色按 m/p/s/z 顺序输出。
    """
    if not hai_str:
        return ""
    # 按花色分桶: m/p/s/z，桶内存 (排序值, 显示字符)；赤5 显示 0 但排在 5 的位置
    groups = {"m": [], "p": [], "s": [], "z": []}
    for t in hai_str.split(","):
        if not t:
            continue
        tid = int(t)
        if tid in RED_FIVES:  # 赤5: 0m/0p/0s
            red = RED_FIVES[tid]
            groups[red[-1]].append((5, "0"))
            continue
        if 0 <= tid < 108:  # 数牌
            suit = ["m", "p", "s"][tid // 36]
            num = (tid % 36) // 4 + 1
            groups[suit].append((num, str(num)))
        elif 108 <= tid < 136:  # 字牌: 1z-7z
            n = tid // 4 - 26
            groups["z"].append((n, str(n)))
        # 其他非法 tid 忽略
    parts = []
    for suit in ("m", "p", "s", "z"):
        if groups[suit]:
            parts.append(
                "".join(lbl for _, lbl in sorted(groups[suit], key=lambda x: x[0])) + suit
            )
    return " ".join(parts)


def _format_melds(m_str: str) -> str:
    """天凤副露数字码 → 牌面串，与雀魂同一口径：只列牌面，不带吃/碰/杠标签。

    AGARI/N 的 m 是若干十进制整数（逗号分隔），每个整数按位打包：
      bit0-1 来源家、bit2 吃、bit3/4 碰或加杠、bit5 拔（三人）、否则杠。
    位定义与牌谱解码器（NegativeMjark/tenhou-log 的 Meld.decode）一致。
    拔北不进牌面，累计成 `拔北×n` 挂在这一行末尾（同雀魂）。
    """
    if not m_str:
        return ""
    out = []
    nuki = 0
    for part in m_str.split(","):
        part = part.strip()
        if not part.lstrip("-").isdigit():
            continue
        try:
            label, tiles = _decode_meld(int(part))
        except Exception:
            continue
        if label == "拔":
            nuki += 1
            continue
        faces = _tiles_str(tiles)
        if faces:
            out.append(faces)
    if nuki:
        out.append(f"拔北×{nuki}")
    return " ".join(out)


def _tiles_str(tiles: list) -> str:
    """一个副露的牌面拼成 '340s' / '111z'，与手牌那一行同一套 mspz 记法。

    一个副露内牌面同花色，赤5 显示 '0' 但排在 5 的位置，与 _format_hand 同一口径。
    """
    suit = ""
    faces = []
    for t in tiles:
        n = tile_name(t)
        if len(n) == 2 and n[1] in "mpsz":
            suit = n[1]
            faces.append((5 if n[0] == "0" else int(n[0]), n[0]))
    return "".join(lbl for _, lbl in sorted(faces, key=lambda x: x[0])) + suit


def _decode_meld(code: int) -> tuple:
    frm = code & 0x3
    if code & 0x4:  # 吃
        t0, t1, t2 = (code >> 3) & 3, (code >> 5) & 3, (code >> 7) & 3
        base = (code >> 10) // 3
        base = (base // 7) * 9 + base % 7
        return "吃", [t0 + 4 * base, t1 + 4 * (base + 1), t2 + 4 * (base + 2)]
    if code & 0x18:  # 碰 / 加杠
        t4 = (code >> 5) & 3
        t0, t1, t2 = ((1, 2, 3), (0, 2, 3), (0, 1, 3), (0, 1, 2))[t4]
        base = (code >> 9) // 3
        tiles = [t0 + 4 * base, t1 + 4 * base, t2 + 4 * base]
        if code & 0x8:
            return "碰", tiles
        tiles.append(t4 + 4 * base)
        return "加杠", tiles
    if code & 0x20:  # 拔（三人麻将）
        return "拔", [code >> 8]
    base = (code >> 8) // 4  # 杠
    tiles = [4 * base, 4 * base + 1, 4 * base + 2, 4 * base + 3]
    return ("暗杠" if frm == 0 else "明杠"), tiles


def _to_int(v, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _sc_lines(sc: str, names: list) -> list:
    """AGARI/RYUUKYOKU 的 sc（每家 变动前,变动，百点）→ 四家点数行。"""
    vals = (sc or "").split(",")
    if len(vals) < 8:
        return []
    before = [_to_int(vals[i * 2]) * 100 for i in range(4)]
    after = [(b + _to_int(vals[i * 2 + 1]) * 100) for i, b in enumerate(before)]
    return score_rows(names, before, after)


def _han_total(ev: dict) -> str:
    """番数：把 yaku 里各家番数加起来；役满另算（役满没有番数可加，整列省掉）。"""
    if (ev.get("yakuman") or "").strip(", "):
        return ""
    parts = [p for p in (ev.get("yaku") or "").split(",") if p != ""]
    han = 0
    for i in range(0, len(parts) - 1, 2):
        try:
            h = int(parts[i + 1])
        except ValueError:
            continue
        if h > 0:
            han += h
    return f"{han}番" if han else ""


def format_agari_detail(ev: dict, names: list, seed: str = "") -> str:
    """把实时 AGARI 节点格式化为单局结算（全部取牌谱原值，不做推算）。"""
    who = _to_int(ev.get("who", 0))
    fw = str(ev.get("fromWho", who))
    ten = (ev.get("ten") or "0,0,0").split(",")
    fu = ten[0] if ten and ten[0] else "?"
    total = _to_int(ten[1]) if len(ten) > 1 else 0
    limit = _LIMIT_NAMES.get(_to_int(ten[2]) if len(ten) > 2 else 0, "")
    is_tsumo = fw == str(who)
    winner = names[who] if who < len(names) and names[who] else f"player{who}"

    head = f"{round_name(seed)} {winner} " + ("ツモ " if is_tsumo else "ロン ")
    if not is_tsumo:
        fwi = _to_int(fw, -1)
        disc = names[fwi] if 0 <= fwi < len(names) and names[fwi] else "?"
        head += f"{disc} "
    han = _han_total(ev)
    head += f"{total}点 {han}{fu}符"
    if limit:
        head += f" {limit}"
    lines = [head]

    hand = _format_hand(ev.get("hai", ""))
    if hand:
        lines.append(f"手牌: {hand}")
    melds = _format_melds(ev.get("m", ""))
    if melds:
        lines.append(f"副露: {melds}")
    yaku = yaku_lines(ev.get("yaku", ""), ev.get("yakuman", ""))
    if yaku:
        lines.append(yaku)
    lines.extend(_sc_lines(ev.get("sc", ""), names))
    return "\n".join(lines)


def format_ryuukyoku_detail(ev: dict, names: list, seed: str = "") -> str:
    """把实时 RYUUKYOKU 节点格式化为流局结算。"""
    t = str(ev.get("type", "") or "")
    kind = _RYUUKYOKU_TYPE.get(t, "荒牌" if not t else t)
    lines = [f"{round_name(seed)} 流局（{kind}）"]
    # 流局时牌谱只亮出听牌者的手牌（hai0..hai3），据此判断听牌家
    tenpai = []
    for i in range(4):
        if ev.get(f"hai{i}"):
            nm = names[i] if i < len(names) else ""
            tenpai.append(nm or f"player{i}")
    if tenpai:
        lines.append("听牌: " + " ".join(tenpai))
    else:
        lines.append("听牌: 无")
    lines.extend(_sc_lines(ev.get("sc", ""), names))
    return "\n".join(lines)


def sc_to_scores(sc: str) -> list:
    """AGARI/RYUUKYOKU 的 sc="点数(百点),变化,…" → 四家结算后的真实点数。"""
    vals = (sc or "").split(",")
    if len(vals) >= 8:
        return [
            (_to_int(vals[i * 2]) + _to_int(vals[i * 2 + 1])) * 100 for i in range(4)
        ]
    return []


def end_rows(owari: str, names: list, scores: list = None) -> tuple:
    """终局结果行 (按名次排好的 [(名字, 点数, 精算点位)], 补充说明)。

    owari 是 "点数(百点),点位" 交替的四家序列（实测
    owari="247,15.0,536,73.0,235,-6.0,182,-82.0"）。整串拿不到时退回最近一次
    sc 里的真实点数，并说明没有精算点位；两者都没有就交空表给调用方兜底。
    """
    nn = list(names or [])
    known = any(nn)
    vals = [v.strip() for v in (owari or "").split(",")]
    seats = len(vals) // 2
    if seats >= 3 and known:
        rows = []
        for i in range(min(seats, len(nn))):
            nm = nn[i]
            pts = _to_int(vals[i * 2]) * 100
            uma = vals[i * 2 + 1]
            try:
                float(uma)      # 只校验能不能当数，不留下 float：-15.0 转完会印成 -15
            except ValueError:
                uma = None
            if not nm and not pts:
                continue          # 三麻空着的那一格
            rows.append((nm or f"player{i}", pts, uma))
        rows.sort(key=lambda r: -r[1])
        return rows, []
    if scores and known:
        rows = [
            (nn[i] or f"player{i}", int(scores[i]), None)
            for i in range(min(len(scores), len(nn)))
            if nn[i] or scores[i]
        ]
        rows.sort(key=lambda r: -r[1])
        return rows, ["注: 无终局结算，点数为最后一次小局结算值，无点位"]
    return [], []


def end_render(mid: str, p: dict, focus=()) -> str:
    """按一份终局数据 + 本群订阅玩家渲染【对局完成】。

    ThGameSession 自己报终局、自动跟踪替各群报终局都走这里，保证两种入口
    出来的格式一模一样。
    """
    rows, notes = end_rows(p.get("owari", ""), p.get("names") or [],
                           p.get("scores") or [])
    if p.get("reason"):
        notes.append(f"注: {p['reason']}")
    if p.get("conn_err") and not p.get("seen"):
        notes.append(f"注: 观战流异常 {p['conn_err']}")
    if not rows:
        if not p.get("seen") and not p.get("scores"):
            # 一条牌流都没订到：多半是码归档了或填错了，别装作这局打完了
            return (f"#{mid} 观战流无任何数据，无播报内容。\n"
                    "可用 wglog <牌谱码> 查询完整牌谱。")
        notes.append("注: 无终局结算数据，无名次，可用 wglog <牌谱码> 查询")
    return finish_text(mid, rows, focus or (), notes)


def is_finish_text(text: str) -> bool:
    """这条是不是一场对局的终局播报（end_render 的两种收尾都算）。"""
    return "【对局完成】" in text or "观战流无任何数据" in text


def short_id(code: str) -> str:
    import hashlib
    return hashlib.md5(code.encode()).hexdigest()[:4]


# =====================================================================
#  单场对局跟踪
# =====================================================================
class ThGameSession:
    """跟踪一场天凤对局，直到它结束。

    天凤的观战流是延迟约 5 分钟、且补发完当前局就主动断开，所以这里
    按固定间隔重新订阅，把每次补发的节点按 (tag,ba,sc,...) 去重后推送，
    等价于只推"没见过的小局结算"。局间空档也会拿到空响应，所以给了
    listed 回调时以"还在不在段位房列表"为准；没有的话退回老规矩：
    连续几次干净空响应就判终局。
    """

    REFRESH = 20.0          # 重新订阅间隔（秒）
    WATCH_TIMEOUT = 12.0    # 单次订阅最长等待
    MAX_LIFE = 4 * 3600.0   # 超过 4 小时还没结束就放弃跟踪

    def __init__(self, wg_id: str, push, names=None, room: str = "", rule: str = "",
                 watchers=None, listed=None, on_finish=None):
        self.wg_id = wg_id
        # async def push(text)；传 None 表示局内消息全部丢弃（自动跟踪用）
        self.push = push
        # 终局数据回调 f(payload:dict)；给了它就完全不发消息，只交数据
        self.on_finish = on_finish
        self.mid = short_id(wg_id)
        # 段位房列表快照查询（wg 码 → 还在不在列表）；没有就退化成纯空响应判定
        self.listed = listed
        nn = [n for n in (names or [])][:4]
        self.names = nn + [""] * (4 - len(nn))
        # 群里订阅的玩家名：终局那句点评按他们来讲
        self.watchers = {w for w in (watchers or ()) if w}
        self.room = room
        self.rule = rule
        self.rule_name = game_type_name(rule) if rule else ""
        self.seen = set()
        self.scores: list[int] = []   # 最近一次真实点数（百点换算后的点数）
        self.cur_seed = ""
        self.oya = None
        self.hands = 0
        self.empty = 0
        self.conn_err = ""
        self.finished = False
        self._err_pushed = False
        self._task = None
        self._started = time.time()

    # ---- 生命周期 ---------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self.finished

    def label(self) -> str:
        who = "/".join(n for n in self.names if n) or self.wg_id
        return f"#{self.mid} {self.rule_name or '天凤'} {who}"

    # ---- 主循环 -----------------------------------------------------
    async def _run(self) -> None:
        while not self.finished and time.time() - self._started < self.MAX_LIFE:
            try:
                res = await watch_once(self.wg_id, timeout=self.WATCH_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                res = {"names": [], "events": [], "error": f"{type(e).__name__}: {e}"}

            nn = [n for n in (res.get("names") or []) if n]
            if len(nn) >= 2:
                got = list(res["names"])[:4]
                self.names = got + [""] * (4 - len(got))

            events = res.get("events") or []
            if events:
                self.empty = 0
                await self._ingest(events)
            elif res.get("error"):
                self.conn_err = res["error"]
                if not self._err_pushed and not self.seen:
                    self._err_pushed = True
                    await self._emit(
                        f"#{self.mid} 观战连接异常（{self.conn_err}），后台会持续重试。"
                    )
            else:
                # 干净的空响应有两种可能：这一局刚打完、下一局还没开（牌流里
                # 没东西可补发），或者码已经随对局归档。实测牌流有约 5 分钟
                # 延迟，所以前者很常见，别把局间空档判成终局：只要对局还在
                # 段位房列表里就继续等，从列表消失后再连续空到阈值才收场。
                self.empty += 1
                if await self._in_list():
                    self.empty = 0
                elif self.empty >= (2 if (self.seen or self.scores) else 3):
                    await self._finish("")
                    break
            if self.finished:
                break
            await asyncio.sleep(self.REFRESH)

        if not self.finished:
            await self._finish("", "跟踪超时（4 小时未见终局）")

    async def _in_list(self) -> bool:
        """这局还在不在段位房列表；回调可以是同步或 async 的。"""
        if not self.listed:
            return False
        try:
            r = self.listed(self.wg_id)
            if inspect.isawaitable(r):
                r = await r
            return bool(r)
        except Exception:
            return False

    # ---- 事件处理 ---------------------------------------------------
    async def _ingest(self, events: list) -> None:
        for ev in events:
            tag = ev.get("tag")
            if tag == "INIT":
                self.cur_seed = ev.get("seed") or self.cur_seed
                self.oya = _to_int(ev.get("oya"), self.oya if self.oya is not None else 0)
                self._apply_ten(ev.get("ten", ""))
            elif tag in ("AGARI", "RYUUKYOKU"):
                key = (
                    tag,
                    ev.get("ba", ""),
                    ev.get("sc", ""),
                    ev.get("who", ""),
                    ev.get("machi", ""),
                )
                if key in self.seen:
                    continue
                self.seen.add(key)
                self.hands += 1
                if tag == "AGARI":
                    await self._emit(
                        event_head(self.mid, "小局结算", now_hm()) + "\n"
                        + format_agari_detail(ev, self.names, self.cur_seed)
                    )
                else:
                    await self._emit(
                        event_head(self.mid, "流局", now_hm()) + "\n"
                        + format_ryuukyoku_detail(ev, self.names, self.cur_seed)
                    )
                self._apply_sc(ev.get("sc", ""))
                if ev.get("owari"):
                    await self._finish(ev["owari"])
                    return

    def _apply_ten(self, ten: str) -> None:
        vals = (ten or "").split(",")
        if len(vals) >= 4:
            self.scores = [_to_int(v) * 100 for v in vals[:4]]

    def _apply_sc(self, sc: str) -> None:
        got = sc_to_scores(sc)
        if got:
            self.scores = got

    # ---- 终局 -------------------------------------------------------
    def _payload(self, owari: str, reason: str = "") -> dict:
        return {
            "wg": self.wg_id,
            "rule": self.rule,
            "names": list(self.names),
            "owari": owari or "",
            "scores": list(self.scores),
            "seen": bool(self.seen),
            "reason": reason,
            "conn_err": self.conn_err,
        }

    async def _finish(self, owari: str, reason: str = "") -> None:
        if self.finished:
            return
        self.finished = True
        p = self._payload(owari, reason)
        if self.on_finish:
            # 自动跟踪的会话：只交数据，文案由各群自己的订阅玩家渲染
            try:
                self.on_finish(p)
            except Exception as e:
                _LOG.warning(f"[th] 终局回调失败: {e}")
            return
        await self._emit(end_render(self.mid, p, self.watchers))

    async def _emit(self, text: str) -> None:
        if self.push is None:
            return      # 自动跟踪：局内消息丢弃，只有终局数据有用
        try:
            await self.push(text)
        except Exception as e:
            _LOG.warning(f"[th] 推送失败: {e}")

    # ---- 查询 -------------------------------------------------------
    def brief(self) -> str:
        if self.scores:
            sc = " ".join(
                f"{self.names[i]} {self.scores[i]}" for i in range(4)
                if self.names[i]
            )
            return f"{self.label()} 已结算{self.hands}局 | {sc}"
        return f"{self.label()} 已结算{self.hands}局 | 比分未取到"


# =====================================================================
#  订阅玩家的开局/散场自动播报
# =====================================================================
class ThWatcher:
    """只读段位房列表：订阅玩家一进房就推【对局开始】，从列表消失就推【对局完成】。

    逐局信息（每局谁和了、比分）仍然只有显式 wg 开的那场会播。发现一场新的订阅
    对局时先用 on_new_game 交给主程序挂一条常驻牌流（终局名次/点位由那条流的
    on_finish 报回），自己再做低频采样（一轮最多 SAMPLE_MAX 场，不推消息，只把
    最后一次真实结算和 owari 记在状态里）——常驻流排不上时，散场那条靠这份采样。
    """

    POLL = 60.0            # 列表一分钟一轮；天凤的 rooms 列表本身就是分钟级更新
    END_GRACE = 3          # 连续 3 轮查不到才算散场，避免一次网络抖动误报
    GAME_TTL = 6 * 3600.0  # 超过 6 小时还挂着的当僵尸清掉
    LISTED_TTL = 240.0     # 列表快照超过 4 分钟就不给会话当依据
    SAMPLE_MAX = 4         # 每轮最多替几场去碰一次牌流
    SAMPLE_TIMEOUT = 10.0  # 单次订阅最长等待

    def __init__(self, notifier, lobbies=("1",), on_new_game=None):
        self.notifier = notifier        # f(text, names:set, rule_name:str, wg:str)
        # 新发现一场订阅对局时通知（主程序用它给这场开一条常驻牌流）
        self.on_new_game = on_new_game
        self.lobbies = tuple(lobbies)
        self.names: set = set()         # 订阅的天凤玩家名（所有会话的并集）
        self.last_error = ""
        self._games: dict = {}          # 完整 WG 码 → 状态
        self._listed: set = set()       # 最近一轮列表里出现过的 WG 码
        self._listed_ts = 0.0
        self._rr = 0                    # 采样轮转指针
        self._thread = None
        self._stop = threading.Event()

    # ---- 订阅名 ----

    def add(self, nick: str) -> None:
        if nick:
            self.names.add(nick)

    def remove(self, nick: str) -> None:
        self.names.discard(nick)

    def sync(self, names) -> None:
        """用外部给出的全集覆盖（thdel 之后别把上一轮的名字留在表里）。"""
        self.names = {n for n in names if n}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def snapshot(self) -> list:
        """当前认定在打的订阅对局（wgstat 用）。"""
        out = []
        for st in self._games.values():
            out.append({
                "mid": short_id(st["wg"]),
                "wg": st["wg"],
                "rule": game_type_name(st["rule"]),
                "players": " ".join(st["names"]),
                "hits": sorted(st["hits"]),
                "since": time.strftime("%H:%M", time.localtime(st["start_ts"])),
            })
        return out

    # ---- 轮询 ----

    def _loop(self) -> None:
        while not self._stop.wait(self.POLL):
            try:
                self._tick()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
                _LOG.warning(f"[th] 自动播报轮询失败: {e}")

    def _rows(self) -> tuple:
        """(去重后的对局行, 是否至少有一个大厅拉成功)。"""
        rows, seen, ok = [], set(), False
        for lb in self.lobbies:
            try:
                got = fetch_games(lb)
                ok = True
            except Exception as e:
                self.last_error = f"大厅 {lb}: {type(e).__name__}"
                continue
            for g in got:
                if g["wg"] in seen:
                    continue
                seen.add(g["wg"])
                rows.append(g)
        return rows, ok

    def is_listed(self, wg: str) -> bool:
        """这个码最近一轮还在不在段位房列表（会话用它区分局间空档和码失效）。"""
        if time.time() - self._listed_ts > self.LISTED_TTL:
            return False
        return str(wg).upper() in self._listed

    def _call_new(self, game: dict) -> None:
        """把这场交给主程序挂常驻牌流；重复交没坏处（主程序按码去重）。"""
        if not self.on_new_game:
            return
        try:
            self.on_new_game(game)
        except Exception as e:
            _LOG.warning(f"[th] 新对局挂跟踪失败: {e}")

    def _tick(self) -> None:
        if not self.names:
            return
        rows, ok = self._rows()
        now = time.time()
        if ok:
            self._listed = {str(g["wg"]).upper() for g in rows}
            self._listed_ts = now
        alive = set()
        for g in rows:
            hits = [p for p in self.names
                    if any(p in n or n in p for n in g["names"] if n)]
            if not hits:
                continue
            wg = str(g["wg"]).upper()
            alive.add(wg)
            st = self._games.get(wg)
            names, bits = seats(g)
            if st is None:
                st = {"wg": wg, "rule": str(g.get("rule", "") or ""),
                      "names": names, "bits": bits, "hits": set(hits),
                      "start_ts": now, "gone": 0, "sc": "", "owari": ""}
                self._games[wg] = st
                self._call_new(dict(g, names=names))
                self._notify(self._start_text(st), set(hits),
                             game_type_name(st["rule"]), wg)
            else:
                new_hits = set(hits) - st["hits"]
                st["gone"] = 0
                st["hits"] |= set(hits)
                if names:
                    st["names"] = names
                    st["bits"] = bits
                if new_hits:
                    # 中途才订阅上的人：这场之前没挂过流，现在补一条
                    self._call_new(dict(g, names=names))
        if not ok and not alive:
            return          # 这轮一个大厅都没拉到，别把在打的场判成散场
        self._sample()
        for wg, st in list(self._games.items()):
            if wg in alive:
                continue
            st["gone"] += 1
            if st["gone"] < self.END_GRACE:
                continue
            self._games.pop(wg, None)
            if not st.get("owari"):
                self._final_probe(st)
            self._notify(self._end_text(st), st["hits"],
                         game_type_name(st["rule"]), wg)
        for wg, st in list(self._games.items()):
            if now - st["start_ts"] > self.GAME_TTL:
                self._games.pop(wg, None)

    def _notify(self, text: str, names: set, rule_name: str = "", wg: str = "") -> None:
        try:
            self.notifier(text, names, rule_name, wg)
        except Exception as e:
            _LOG.warning(f"[th] 自动播报回调失败: {e}")

    def _sample(self) -> None:
        """给在打的订阅对局碰一次牌流，只记录不播报（散场的名次靠它）。"""
        games = list(self._games.values())
        if not games:
            return
        n = min(self.SAMPLE_MAX, len(games))
        for i in range(n):
            st = games[(self._rr + i) % len(games)]
            if st.get("owari"):
                continue        # 已经拿到全场精算点位的不用再碰
            try:
                res = asyncio.run(watch_once(st["wg"], timeout=self.SAMPLE_TIMEOUT))
            except Exception as e:
                st["sample_err"] = f"{type(e).__name__}"
                continue
            self._record(st, res)
        self._rr += n

    @staticmethod
    def _record(st: dict, res: dict) -> None:
        nn = [n for n in (res.get("names") or []) if n]
        if len(nn) >= 3:
            st["s_names"] = list(res["names"])[:4]
        for ev in res.get("events") or []:
            if ev.get("tag") in ("AGARI", "RYUUKYOKU"):
                st["sc"] = str(ev.get("sc", "") or "")
                if ev.get("owari"):
                    st["owari"] = str(ev["owari"])
                    st["owari_ts"] = time.time()

    def _final_probe(self, st: dict) -> None:
        """散场那一刻再碰两次：收尾窗口只有那么一会儿，错过就只能报旧点数。"""
        for _ in range(2):
            try:
                res = asyncio.run(watch_once(st["wg"], timeout=self.SAMPLE_TIMEOUT))
            except Exception:
                return
            self._record(st, res)
            if st.get("owari"):
                return
            if self._stop.wait(10.0):
                return

    # ---- 文案 ----

    def _start_text(self, st: dict) -> str:
        return room_start_text(short_id(st["wg"]),
                               f"天凤 {game_type_name(st['rule'])}",
                               st["names"], now_hm(), st["hits"],
                               st.get("bits") or ())

    def _end_text(self, st: dict) -> str:
        """散场就发统一的【对局完成】，格式和 wg 会话报的那条完全一样。"""
        mid = short_id(st["wg"])
        names = st.get("s_names") or st["names"]
        rows, _ = end_rows(st.get("owari", ""), names, sc_to_scores(st.get("sc", "")))
        if rows:
            notes = [] if st.get("owari") else [
                "注: 无终局结算，点数为最后一次小局结算值，无点位"]
            return finish_text(mid, rows, st.get("hits") or (), notes)
        # 一个结算都没订到：宁可不报名次，也不拿列表里的名字编一个结果
        return finish_text(mid, [], (),
                           ["注: 无终局结算数据，无名次，可用 wglog <牌谱码> 查询"])


