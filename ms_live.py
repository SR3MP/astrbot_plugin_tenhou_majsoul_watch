# -*- coding: utf-8 -*-
"""雀魂观战：官方观战流（观战票）+ 牌谱回放兜底。

两条数据面（2026-11 CN 服实测，客户端 4.0.51）：
  1) 实时观战流：Lobby.fetchOBToken(uuid) → {token, delay}，观战节点是登录网关
     同主机的 wss://route-N.maj-soul.com/ob。那条连接不走 protobuf RPC，而是文本
     指令装在 binary 帧里：`<= Auth <seq> {"token":…}` → `<= StartOb <seq+1> {}`，
     之后帧为 14 字节头 + 标准 Wrapper（.lq.RecordNewRound 等，与牌谱同构），
     空闲时要发 `<= FetchSequence` 主动拉；终局帧是 .lq.NotifyGameEndResult。
     delay 是服务端强制延迟（实测 300 秒），开局不到 5 分钟的场次 Auth 成功但
     一帧不给。
  2) 牌谱回放：Lobby.fetchGameRecord 在局未结束时返回 1203，结束后过一阵
     （牌谱屋说法：几分钟到几小时）才能读到 GameDetailRecords。
     这条能补上实时流断掉之后的每一局明细。

只用服务端返回的真实字段渲染，拿不到就不编数字。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

from astrbot.api import logger

from .broadcast import (event_head, finish_text, now_hm, rank_rows, score_rows,
                        start_text)
from .majsoul_client import (MSRpcError, UA, decode_action_data,
                             ms_field_label, ms_final_point, ms_short_id)

WIND_CN = {0: "东", 1: "南", 2: "西", 3: "北"}

# 雀魂役名表：FanInfo.id 用的是客户端数据表 fan.json 的编号（协议里没有 Yaku 枚举，
# CN 服下发的 FanInfo.name 实测是空串）。编号取自解包国服客户端的公开表，各镜像一致。
# 番数不用这张表，只用服务端下发的 FanInfo.val；id 不在表里就写 役<id>，不猜役种。
MS_FAN_NAMES = {
    1: "门前清自摸和", 2: "立直", 3: "枪杠", 4: "岭上开花", 5: "海底摸月",
    6: "河底捞鱼", 7: "役牌:白", 8: "役牌:发", 9: "役牌:中",
    10: "役牌:门风牌", 11: "役牌:场风牌", 12: "断幺九", 13: "一杯口", 14: "平和",
    15: "混全带幺九", 16: "一气通贯", 17: "三色同顺", 18: "两立直",
    19: "三色同刻", 20: "三杠子", 21: "对对和", 22: "三暗刻", 23: "小三元",
    24: "混老头", 25: "七对子", 26: "纯全带幺九", 27: "混一色", 28: "二杯口",
    29: "清一色", 30: "一发", 31: "宝牌", 32: "红宝牌", 33: "里宝牌",
    34: "拔北宝牌", 35: "天和", 36: "地和", 37: "大三元", 38: "四暗刻",
    39: "字一色", 40: "绿一色", 41: "清老头", 42: "国士无双", 43: "小四喜",
    44: "四杠子", 45: "九莲宝灯", 46: "八连庄", 47: "纯正九莲宝灯",
    48: "四暗刻单骑", 49: "国士无双十三面", 50: "大四喜",
    51: "燕返", 52: "杠振", 53: "十二落抬", 54: "五门齐", 55: "三连刻",
    56: "一色三同顺", 57: "一筒摸月", 58: "九筒捞鱼", 59: "人和",
    60: "大车轮", 61: "大竹林", 62: "大数邻", 63: "石上三年", 64: "大七星",
    65: "人和",
}


# =====================================================================
#  牌名
# =====================================================================
def ms_tile_name(t) -> str:
    """雀魂本来就按 mspz 记法发牌面（'5z'/'0p'/'9s'），原样用，字牌不换成中文。"""
    return str(t).strip()


def ms_tiles_compact(seq) -> str:
    """['1p','1p','2p','5z'] → '112p 5z' —— 与天凤播报同一套记法。"""
    groups: dict[str, list[str]] = {}
    for raw in seq or []:
        t = ms_tile_name(raw)
        if len(t) != 2 or t[1] not in "mpsz":
            continue
        groups.setdefault(t[1], []).append(t[0])
    return " ".join("".join(sorted(groups[s])) + s
                    for s in ("m", "p", "s", "z") if groups.get(s))


def ms_ming_text(ming) -> str:
    """HuleInfo.ming 由服务端直接给字符串，实测形如 'kezi(2z,2z,2z)'，播报只留牌面。

    拔北不算副露，别混进牌面里（那行末另有 拔北×n），这里跳过。
    """
    out = []
    for raw in ming or []:
        s = str(raw).strip()
        if not s:
            continue
        m = re.match(r"^([A-Za-z_]+)\((.*)\)$", s)
        if not m:
            out.append(s)
            continue
        if m.group(1) in ("bankin", "babei"):
            continue
        tiles = [t for t in re.split(r"[,;、\s]+", m.group(2)) if t]
        part = ms_tiles_compact(tiles)
        if part:
            out.append(part)
    return " ".join(out)


def round_label(chang: int, ju: int, ben: int) -> str:
    """局名，写成天凤播报那一行的样子：'东3局 2本場'。"""
    return f"{WIND_CN.get(chang, '?')}{ju + 1}局 {ben}本場"


# =====================================================================
#  牌谱 → 逐局结果
# =====================================================================
def _hule_dict(h) -> dict:
    # 取证探针：这些字段哪些真的下发、谁和四家点数变化一致，靠它定；定完就删
    logger.info("[ms-hule] seat=%d zimo=%s yiman=%s count=%d fu=%d sum=%d paid=%d "
              "rong=%d qin=%d xian=%d title=%s fans=%s"
              % (int(h.seat), bool(h.zimo), bool(h.yiman), int(h.count), int(h.fu),
                 int(h.point_sum), int(h.dadian), int(h.point_rong),
                 int(h.point_zimo_qin), int(h.point_zimo_xian),
                 list(h.title), [(int(f.id), int(f.val), f.name) for f in h.fans]))
    return {
        "seat": int(h.seat),
        "zimo": bool(h.zimo),
        "liqi": bool(h.liqi),
        "fu": int(h.fu),
        # 服务端的 hand 是 13 张，和了牌单独在 hu_tile，播报时并进去凑成 14 张
        "hand": [ms_tile_name(x) for x in h.hand],
        "hu": ms_tile_name(h.hu_tile),
        "ming": ms_ming_text(h.ming),
        "dora": [ms_tile_name(x) for x in h.doras],
        "li_dora": [ms_tile_name(x) for x in h.li_doras],
        # CN 服的 fan.name 实测是空串，非空才直接用，空则留给 _yaku_lines 查役名表
        "fans": [(int(f.id), int(f.val), (f.name or "").strip()) for f in h.fans],
        "yiman": bool(h.yiman),
    }


def new_round(chang, ju, ben, liqibang, doras, scores) -> dict:
    return {
        "label": round_label(chang, ju, ben),
        "before": [int(v) for v in scores],
        "after": [int(v) for v in scores],
        "hules": [],
        "babei": {},
        "tingpai": [],
        "liujumanguan": False,
        "abortive": False,
        "last_discard": None,
    }


def feed_round_event(cur: dict, tn: str, m) -> None:
    """把一局内的 Record* 事件累计进 cur（流局/点炮家/拔北都靠这些）。"""
    if cur is None:
        return
    if tn == "RecordDiscardTile":
        cur["last_discard"] = (int(m.seat), ms_tile_name(m.tile))
    elif tn == "RecordBaBei":
        s = int(m.seat)
        cur["babei"][s] = cur["babei"].get(s, 0) + 1
    elif tn == "RecordNoTile":
        cur["after"] = no_tile_after(m, cur["after"])
        cur["tingpai"] = [bool(p.tingpai) for p in m.players]
        cur["liujumanguan"] = bool(m.liujumanguan)
    elif tn == "RecordLiuJu":
        # 中途流局无得失也无听牌；type 是原因码，proto 里没有枚举，不猜
        cur["abortive"] = True


def no_tile_after(m, base) -> list:
    """流局后的点数：RecordNoTile.scores 只有一条，old/delta 才是四家数组。

    实测 score 那位是空的（读出来全是 0），别拿它当终分。
    """
    info = list(m.scores or [])
    if len(info) != 1:
        return list(base or [])
    old = [int(v) for v in info[0].old_scores]
    dlt = [int(v) for v in info[0].delta_scores]
    if not old or len(old) != len(dlt):
        return list(base or [])
    return [o + d for o, d in zip(old, dlt)]


def parse_hands(detail) -> list[dict]:
    """GameDetailRecords.actions → 每一局的结算信息（全部取服务端字段）。"""
    hands: list[dict] = []
    cur: Optional[dict] = None
    for ga in detail.actions:
        tn, m = decode_action_data(ga.result)
        if tn == "RecordNewRound":
            if cur is not None:
                if (not cur["hules"] and not cur["tingpai"]
                        and not cur.get("abortive")):
                    # 流局：牌谱没给 RecordNoTile 时，罚分只体现在下一局的开局点数里
                    cur["after"] = [int(v) for v in m.scores] or cur["after"]
                hands.append(cur)
            cur = new_round(m.chang, m.ju, m.ben, m.liqibang, m.doras, m.scores)
            continue
        if cur is None:
            continue
        if tn == "RecordHule":
            cur["before"] = [int(v) for v in m.old_scores] or cur["before"]
            cur["after"] = [int(v) for v in m.scores]
            cur["hules"] = [_hule_dict(h) for h in m.hules]
            hands.append(cur)
            cur = None
            continue
        feed_round_event(cur, tn, m)
    if cur is not None:
        hands.append(cur)
    return hands


def _provider(hand: dict, hu: dict) -> Optional[int]:
    """点炮家：荣和时取最后一张与和牌同名的打牌；对不上就不猜。"""
    ld = hand.get("last_discard")
    if ld and ld[1] == hu["hu"]:
        return ld[0]
    return None


def _seat_name(names: list[str], seat: int) -> str:
    return names[seat] if 0 <= seat < len(names) else f"seat{seat}"


def hand_lines(hand: dict, names: list[str]) -> list[str]:
    """一局 → 与天凤【小局结算】/【流局】同款的几行，末尾都是逐家点数。"""
    if not hand["hules"]:
        lines = liuju_lines(hand, names)
    else:
        lines = []
        for hu in hand["hules"]:
            lines += _hule_lines(hu, hand, names)
    return lines + score_rows(names, hand["before"], hand["after"])


def _yaku_lines(fans) -> str:
    """役行照天凤的写法 ' 1 立直'：番数只用服务端 val，役名服务端没下发就查 MS_FAN_NAMES。

    id 不在表里写 役<id>，只说有几个役、几番，不猜役种。
    """
    out = []
    for fid, val, name in (fans or []):
        if val <= 0:
            continue
        out.append(f" {val} {name or MS_FAN_NAMES.get(fid) or f'役{fid}'}")
    return "\n".join(out)


def _paid_text(hu: dict, hand: dict) -> str:
    """和了行的点数 = 和了家这局的实际点数变化（before/after 都是服务端给的数组）。

    HuleInfo.point_sum 实测比四家的点数变化大一截，拿它当点数会播出对不上账的数，
    所以只用点数变化；变化读不到就整段不写。
    """
    seat = hu["seat"]
    try:
        delta = int(hand["after"][seat]) - int(hand["before"][seat])
    except (IndexError, KeyError, TypeError, ValueError):
        return ""
    return f"{delta}点"


def _hanfu_text(hu: dict) -> str:
    """'X番Y符'：番数取服务端每个役的番数相加（与天凤同一算法），役满不写番。"""
    han = 0 if hu["yiman"] else sum(v for _i, v, _n in (hu["fans"] or []) if v > 0)
    fu = f"{hu['fu']}符" if hu["fu"] else ""
    if han and fu:
        return f"{han}番{fu}"
    return f"{han}番" if han else fu


def _hule_lines(hu: dict, hand: dict, names: list[str]) -> list[str]:
    """一个和了 → 标题行 + 手牌/副露/役（一炮多响就并列几段）。"""
    who = _seat_name(names, hu["seat"])
    if hu["zimo"]:
        how = "自摸 "
    else:
        frm = _provider(hand, hu)
        how = "荣和 %s " % (_seat_name(names, frm) if frm is not None else "?")
    head = f"{hand['label']} {who} {how}" + " ".join(
        p for p in (_paid_text(hu, hand), _hanfu_text(hu)) if p)
    if hu["yiman"]:
        head += " 役满"
    lines = [head]
    tiles = ms_tiles_compact(list(hu["hand"]) + ([hu["hu"]] if hu["hu"] else []))
    if tiles:
        lines.append(f"手牌: {tiles}")
    ming = hu["ming"]
    bb = (hand.get("babei") or {}).get(hu["seat"])
    if bb:
        ming = (ming + f" 拔北×{bb}").strip()
    if ming:
        lines.append(f"副露: {ming}")
    yaku = _yaku_lines(hu["fans"])
    if yaku:
        lines.append(yaku)
    return lines


def liuju_lines(hand: dict, names: list[str]) -> list[str]:
    """流局 → 天凤那两行：'东1局 0本場 流局（荒牌）' + 听牌名单。"""
    if hand.get("abortive"):
        return [f"{hand['label']} 流局（中途流局）"]
    kind = "流局满贯" if hand.get("liujumanguan") else "荒牌"
    lines = [f"{hand['label']} 流局（{kind}）"]
    tp = hand.get("tingpai") or []
    if not tp:
        lines.append("听牌: 未知")
        return lines
    tenpai = [_seat_name(names, i) for i, ok in enumerate(tp) if ok]
    lines.append("听牌: " + (" ".join(tenpai) if tenpai else "无"))
    return lines


def replay_text(mid: str, field: str, names: list[str], hands: list[dict],
                max_lines: int = 45) -> list[str]:
    """整场回放 → 一到两条消息；逐局那几行与实时观战、天凤牌谱一模一样。"""
    body: list[str] = []
    for h in hands:
        body.append(event_head(mid, "小局结算" if h["hules"] else "流局", ""))
        body += hand_lines(h, names)
        body.append("")
    head = start_text(mid, ms_field_label(field), names, "", (),
                      [f"共 {len(hands)} 局", "注: 牌谱回放"])
    out: list[str] = []
    chunk = [head, ""]
    lines = 0
    for ln in body:
        if lines >= max_lines:
            out.append("\n".join(chunk))
            chunk = [f"#{mid}【牌谱回放·续】"]
            lines = 0
        chunk.append(ln)
        lines += 1
    out.append("\n".join(chunk))
    return out


def final_rows(result, names: list[str]) -> list:
    """牌谱 head.result（[(座位, 终局绝对点数, 点位)]）→ 【对局完成】那几行。"""
    items = [(_seat_name(names, int(seat)), final, pt)
             for seat, final, pt in (result or [])]
    if items and not any(int(f) for _nm, f, _pt in items):
        return []          # 服务端这几个字段整场没填：没有点数可讲
    return rank_rows(items)


# =====================================================================
#  实时观战流（拿到观战票才走得通）
# =====================================================================
async def open_observe(endpoint: str, token: str):
    """连观战节点，返回 (ObserveStream, 对局头, err)。

    实测（2026-11 CN）这条连接用的是装在二进制帧里的文本协议：
      发 b'<= Auth <序号> {"token":"…"}' → 收 b'=> <序号> {create_time,start_time,delay,head}'
      发 b'<= StartOb <序号> {}'          → 收 b'=> <序号> {}'，之后才推 .lq.Record* 帧
    旧的 FastTest.authObserve/startObserve 通道服务端已经不在这条连接上应答。
    """
    import websockets
    try:
        # 观战节点不回 ping 帧：客户端 keepalive 一开就是自己把自己踢掉
        # （实测 40 秒左右 ConnectionClosedError: keepalive ping timeout，
        # 而段位场延迟 300 秒，等于每一局都在第一个结算之前就断）。保活交给
        # ObserveStream.pull 的 FetchSequence，和真实客户端一致。
        ws = await websockets.connect(
            endpoint, origin="https://game.maj-soul.com",
            additional_headers={"User-Agent": UA},
            ping_interval=None, open_timeout=10)
    except Exception as e:
        return None, None, f"{endpoint} 连接失败 {type(e).__name__}: {e}"
    try:
        await ws.send(f'<= Auth 1 {{"token":"{token}"}}'.encode())
        head = auth_payload(await ws_recv(ws, 15))
        await ws.send(b"<= StartOb 2 {}")
        await ws_recv(ws, 15)
    except Exception as e:
        await quiet_close(ws)
        return None, None, f"{endpoint} 握手失败 {type(e).__name__}: {e}"
    if not head:
        await quiet_close(ws)
        return None, None, f"{endpoint} 握手回了个看不懂的东西"
    return ObserveStream(ws), head, ""


async def ws_recv(ws, timeout: float) -> bytes:
    m = await asyncio.wait_for(ws.recv(), timeout)
    return m if isinstance(m, (bytes, bytearray)) else str(m).encode()


async def quiet_close(ws) -> None:
    try:
        await ws.close()
    except Exception:
        pass


def auth_payload(raw: bytes) -> dict:
    """b'=> 1 {…,"head":"{\\"uuid\\"…}"}' → 对局头 dict；看不懂就返回空。

    delay/create_time/start_time 在外层、head 是 GameLiveHead 的 JSON 字符串，
    两层合成一个 dict 交给上层（延迟只在外层）。
    """
    s = (raw or b"").decode(errors="replace").strip()
    if not s.startswith("=>") or " " not in s:
        return {}
    try:
        body = json.loads(s.split(" ", 2)[2])
    except Exception:
        return {}
    head = body.get("head")
    if isinstance(head, str):
        try:
            merged = json.loads(head)
        except Exception:
            return {}
    elif isinstance(head, dict):
        merged = dict(head)
    else:
        merged = {}
    for k in ("delay", "create_time", "start_time"):
        if k in body and not merged.get(k):
            merged[k] = body[k]
    return merged
class ObserveStream:
    """观战节点连接：文本协议与二进制帧混在一条连接上，二进制帧头固定 14 字节。"""

    HDR = 14    # seq(2) + 相对毫秒(4) + 2 + 类型(2) + 长度(4)

    def __init__(self, ws):
        self.ws = ws
        self._seq = 2
        self._pull = 0

    async def recv(self, timeout: float):
        """返回 ('msg', (类型名, 消息)) / ('text', 原文) / ('skip', 名字)。"""
        from .ms import protocol_pb2 as pb
        raw = await ws_recv(self.ws, timeout)
        if raw[:2] in (b"=>", b"<="):
            return "text", raw.decode(errors="replace")
        w = pb.Wrapper()
        w.ParseFromString(raw[self.HDR:])
        name = (w.name or "").split(".")[-1]
        cls = getattr(pb, name, None)
        if cls is None:
            return "skip", name
        m = cls()
        m.ParseFromString(w.data)
        return "msg", (name, m)

    async def pull(self) -> None:
        """真实客户端在静默时按 id 递增催一次，照做，不自己猜节奏。"""
        self._seq += 1
        self._pull += 1
        await self.ws.send(f'<= FetchSequence {self._seq} {{"id":{self._pull}}}'.encode())

    async def close(self) -> None:
        await quiet_close(self.ws)


# =====================================================================
#  单场跟踪
# =====================================================================
class MsGameSession:
    """跟踪一场雀魂对局：能开票就实时播，开不了就等牌谱出来后回放。"""

    POLL = 60.0             # 牌谱轮询间隔（服务端 1 req/s 限流，这里远低于）
    MAX_LIFE = 4 * 3600.0   # 4 小时仍读不到牌谱就放弃
    IDLE_QUIT = 600.0       # 实时流静默 10 分钟就收工
    RECONNECT = 3           # 断流后最多重连几次（票可复用）
    END_WAIT = 90 * 60.0    # 断流后最多等这么久，靠牌谱补终局名次

    def __init__(self, uuid: str, push, client, game: Optional[dict] = None,
                 watchers=None):
        self.uuid = uuid
        self.mid = ms_short_id(uuid)
        self.push = push
        self.client = client
        game = game or {}
        self.field = game.get("field") or "未知场"
        self.names = [p.get("nickname", "") for p in (game.get("players") or [])]
        # 群里订阅的昵称：终局那句点评按他们来讲
        self.watchers = {w for w in (watchers or ()) if w}
        # 列表行里标了 live=False 的（比赛场牌谱索引）就是已终局的那场，
        # 再试观战票只会拿到 1203，直接等牌谱回放即可。
        self.known_over = game.get("live") is False
        self.mode = "record"
        self.hands = 0
        # 最近一局结算后的四家点数，wginfo 用；服务端没给过就留空
        self.last_scores: list[int] = []
        self.finished = False
        self.note = ""
        self._task = None
        self._stream = None
        self._cur: Optional[dict] = None
        # 已经播过的小局（场/局/本局）：断流重连后服务端会把整场再推一遍
        self._seen_rounds: set = set()
        # 实时流探针：催帧次数、每种帧收到几次（见 _run_live 的统计行）
        self._pulls = 0
        self._frames: dict[str, int] = {}
        # 服务端在 Auth 应答里给的观战延迟（实测段位场 300 秒）
        self.delay = 0

    # ---- 生命周期 ---------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._stream is not None:
            try:
                asyncio.ensure_future(self._stream.close())
            except Exception:
                pass
            self._stream = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self.finished

    def label(self) -> str:
        who = "/".join(n for n in self.names if n) or self.uuid[:8]
        return f"#{self.mid} {ms_field_label(self.field)} {who}"

    def brief(self) -> str:
        how = "实时观战" if self.mode == "live" else "牌谱回放"
        base = f"{self.label()} [{how}] 已结算{self.hands}局"
        if not self.last_scores:
            return f"{base} | 比分未取到"
        sc = " ".join(f"{_seat_name(self.names, i) or f'seat{i}'} {v}"
                      for i, v in enumerate(self.last_scores))
        return f"{base} | {sc}"

    # ---- 主循环 -----------------------------------------------------
    async def _run(self) -> None:
        """【对局开始】由 wg 那条回执发（和天凤一致），这里只管局内消息。"""
        if self.known_over:
            # 已终局：没有"对局开始"可播，等牌谱读到了直接回放
            await self._run_record()
            return
        if await self._try_live():
            if await self._run_live():
                return
            self.mode = "record"
            await self._emit(
                f"#{self.mid} {self.note}，改用牌谱回放；"
                "牌谱可读后推送逐局结算与终局名次。"
            )
        await self._run_record()

    async def _try_live(self) -> bool:
        """开票成功且连上观战节点才走实时流，否则退回牌谱回放。"""
        try:
            auth = await asyncio.to_thread(self.client.get_observe_auth, self.uuid)
        except MSRpcError as e:
            self.note = f"观战获取失败（code={e.code}）"
        except Exception as e:
            self.note = f"观战获取失败（{type(e).__name__}）"
        else:
            token = auth.get("token") or ""
            stream, head, err = await open_observe(auth.get("location", ""), token)
            if stream is not None:
                self._stream = stream
                self.delay = int(head.get("delay") or 0) if head else 0
                self.apply_head(head)
                self.mode = "live"
                return True
            self.note = f"观战节点接入失败（{err}）"
        logger.info(f"[ms-live] #{self.mid} {self.note}")
        await self._emit(
            f"#{self.mid} {self.note}，改用牌谱回放；"
            "牌谱可读后推送逐局结算与终局名次。"
        )
        return False

    def apply_head(self, head: dict) -> None:
        """观战流的对局头比直播列表更全，缺名字就补，已有的一律不覆盖。"""
        players = (head or {}).get("players") or []
        if players and not any(self.names):
            self.names = [p.get("nickname", "") for p in players]

    # ---- 实时流 -----------------------------------------------------
    async def _run_live(self) -> bool:
        """跑实时流。返回 False = 这场流里一帧没给，让上层退回牌谱回放。"""
        deadline = time.time() + self.MAX_LIFE
        started = time.time()
        last = time.time()
        # 服务端有 delay：刚开的对局先静默那几分钟是正常的，多给 IDLE_QUIT 余量
        idle_limit = self.IDLE_QUIT + self.delay
        retries = 0
        while time.time() < deadline and not self.finished:
            try:
                kind, val = await self._stream.recv(30)
            except asyncio.TimeoutError:
                await self._stream.pull()
                self._pulls += 1
                if time.time() - last > (self.IDLE_QUIT if self.hands
                                         else idle_limit):
                    break
                continue
            except Exception as e:
                logger.info(f"[ms-live] #{self.mid} 流中断 {type(e).__name__}: {e}")
                if retries < self.RECONNECT and await self._reconnect():
                    retries += 1
                    last = time.time()
                    continue
                break
            if kind != "msg":
                continue
            last = time.time()
            if val[0] not in self._frames:
                # 实时流到底给不给帧只靠这条判定；首帧一行 + 收工一行统计
                logger.info(f"[ms-live] #{self.mid} 观战流首帧 {val[0]}"
                          f"（连上后 {int(time.time() - started)} 秒、催帧 {self._pulls} 次）")
            self._frames[val[0]] = self._frames.get(val[0], 0) + 1
            await self._on_live_msg(*val)
        logger.info(f"[ms-live] #{self.mid} 流帧统计 {self._frames or '一帧没给'}"
                  f"，催帧 {self._pulls} 次，重连 {retries} 次")
        if self.finished:
            return True
        await self._close_stream()
        if self.hands or self._cur:
            # 已经播过结算却没等到终局帧：等牌谱把最后那条名次补上
            await self._end_from_record(self.note or "观战流中断")
            return True
        self.note = "观战流无数据"
        return False

    async def _reconnect(self) -> bool:
        """同一张票再连一次（实测票可复用）；服务端会把整场重推，靠 _seen_rounds 去重。"""
        try:
            auth = await asyncio.to_thread(self.client.get_observe_auth, self.uuid)
            stream, head, err = await open_observe(auth.get("location", ""),
                                                   auth.get("token") or "")
        except Exception as e:
            self.note = f"重连失败（{type(e).__name__}）"
            return False
        if stream is None:
            self.note = f"重连失败（{err}）"
            return False
        await self._close_stream()
        self._stream = stream
        self.apply_head(head)
        await self._emit(f"#{self.mid} 观战流中断，已重连。")
        return True

    async def _close_stream(self) -> None:
        if self._stream is not None:
            await self._stream.close()
            self._stream = None

    async def _on_live_msg(self, tn: str, m) -> None:
        """观战流帧：Record* 与牌谱同构，终局另有 NotifyGameEndResult。"""
        if tn == "NotifyGameEndResult":
            await self._finish_live(result=list(m.result.players))
            return
        if tn == "RecordNewRound":
            key = (int(m.chang), int(m.ju), int(m.ben))
            if key in self._seen_rounds:
                # 重连后服务端会把整场再推一遍：这局播过了就别再建轮次
                self._cur = None
                return
            self._seen_rounds.add(key)
            if self._cur is not None and not self._cur["hules"]:
                await self._emit_liuju(self._cur)
            self._cur = new_round(m.chang, m.ju, m.ben, m.liqibang, m.doras,
                                  m.scores)
            return
        if self._cur is None:
            return
        feed_round_event(self._cur, tn, m)
        if tn == "RecordNoTile":
            await self._emit_liuju(self._cur)
            self._cur = None
            return
        if tn == "RecordLiuJu":
            await self._emit_liuju(self._cur)
            self._cur = None
            return
        if tn != "RecordHule":
            return
        self._cur["after"] = [int(v) for v in m.scores]
        self._cur["before"] = [int(v) for v in m.old_scores] or self._cur["before"]
        self._cur["hules"] = [_hule_dict(h) for h in m.hules]
        self.hands += 1
        self.last_scores = list(self._cur["after"])
        await self._emit(event_head(self.mid, "小局结算", now_hm()) + "\n"
                         + "\n".join(hand_lines(self._cur, self.names)))
        self._cur = None

    async def _emit_liuju(self, hand: dict) -> None:
        self.hands += 1
        self.last_scores = list(hand["after"])
        await self._emit(event_head(self.mid, "流局", now_hm()) + "\n"
                         + "\n".join(hand_lines(hand, self.names)))

    async def _finish_live(self, result=None) -> bool:
        """实时流的终局帧到了就当场播终局；返回 False 表示名次还没着落。"""
        if self.finished:
            return True
        # NotifyGameEndResult 的 PlayerItem：绝对终局点数在 part_point_1/2，
        # total_point 还叠了顺位奖励不能直接用；grading_score 是段位分变化。
        items = [(_seat_name(self.names, int(p.seat)),
                   ms_final_point(p), float(p.grading_score))
                 for p in (result or [])]
        if not items or not any(int(f) for _nm, f, _pt in items):
            return False     # 服务端这几个字段整场没填：没有点数可讲
        rows = rank_rows(items)
        if not rows:
            return False
        self.finished = True
        await self._emit(finish_text(self.mid, rows, self.watchers,
                                     [f"注: {self.note}"] if self.note else []))
        await self._close_stream()
        return True

    async def _end_from_record(self, why: str) -> None:
        """终局帧没等到：等这场牌谱可读，只用它的 result 补那一条【对局完成】。"""
        deadline = time.time() + self.END_WAIT
        first = True
        while time.time() < deadline and not self.finished:
            if not first:
                await asyncio.sleep(self.POLL)
            first = False
            try:
                rec = await asyncio.to_thread(self.client.get_game_detail, self.uuid)
            except Exception:
                continue      # 1203：还没终局，或牌谱还没生成
            rows = final_rows(rec.get("result"), self.names)
            if not rows:
                if rec.get("result"):
                    # 牌谱可读、result 也在，只是服务端没填终局点数
                    self.finished = True
                    await self._emit(finish_text(
                        self.mid, [], self.watchers,
                        [f"注: {why}，已播 {self.hands} 局结算；"
                         "牌谱无终局点数，无名次"]))
                    return
                continue
            self.finished = True
            await self._emit(finish_text(
                self.mid, rows, self.watchers,
                [f"注: {why}，已播 {self.hands} 局结算；"
                 "名次取自终局牌谱"]))
            return
        self.finished = True
        await self._emit(finish_text(
            self.mid, [], self.watchers,
            [f"注: {why}，已播 {self.hands} 局结算；"
             f"等待牌谱 {int(self.END_WAIT // 60)} 分钟超时，无终局名次"]))

    # ---- 兜底：等牌谱 ------------------------------------------------
    async def _run_record(self) -> None:
        deadline = time.time() + self.MAX_LIFE
        last_err = ""
        first = True
        while time.time() < deadline and not self.finished:
            if not first:
                await asyncio.sleep(self.POLL)
            first = False
            try:
                rec = await asyncio.to_thread(self.client.get_game_detail, self.uuid)
            except MSRpcError as e:
                last_err = f"code={e.code}"
                continue
            except Exception as e:
                last_err = f"{type(e).__name__}"
                continue
            detail = rec.get("detail")
            hands = parse_hands(detail) if detail is not None else []
            if not hands:
                last_err = "解析出 0 局"
                continue
            await self._finish_record(rec, hands)
            return
        if not self.finished:
            self.finished = True
            await self._emit(
                f"#{self.mid} 跟踪超时（{int(self.MAX_LIFE // 3600)} 小时），"
                "牌谱不可读"
                + (f"（{last_err}）" if last_err else "") + "，已停止跟踪。"
            )

    async def _finish_record(self, rec: dict, hands: list[dict]) -> None:
        self.finished = True
        self.hands = len(hands)
        self.last_scores = list(hands[-1]["after"])
        names = list(rec.get("names") or self.names or [])
        while names and not names[-1]:
            names.pop()
        names = [n or "?" for n in names] or self.names
        self.names = names
        # 比赛场那行的 field 是比赛名，牌谱 head 推不出比赛名，别用它覆盖
        if self.field in ("", "未知场"):
            self.field = rec.get("field") or self.field
        field = self.field
        for text in replay_text(self.mid, field, names, hands):
            await self._emit(text)
        rows = final_rows(rec.get("result"), names)
        notes = []
        if not rows:
            notes.append("注: 牌谱无终局点数，无名次")
        if self.note:
            notes.append(f"注: {self.note}")
        await self._emit(finish_text(self.mid, rows, self.watchers, notes))

    async def _emit(self, text: str) -> None:
        try:
            await self.push(text)
        except Exception as e:
            logger.warning(f"[ms-live] 推送失败: {e}")
