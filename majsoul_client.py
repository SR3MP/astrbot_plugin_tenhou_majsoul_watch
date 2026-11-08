"""
雀魂 WebSocket 客户端（CN 服 maj-soul.com）
核心实现：连接网关 + 登录 + 玩家详情/对局记录 RPC。
修复说明：请求连接握手必须携带 platform="Web"(proto字段6)，否则网关返回错误码151。
本模块供 astrbot_plugin_tenhou_watch 使用；HTTP 端点已被官方废弃（405），一律走 WS。

实测（2026-11 CN 服）：实时观战走 Lobby.fetchOBToken(uuid) 拿票，再连登录网关
同主机的 wss://route-N/ob（协议见 ms_live.open_observe）；createGameObserveAuth
一律 1216、fetchGameLiveInfo / fetchGameLiveLeftSegment 返回 error code 2，网页
客户端根本不调它们。fetchGameLiveList、fetchAccountState、fetchAccountInfo、
fetchGameRecord、fetchGameRecordList 正常可用。
注意 fetchGameRecordList 的请求只有 start/count/type，没有账号参数，返回的是
**当前登录账号自己**的记录，不能用来查别人。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
import urllib.parse
import urllib.request
from typing import Optional

import websockets

from .broadcast import finish_text, now_hm, rank_rows, room_start_text
from .ms.base import MSRPCChannel
from .ms.rpc import Lobby, Route
from .ms import protocol_pb2 as pb

log = logging.getLogger("tenhou_watch.majsoul")

GAME_URL = "https://game.maj-soul.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 雀魂牌谱屋（amae-koromo）公开 API，用于按昵称反查 account_id，无需登录。
# 前端在 1/2/4/5-data 之间轮换，实测只有部分分片可解析结果，故按序尝试。
KOROMO_SHARDS = ("5", "1", "2", "4")
KOROMO_SEARCH_TPL = "https://{sh}-data.amae-koromo.com/api/v2/{pl}/search_player/"

# fetchGameLiveList 的 filter_id 实测分布（2026-11 CN 服，取满 100 条样本统计人数与玩家段位）：
#   208/209 四人 雀杰~雀豪   211/212 四人 雀豪~雀圣   216 四人 雀圣以上
#   221/222/223/224 三人 初心~雀圣   226 三人 雀杰~雀圣
# 其余 101/102 与 201-227 中的多数值当前恒为空。保留全量遍历以覆盖冷门场，
# 但结果会按 filter_id 记录场信息，不再依赖 GameConfig.category。
LIVE_MODE_IDS = [101, 102] + list(range(201, 229))

# filter_id → 段位场。live list 是按场请求的，请求参数就是场身份，比拿各家段位
# 猜可靠（段位房允许相邻段位同桌：玉之间坐满四个雀豪，按段位推断会念成金之间，
# 实测踩过）。依据：
#   212 —— 网页客户端"玉之间 四人南"页面自己发的 fetchGameLiveList 就是 212，
#          且该场每行 GameConfig.meta.mode_id=12（= filter_id-200），216 同理为 16
#   208/209/211/216 —— 各取 100 行样本统计段位：208/209 雀杰~雀豪，211/212 雀豪~雀圣，
#          216 雀圣以上，对应金/玉/王座
# 三人场和银之间还没锚定，落回按段位推断。
MS_LIVE_FID_FIELD = {208: "金", 209: "金", 211: "玉", 212: "玉", 216: "王座"}


def _http_json(url: str, timeout: float = 8.0):
    """简单的 GET + JSON 解析，带超时，供牌谱屋等公开接口使用。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body)


def _koromo_search_player(pl: str, nickname: str) -> list:
    """牌谱屋按昵称搜索（pl=pl4/pl3），逐个分片试，命中即返回。

    分片域名会轮换，单个分片挂了属常态，全部失败才返回 []。
    """
    quoted = urllib.parse.quote(nickname)
    for shard in KOROMO_SHARDS:
        try:
            return _http_json(KOROMO_SEARCH_TPL.format(sh=shard, pl=pl) + quoted) or []
        except Exception as e:
            log.debug(f"[koromo] 分片 {shard} 查询失败: {e}")
    return []

# 段位编码规则（新）：level.id = mode*10000 + tier*100 + star
#   mode: 1=四人, 2=三人   tier: 1=初心 2=雀士 3=雀杰 4=雀豪 5=雀圣 6=魂天 7=魂天N
# 实测：10503=雀圣三(四人) 20301=雀杰一(三人) 10719=魂天19(四人) 20720=魂天20(三人)
_LEVEL_TIER = {1: "初心", 2: "雀士", 3: "雀杰", 4: "雀豪", 5: "雀圣", 6: "魂天"}
# (tier, star) → 升段所需PT（升级线）。四人/三人都用这张表：实测 70 个活选手里
# 14 个 level.id（含 2xxxx 三人）的分数最高值都没有越过表内线，未见反例。
_LEVEL_UP = {
    (1, 1): 20, (1, 2): 80, (1, 3): 200,
    (2, 1): 600, (2, 2): 800, (2, 3): 1000,
    (3, 1): 1200, (3, 2): 1400, (3, 3): 2000,
    (4, 1): 2800, (4, 2): 3200, (4, 3): 3600,
    (5, 1): 4000, (5, 2): 6000, (5, 3): 9000,
}
_CN_NUM = {1: "一", 2: "二", 3: "三"}


def ms_rank_name(level_id: int) -> str:
    """level.id → 中文段位名，如 10503 → 雀圣三、10719 → 魂天19。"""
    if not level_id:
        return "未知"
    tier = (level_id // 100) % 100
    star = level_id % 100
    if tier == 7:
        # 星数用阿拉伯数字：实测到 19、20，_CN_NUM 只有一二三
        return f"魂天{star}"
    base = _LEVEL_TIER.get(tier)
    if not base:
        return f"未知({level_id})"
    if tier == 6:  # 魂天无星
        return "魂天"
    star_cn = _CN_NUM.get(star, str(star))
    return f"{base}{star_cn}"


def ms_level_up(level_id: int) -> int:
    """升段所需PT；0 表示魂天/魂天N 没有升段线。"""
    if not level_id:
        return 0
    tier = (level_id // 100) % 100
    star = level_id % 100
    if tier == 6:
        return 0
    return _LEVEL_UP.get((tier, star), 0)

# GameConfig.mode.mode 实测值：1=四東 2=四南 11=三東 12=三南。
# 注意 GameConfig.category 在 live head 里恒为 2，不能用来判场，
# 旧的 MS_CATEGORY(category) 因此把几乎所有局都念成"四南銀"。
_MS_MODE_LABEL = {1: "四東", 2: "四南", 11: "三東", 12: "三南"}

# GameConfig.meta.mode_id → 段位场。只有这两个是实测锚定的（网页客户端进
# "玉之间 四人南"页面请求的 filter_id=212，那一版返回的每行 mode_id=12；
# 216 的每行 mode_id=16 且样本段位全在雀圣以上），其余值还没锚，落回段位推断。
_MS_MODE_ID_FIELD = {12: "玉", 16: "王座"}


def ms_field_by_tiers(tiers: list[int]) -> str:
    """末位方案：由房内最高段位推断场名。

    实测抽样（CN 服 live list，各 filter_id 100 局）：
      最高≤雀士→银场；雀杰~雀豪→金场；雀圣→玉场；魂天及以上→王座场。
    段位房允许相邻段位同桌，所以这一条会错判：玉之间坐满四个雀豪时最高段位
    只到雀豪，会被念成金之间——能用请求参数或 mode_id 时别用它。
    """
    hi = max([t for t in tiers if t], default=0)
    if hi == 0:
        return ""
    if hi <= 2:
        return "银"
    if hi <= 4:
        return "金"
    if hi == 5:
        return "玉"
    return "王座"


def ms_field_name(tiers: list[int], mode_id: int = 0, filter_id: int = 0) -> str:
    """段位场场名：服务端给的场身份（请求参数 / mode_id）优先，段位推断兜底。"""
    return (MS_LIVE_FID_FIELD.get(filter_id)
            or _MS_MODE_ID_FIELD.get(mode_id)
            or ms_field_by_tiers(tiers))


def ms_room_name(game_config, tiers: list[int], filter_id: int = 0) -> str:
    """GameConfig + 玩家段位 + 请求的 filter_id → '四南金' 风格的场名。"""
    mode, mode_id = "", 0
    if game_config is not None:
        mode = _MS_MODE_LABEL.get(game_config.mode.mode, "")
        mode_id = game_config.meta.mode_id
    field = ms_field_name(tiers, mode_id, filter_id)
    label = f"{mode}{field}"
    return label or "未知场"


def ms_field_label(field: str) -> str:
    """播报里那一栏的场名：比赛场的名字自带"雀魂"，别再叠一个前缀。"""
    f = (field or "").strip() or "未知场"
    return f if f.startswith("雀魂") else f"雀魂 {f}"


def ms_final_point(p) -> int:
    """GameEndResult.PlayerItem 的终局绝对点数，服务端放在 part_point_1/2 里。

    实测（2026-11 CN，比赛场/段位场各几场对过牌谱末局）绝对点数 − total_point
    = 10000×名次，即 total_point 还叠了顺位奖励，拿它当点数会播出不可能的负数。
    """
    return int(p.part_point_1) + int(p.part_point_2)


def ms_short_id(uuid_str: str) -> str:
    """雀魂那场的四位码，带 m 前缀（mahjong soul）。

    前缀是为了和天凤码彻底分家：两边都取 md5 前 4 位时同一个码可能同时属于
    一局天凤、一局雀魂，wg 只能猜一边（实测 wg b970 被天凤那局截走）。加上 m
    之后 `wg m3f9a` 只可能是雀魂、`wg a1b2` 只可能是天凤，指令不用再查两边。
    """
    return "m" + hashlib.md5(uuid_str.encode()).hexdigest()[:4]


def ms_contest_label(name: str) -> str:
    """比赛全名太长（实测 40+ 字），播报里只用段位组那一段。"""
    label = ((name or "").split(" ", 1)[0] or name or "").strip()
    return label[:14]


def ms_record_row(head, contest: str = "") -> dict:
    """GameRecordHead / RecordGame → 跟踪列表用的一行（已终局的对局）。

    比赛牌谱的 RecordGame 与 fetchGameRecord 的 head 字段一致，两处共用。
    """
    acc = {a.seat: a for a in head.accounts}
    tiers = []
    players = []
    for seat in sorted(acc):
        a = acc[seat]
        lvl_id = a.level.id
        tiers.append(lvl_id // 100 % 100)
        players.append({
            "seat": seat,
            "account_id": a.account_id or 0,
            "nickname": a.nickname or "",
            "level": lvl_id,
            "rank": ms_rank_name(lvl_id),
        })
    row = {
        "uuid": head.uuid,
        "start_time": head.start_time,
        "end_time": head.end_time,
        "category": head.config.category if head.HasField("config") else 0,
        "mode": head.config.mode.mode if head.HasField("config") else 0,
        "field": contest or ms_room_name(head.config if head.HasField("config") else None, tiers),
        "players": players,
    }
    if contest:
        row["contest"] = contest
    return row


# type_url 末段 → 归一化类型名。
# 雀魂的 action_data 有两种包装：牌谱里是 .lq.Record*，实时流里是 .lq.Action*，
# 两者字段布局一致。统一归一化到 Record*，调用方只需比较一种名字。
_TWIN_MAP = {}
for _n in dir(pb):
    if _n.startswith("Record"):
        _TWIN_MAP.setdefault("Action" + _n[6:], _n)
        _TWIN_MAP.setdefault(_n, _n)
    elif _n.startswith("Action"):
        _TWIN_MAP.setdefault(_n, _n)
_TWIN_MAP.setdefault("GameEnd", "GameEnd")
_TWIN_MAP.setdefault("GameDetailRecords", "GameDetailRecords")


def decode_action_data(data: bytes):
    """解码一段 action_data/result，返回 (type_name, message)。

    只认 Any 的 type_url，不再用"逐类型试解析"的启发式：
    protobuf 会把不匹配的字段当作 unknown field 照样解析成功，
    实测把 .lq.RecordBaBei 误判成 GameEnd，直接污染【对局完成】的分数。
    """
    from google.protobuf import any_pb2
    if not data:
        return None, None
    try:
        a = any_pb2.Any()
        a.MergeFromString(data)
        base = a.type_url.split(".")[-1] if a.type_url else ""
    except Exception:
        return None, None
    if not base or not a.value:
        return None, None
    norm = _TWIN_MAP.get(base, base)
    cls = getattr(pb, base, None) or getattr(pb, norm, None)
    if cls is None:
        log.debug(f"[ms] 未知 action 类型: {base}")
        return None, None
    try:
        m = cls()
        m.MergeFromString(a.value)
    except Exception as e:
        log.debug(f"[ms] {base} 解析失败: {e}")
        return None, None
    return norm, m


def ms_pt_text(rank: str, pt) -> str:
    """魂天N 的点数除 100 留一位小数（客户端真值 880→8.8、1050→10.5），其余原样。"""
    name = str(rank)
    if name.startswith("魂天") and name != "魂天":
        try:
            return f"{int(pt) / 100:.1f}"
        except (TypeError, ValueError):
            pass
    return str(pt)


def ms_level_text(rank: str, pt, max_pt) -> str:
    """雀魂座位那一列：`雀杰一 603/1200`。

    分母是 ms_level_up() 那张本地升段上限表给的数（服务端不下发 next_score）；
    魂天/魂天N 表里是 0，没有上限可写就只报点数。段位查不到就整列空着，
    宁可少一列也不编一个段位。
    """
    if not rank or rank in ("未知", "?"):
        return ""
    if pt is None:
        return rank
    try:
        score = int(pt)
    except (TypeError, ValueError):
        return rank
    try:
        cap = int(max_pt or 0)
    except (TypeError, ValueError):
        cap = 0
    text = ms_pt_text(rank, score)
    return f"{rank} {text}/{cap}" if cap > 0 else f"{rank} {text}"


# AccountStatisticByGameMode.round_end 的 type：协议里只有 uint32，官方 liqi.proto
# 同样没有枚举。2/3/4 这三个是 2026-11 拿一个账号的资料界面真值逐项锚死的：
# 界面「和了巡數」「平均打點」都等于 xun_count_sum / dadian_sum 除以和了次数，
# 只有 type2+type3 当分母时这两条和「和牌率」「自摸率」四项同时精确吻合
# （928 场那一档：25.15% / 30.94% / 11.80 / 5532 / 11.80 全对）。
# 剩下 0/1/5 是流局与他家和了的三种收场，六个 sum 恰等于总手数，但这三个
# 没有独立锚能一一定死，播报用不到，就不猜。
MS_END_TSUMO = 2
MS_END_RON = 3
MS_END_DEALIN = 4


def ms_rate_text(gm: dict) -> str:
    """雀魂那五个率：`顺2.35 和25.15% 铳11.80% 副37.69% 立17.41%`。

    口径逐项对齐个人资料界面：平均顺位取 game_final_position 名次分布加权，
    和了率 = (自摸 type2 + 点和 type3)/总手数，放铳率 = type4/总手数，
    副露率和立直率各自除以总手数。
    这一档一场没打过（总手数 0）就整段空着，不拿别的场兜底。
    """
    n = int(gm.get("round_count_sum") or 0)
    if n <= 0:
        return ""
    ends = {int(e.get("type") or 0): int(e.get("sum") or 0)
            for e in gm.get("round_end") or []}
    pos = [int(v) for v in gm.get("game_final_position") or []]
    bits = []
    if sum(pos):
        avg = sum((i + 1) * v for i, v in enumerate(pos)) / sum(pos)
        bits.append(f"顺{avg:.2f}")
    hu = ends.get(MS_END_TSUMO, 0) + ends.get(MS_END_RON, 0)
    if hu:
        bits.append(f"和{hu / n * 100:.2f}%")
    bits.append(f"铳{ends.get(MS_END_DEALIN, 0) / n * 100:.2f}%")
    bits.append(f"副{int(gm.get('ming_count_sum') or 0) / n * 100:.2f}%")
    bits.append(f"立{int(gm.get('liqi_count_sum') or 0) / n * 100:.2f}%")
    return " ".join(bits)


def ms_format_pt(stats: dict) -> str:
    """把 player_stats 返回的 dict 格式化成给人看的文本。

    四人(level) 与三人(level3) 段位、PT 分开显示。
    上限为 0（魂天/魂天N）时不写分母，与座位列同口径。
    stats 结构: nickname, rank/pt/max_pt(四人), rank3/pt3/max_pt3(三人)
    """
    nick = stats.get("nickname", "?")

    # 四人
    rank = stats.get("rank", "?")
    pt = stats.get("pt", 0)
    max_pt = stats.get("max_pt", 0)

    # 三人
    rank3 = stats.get("rank3")
    pt3 = stats.get("pt3")
    max_pt3 = stats.get("max_pt3")

    text = f"{rank} {ms_pt_text(rank, pt)}"
    if max_pt != 0:
        text += f"/{max_pt}"
    if rank3 and rank3 != "未知":
        # 同时有三人数据
        text += f"\n{rank3} {ms_pt_text(rank3, pt3)}"
        if max_pt3 != 0:
            text += f"/{max_pt3}"

    return f"{nick}\n{text}"


class MSRpcError(RuntimeError):
    """服务器返回了非 0 的 error.code。旧版一律忽略，导致上层只看到空结果。

    Error 里除 code 外还带 u32_params/str_params/json_param——这些才是服务端
    对"为什么不给读"的说明，只报 code 分不清到底是权限问题还是时机问题。
    """

    def __init__(self, method: str, code: int, params: str = ""):
        super().__init__(f"{method} 被服务器拒绝 (code={code}"
                         + (f", {params}" if params else "") + ")")
        self.method = method
        self.code = code
        self.params = params


def _check_error(res, method: str) -> None:
    err = getattr(res, "error", None)
    code = getattr(err, "code", 0) if err is not None else 0
    if code:
        raise MSRpcError(method, code, _error_params(err))


def _error_params(err) -> str:
    """Error 的附加参数，压成一行短文本；取不到就空串。"""
    if err is None:
        return ""
    parts = []
    try:
        if err.u32_params:
            parts.append("u32=" + ",".join(str(v) for v in err.u32_params[:4]))
        if err.str_params:
            parts.append("str=" + ",".join(
                str(v)[:24] for v in err.str_params[:4]))
        if err.json_param:
            parts.append("json=" + str(err.json_param)[:120])
    except Exception:
        pass
    return " ".join(parts)


class MahjongSoulClient:
    """WebSocket 登录 + RPC 查询客户端。线程安全（内部单事件循环）。

    连接层实测（2026-11 CN 服）：网关连接会空闲断开，旧版没有任何重连，
    容器挂几小时后所有 RPC 只剩超时且被 except 吞掉；重新握手只要 2-5 秒，
    所以每次 RPC 前做一次 alive 检查，断了就重连+重登。
    """

    RECONNECT_COOLDOWN = 20.0
    RANK_CACHE_TTL = 1200.0

    def __init__(self, email: str = "", password: str = "", token: str = ""):
        self.email = email
        self.password = password
        self.access_token = token
        # 只有真正走完 requestConnection + login 才算登录；配置的 token 无法
        # 用于匿名重连（网关仍要求握手），旧版凭 token 直接短路会谎报已登录。
        self._authed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ch = None
        self._lock = threading.Lock()
        self._next_conn = 0.0
        self._version = ""
        self._cfg = None
        self._cvs = ""
        self._route_id = 0
        self.last_error = ""
        self._live_cache: list = []
        self._live_cache_at = 0.0
        # 段位/PT 缓存：account_id → (时间, fetchAccountInfo 结果)
        self._rank_cache: dict = {}
        # 五个率缓存：account_id → (时间, fetchAccountStatisticInfo 结果)
        self._stat_cache: dict = {}
        self._contest_cache: list = []
        self._contest_cache_at = 0.0
        # 已留过判场样本的 filter_id（见 _log_unanchored_mode）
        self._fid_logged: set = set()

    # ---- 连接层 ----

    def _ensure_loop(self):
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, daemon=True).start()

    def ensure_connection(self) -> bool:
        """保证网关连接已建立并完成登录握手。带冷却，避免断开时疯狂重连。"""
        self._ensure_loop()
        with self._lock:
            if self._ch is not None and self._ch.alive and self._authed:
                return True
            now = time.time()
            if now < self._next_conn:
                return False
            self._next_conn = now + self.RECONNECT_COOLDOWN
        try:
            fut = asyncio.run_coroutine_threadsafe(self._reconnect_async(), self._loop)
            return fut.result(75)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning(f"[ms] 重连失败: {self.last_error}")
            return False

    async def _reconnect_async(self) -> bool:
        await self._close_channel()
        await self._connect_async()
        return await self._handshake_async()

    async def _close_channel(self):
        ch, self._ch = self._ch, None
        self._authed = False
        if ch is None:
            return
        try:
            await ch.close()
        except Exception:
            pass

    def _spawn(self, factory, timeout: float = 30.0):
        """在内部循环上执行 factory() 协程；连接断开则重连并重试一次。"""
        if not self.ensure_connection():
            raise ConnectionError(self.last_error or "雀魂网关未连接")
        fut = asyncio.run_coroutine_threadsafe(factory(), self._loop)
        try:
            return fut.result(timeout)
        except (ConnectionError, TimeoutError, OSError) as e:
            log.warning(f"[ms] RPC 失败({type(e).__name__})，重连后重试: {e}")
            with self._lock:
                self._authed = False
                self._next_conn = 0.0
            if not self.ensure_connection():
                raise
            return asyncio.run_coroutine_threadsafe(factory(), self._loop).result(timeout)

    def login(self) -> bool:
        """同步登录（建立连接 + 握手）。"""
        try:
            self._ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(self._login_async(), self._loop)
            return fut.result(75)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning(f"[ms] 登录失败: {self.last_error}")
            return False

    async def _login_async(self) -> bool:
        if self._ch is None or not self._ch.alive:
            await self._close_channel()
            await self._connect_async()
        return await self._handshake_async()

    async def _connect_async(self):
        async with _session() as s:
            async with s.get(f"{GAME_URL}/1/version.json", ssl=False) as r:
                self._version = (await r.json())["version"]
            async with s.get(f"{GAME_URL}/1/v{self._version}/config.json", ssl=False) as r:
                self._cfg = await r.json()
            async with s.get(f"{GAME_URL}/1/", ssl=False) as r:
                html = await r.text()
            pm = re.search(r"productVersion\s*:\s*[\"']([^\"']+)[\"']", html)
            self._cvs = f"web-{pm.group(1) if pm else self._version.replace('.w', '')}"
            for g in self._cfg["ip"][0]["gateways"]:
                self._route_id = g["id"]
                host = g["url"].replace("https://", "").replace(":8443", "")
                try:
                    ch = MSRPCChannel(f"wss://{host}/gateway")
                    ch._ws = await websockets.connect(
                        ch._endpoint, origin=GAME_URL, additional_headers={"User-Agent": UA},
                        ping_interval=20, open_timeout=10)
                    ch._msg_dispatcher = asyncio.create_task(ch.dispatch_msg())
                    self._ch = ch
                    return
                except Exception as e:
                    log.debug(f"[ms] 网关 {host} 连接失败: {e}")
                    self._ch = None
                    continue
            raise ConnectionError("无法连接雀魂网关")

    async def _handshake_async(self) -> bool:
        """requestConnection + login。缺 platform="Web" 时网关返回 151。"""
        if not (self.email and self.password):
            self.last_error = "未配置雀魂账号密码"
            return False
        ch = self._ch
        if ch is None or not ch.alive:
            self.last_error = "网关未连接"
            return False
        route = Route(ch)
        lobby = Lobby(ch)
        rreq = pb.ReqRequestConnection()
        rreq.type = 1
        rreq.route_id = self._route_id
        rreq.timestamp = int(time.time())
        # 关键：platform="Web"（proto 字段6），缺失则网关返回151
        await route._channel.send_request(
            ".lq.Route.requestConnection", rreq.SerializeToString() + b"\x32\x03Web")
        req = pb.ReqLogin()
        req.account = self.email
        req.password = hmac.new(b"lailai", self.password.encode(), hashlib.sha256).hexdigest()
        req.reconnect = False
        req.device.platform = "pc"; req.device.hardware = "pc"; req.device.os = "windows"
        req.device.os_version = "win10"; req.device.is_browser = True
        req.device.software = "Chrome"; req.device.sale_platform = "web"
        req.device.screen_width = 1920; req.device.screen_height = 1080
        req.device.user_agent = UA; req.device.screen_type = 1
        req.random_key = str(uuid.uuid1())
        req.client_version.resource = self._version
        req.gen_access_token = True
        req.currency_platforms.append(2)
        req.type = 0
        req.client_version_string = self._cvs
        req.tag = "cn"
        res = await lobby.login(req)
        _check_error(res, "login")
        self.access_token = getattr(res, "access_token", "") or self.access_token
        self._authed = True
        self.last_error = ""
        log.info("[ms] 登录完成")
        return True

    @property
    def authed(self) -> bool:
        return self._authed and self._ch is not None and self._ch.alive

    def lookup_player(self, account_id=None, nickname=None) -> dict:
        """查询玩家段位/PT（account_id 优先，其次按昵称搜索）。

        已登录：走 WebSocket RPC（含三人数据，最准）。
        未登录/连不上：牌谱屋 API 兜底，仅支持按昵称。
        """
        if not self.ensure_connection():
            if nickname:
                return self._koromo_lookup_nickname(nickname)
            return {}
        if account_id is not None:
            aid = account_id
        elif nickname:
            aid = self._search_aid_by_nickname(nickname)
            if aid is None:
                return {}
        else:
            return {}
        try:
            return self._spawn(lambda: self._fetch_info(aid), 30)
        except Exception:
            # RPC 失败时回落牌谱屋（仅当给了昵称，能直接按昵称搜）
            if nickname:
                koromo = self._koromo_lookup_nickname(nickname)
                if koromo:
                    return koromo
            return {}

    def seat_bits(self, players, three: bool) -> list:
        """各家 `段位 pt/上限`，与 players 下标对位；查不到的座位给空串。

        live list 里不带 PT，只能 fetchAccountInfo 一家问一次，所以按账号缓存
        20 分钟——同一个人连着开几场不必反复问。三麻场问的是 level3。
        """
        out = []
        for p in players or []:
            aid = p.get("account_id") if isinstance(p, dict) else None
            info = self._rank_of(aid) if aid else {}
            if three:
                # 三麻只认 level3；列表行那列除比赛场外都取 level（四麻），不兜底
                out.append(ms_level_text(info.get("rank3") or "",
                                         info.get("pt3"), info.get("max_pt3")))
            else:
                bit = ms_level_text(info.get("rank") or "",
                                    info.get("pt"), info.get("max_pt"))
                if not bit:
                    # 查不到人：列表行自带的段位还能报，只是没有分数
                    bit = ms_level_text(p.get("rank") or "", None, None)
                out.append(bit)
        return out

    def _rank_of(self, account_id: int) -> dict:
        now = time.time()
        hit = self._rank_cache.get(account_id)
        if hit and now - hit[0] < self.RANK_CACHE_TTL:
            return hit[1]
        info = self.lookup_player(account_id=account_id) or {}
        if not info:
            return {}          # 查失败别进缓存，下一场还想再试一次
        if len(self._rank_cache) > 512:
            self._rank_cache.clear()
        self._rank_cache[account_id] = (now, info)
        return info

    def seat_rates(self, players, mode) -> list:
        """各家五个率，与 players 下标对位；查不到或这一档没打过给空串。

        mode 就是这一场 GameConfig.mode.mode（1=四東 2=四南 11=三東 12=三南），
        和个人资料界面那四页一一对应。只有段位场有对应的那一页，比赛场/友人房
        传 mode=0，整段不播——拿段位场的数据去说一场友谊赛是另一回事。
        """
        mode = int(mode or 0)
        if mode not in (1, 2, 11, 12):
            return ["" for _ in players or []]
        out = []
        for p in players or []:
            aid = p.get("account_id") if isinstance(p, dict) else None
            out.append(ms_rate_text(self._stat_mode(aid, mode)) if aid else "")
        return out

    def _stat_mode(self, account_id: int, mode: int) -> dict:
        """段位场统计里 mode 那一档；回包按账号缓存 20 分钟（和段位分同一口径）。"""
        now = time.time()
        hit = self._stat_cache.get(account_id)
        if hit and now - hit[0] < self.RANK_CACHE_TTL:
            resp = hit[1]
        else:
            try:
                resp = self.stat_raw(account_id) or {}
            except Exception as e:
                log.debug(f"[ms] 统计取不到 {account_id}: {type(e).__name__}: {e}")
                return {}
            if not resp:
                return {}      # 查失败别进缓存，下一场还想再试一次
            if len(self._stat_cache) > 512:
                self._stat_cache.clear()
            self._stat_cache[account_id] = (now, resp)
        blk = ((resp.get("detail_data") or {}).get("rank_statistic") or {})
        blk = ((blk.get("total_statistic") or {}).get("all_level_statistic") or {})
        for gm in blk.get("game_mode") or []:
            if int(gm.get("mode") or 0) == mode:
                return gm
        return {}

    def _koromo_lookup_nickname(self, nickname: str) -> dict:
        """牌谱屋按昵称反查：search_player/<昵称> 一步返回 id+level（段位+分数）。

        四人(pl4)与三人(pl3)是两条记录，都查、命中后合并；精确匹配昵称。
        未找到或网络异常返回 {}。

        注意：牌谱屋 search_player 返回不带服务器字段，单条精确匹配时视为
        国服；若同一昵称命中多条（重名/跨服），调用方应走 _koromo_search_candidates
        列出候选由用户选择，不要盲目取第一条。
        """
        d: dict = {}
        for suffix, three in (("pl4", False), ("pl3", True)):
            try:
                items = _koromo_search_player(suffix, nickname)
                for item in items or []:
                    if item.get("nickname") != nickname or not item.get("id"):
                        continue
                    if not d:
                        d = {"account_id": int(item["id"]),
                             "nickname": item.get("nickname") or "?"}
                    lvl = item.get("level") or {}
                    level_id = lvl.get("id") or 0
                    if three:
                        d["rank3"] = ms_rank_name(level_id)
                        d["pt3"] = lvl.get("score", 0)
                        d["max_pt3"] = ms_level_up(level_id)
                        d["level3"] = level_id
                    else:
                        d["rank"] = ms_rank_name(level_id)
                        d["pt"] = lvl.get("score", 0)
                        d["max_pt"] = ms_level_up(level_id)
                        d["level"] = level_id
            except Exception:
                continue
        return d

    def koromo_search_candidates(self, nickname: str) -> list:
        """公开：按昵称搜索候选（免登录，牌谱屋）。供 mspt/msadd 在重名时列出选择。"""
        return self._koromo_search_candidates(nickname)

    def _koromo_search_candidates(self, nickname: str) -> list:
        """按昵称搜索候选玩家（牌谱屋，免登录），用于重名/多服时的选择。

        只返回精确匹配昵称的记录，每条含 account_id、昵称、四人/三人段位与 PT。
        牌谱屋不返回服务器字段，因此无法直接判定国服，需由调用方决定；
        单条候选时可直接使用，多条时列出给用户挑。
        网络异常或无匹配返回 []。
        """
        seen = {}
        for suffix, three in (("pl4", False), ("pl3", True)):
            try:
                items = _koromo_search_player(suffix, nickname)
                for item in items or []:
                    if item.get("nickname") != nickname or not item.get("id"):
                        continue
                    aid = int(item["id"])
                    entry = seen.get(aid)
                    if entry is None:
                        entry = {"account_id": aid,
                                 "nickname": item.get("nickname") or nickname,
                                 "rank": "?", "pt": 0, "max_pt": 0,
                                 "rank3": None, "pt3": 0, "max_pt3": 0}
                        seen[aid] = entry
                    lvl = item.get("level") or {}
                    level_id = lvl.get("id") or 0
                    if three:
                        entry["rank3"] = ms_rank_name(level_id)
                        entry["pt3"] = lvl.get("score", 0)
                        entry["max_pt3"] = ms_level_up(level_id)
                    else:
                        entry["rank"] = ms_rank_name(level_id)
                        entry["pt"] = lvl.get("score", 0)
                        entry["max_pt"] = ms_level_up(level_id)
            except Exception:
                continue
        return list(seen.values())

    def _search_aid_by_nickname(self, nickname: str) -> Optional[int]:
        """按昵称搜索 account_id。

        顺序：牌谱屋 API(免登录) → 实时观战列表 → 官方 searchAccountByPattern。
        返回精确匹配的第一个 account_id；找不到返回 None。
        """
        try:
            return self._spawn(
                lambda: self._search_by_nickname_async(nickname), 30)
        except Exception:
            return None

    async def _search_by_nickname_async(self, nickname: str) -> Optional[int]:
        # 1) 牌谱屋（amae-koromo）公开 API：按昵称反查 account_id，无需登录态。
        #    先查四人场(pl4)，再查三人场(pl3)，取精确匹配的第一个。
        for suffix in ("pl4", "pl3"):
            try:
                items = await asyncio.to_thread(_koromo_search_player, suffix, nickname)
                for item in items or []:
                    if item.get("nickname") == nickname and item.get("id"):
                        return int(item["id"])
            except Exception:
                continue
        # 2) 实时观战列表精确匹配（覆盖正在对局的玩家，需登录态）
        try:
            live = await self._fetch_live_list()
            for game in live:
                for p in game.get("players", []):
                    if p.get("nickname") == nickname and p.get("account_id"):
                        return p["account_id"]
        except Exception:
            pass
        # 3) 官方 searchAccountByPattern（服务器大多不支持，命中为空则返回 None）
        lobby = Lobby(self._ch)
        req = pb.ReqSearchAccountByPattern()
        req.pattern = nickname
        res = await lobby.search_account_by_pattern(req)
        if res.error and res.error.code:
            return None
        first = None
        for aid in res.match_accounts:
            try:
                info = await self._fetch_info(aid)
            except Exception:
                continue
            if first is None:
                first = aid
            if info.get("nickname") == nickname:
                return aid
        return first

    async def _fetch_info(self, aid: int) -> dict:
        lobby = Lobby(self._ch)
        ri = pb.ReqAccountInfo(); ri.account_id = aid
        ai = await lobby.fetch_account_info(ri)
        _check_error(ai, "fetchAccountInfo")
        acc = ai.account
        lvl = acc.level
        lvl3 = acc.level3 if acc.HasField("level3") else None
        d = {
            "account_id": aid,
            "nickname": acc.nickname,
            "rank": ms_rank_name(lvl.id),
            "pt": lvl.score,
            "max_pt": ms_level_up(lvl.id),
            "level": lvl.id,
            "level3": lvl3.id if lvl3 else 0,
        }
        if lvl3 is not None and lvl3.id:
            d["rank3"] = ms_rank_name(lvl3.id)
            d["pt3"] = lvl3.score
            d["max_pt3"] = ms_level_up(lvl3.id)
        return d

    def stat_raw(self, account_id: int) -> dict:
        """fetchAccountStatisticInfo 的原样回包 → dict，供 _stat_mode 取那一档计数器。"""
        return self._spawn(lambda: self._fetch_stat(account_id), 30)

    async def _fetch_stat(self, aid: int) -> dict:
        from google.protobuf.json_format import MessageToDict
        lobby = Lobby(self._ch)
        ri = pb.ReqAccountStatisticInfo(); ri.account_id = aid
        res = await lobby.fetch_account_statistic_info(ri)
        _check_error(res, "fetchAccountStatisticInfo")
        return MessageToDict(res, preserving_proto_field_name=True)

    # ---- 实时观战（live）接口 ----

    def get_live_list(self, max_age: float = 0.0) -> list:
        """当前进行中的对局列表。

        max_age>0 时使用缓存：全量 sweep 要发 30 个请求，轮询线程没必要
        每 30 秒重跑一次；开局检测只需要 uuid→玩家/场 的映射稍微滞后无妨。
        """
        if max_age > 0:
            with self._lock:
                if self._live_cache and time.time() - self._live_cache_at < max_age:
                    return self._live_cache
        live = self._spawn(self._fetch_live_list, 60)
        with self._lock:
            self._live_cache = live
            self._live_cache_at = time.time()
        return live

    async def _fetch_live_list(self) -> list:
        # 遍历所有有效模式（LIVE_MODE_IDS）拉取，按 uuid 去重合并为全量可观战对局。
        # 单个模式请求失败（如 code 8 或网络抖动）不影响其他模式，仍返回已拉到的部分；
        # 调用方可在结果为空时再用 fetchAccountState 兜底检测 playing 状态。
        seen = set()
        out = []
        # 分批并发（每批 6 个），避免一次性发起过多请求触发服务器限流。
        for i in range(0, len(LIVE_MODE_IDS), 6):
            chunk = LIVE_MODE_IDS[i:i + 6]
            results = await asyncio.gather(
                *(self._fetch_live_list_mode(fid) for fid in chunk),
                return_exceptions=True)
            for r in results:
                if isinstance(r, Exception) or not r:
                    continue
                for game in r:
                    if game["uuid"] in seen:
                        continue
                    seen.add(game["uuid"])
                    out.append(game)
        return out

    async def _fetch_live_list_mode(self, filter_id: int) -> list:
        """拉取单个模式 filter_id 下的进行中对局列表。

        无效模式或服务器返回错误（如 code 8）时返回空列表，不抛异常。
        """
        lobby = Lobby(self._ch)
        req = pb.ReqGameLiveList(); req.filter_id = filter_id
        res = await lobby.fetch_game_live_list(req)
        if res.HasField("error") and res.error.code != 0:
            return []
        self._log_unanchored_mode(filter_id, res.live_list)
        return [self._live_row(lv, filter_id=filter_id) for lv in res.live_list]

    def _log_unanchored_mode(self, filter_id: int, live_list) -> None:
        """还没锚定场名的 filter_id，第一次拿到对局时留一行样本，好补 MS_LIVE_FID_FIELD。

        每个 filter_id 只记一次：判场靠的是"请求哪个 fid 得到哪批局"，样本不用一直刷。
        """
        if not live_list or filter_id in MS_LIVE_FID_FIELD:
            return
        if filter_id in self._fid_logged:
            return
        self._fid_logged.add(filter_id)
        cfg = live_list[0].game_config
        tiers = [p.level.id // 100 % 100 for p in live_list[0].players]
        log.info(f"[ms] live fid={filter_id} 未锚定：mode_id={cfg.meta.mode_id} "
                 f"mode={cfg.mode.mode} 行数={len(live_list)} 首家段位={tiers}")

    @staticmethod
    def _live_row(lv, contest: str = "", filter_id: int = 0) -> dict:
        """GameLiveHead → 跟踪列表用的一行。

        contest 非空时是比赛场：场名直接用比赛名（段位推断对比赛没意义）。
        filter_id 是拉这一行时用的场参数，判场比段位推断可靠。
        """
        players = []
        tiers = []
        for idx, p in enumerate(lv.players):
            if contest and p.level3.id:
                lvl_id = p.level3.id
            else:
                lvl_id = p.level.id if p.HasField("level") else 0
            tiers.append(lvl_id // 100 % 100)
            players.append({
                "seat": idx,
                "account_id": p.account_id or 0,
                "nickname": p.nickname or "",
                "level": lvl_id,
                "rank": ms_rank_name(lvl_id),
            })
        row = {
            "uuid": lv.uuid,
            "start_time": lv.start_time,
            "category": lv.game_config.category,
            # GameConfig.mode.mode：1/2/11/12，和 fetchAccountStatisticInfo 里
            # AccountStatisticByGameMode.mode 同一套编号，五个率按它取对应那一档
            "mode": lv.game_config.mode.mode,
            "mode_id": lv.game_config.meta.mode_id,
            "filter_id": filter_id,
            "field": contest or ms_room_name(lv.game_config, tiers, filter_id),
            "players": players,
            "live": True,
        }
        if contest:
            row["contest"] = contest
        return row

    # ---- 比赛场（自定义赛事）----

    def get_contest_rows(self, record_limit: int = 3, contest_id: int = 0,
                         unique_id: str = "") -> list:
        """比赛场的对局行：进行中的 + 每场最近终局的（后者只能走牌谱回放）。

        默认只看账号参加的比赛；contest_id / unique_id 可以指到别的比赛。
        行形状与 get_live_list 一致，多带 contest / live 两个键。
        """
        return self._spawn(
            lambda: self._fetch_contest_rows(record_limit, contest_id, unique_id), 90)

    async def _fetch_contest_rows(self, record_limit: int,
                                  contest_id: int = 0, unique_id: str = "") -> list:
        lobby = Lobby(self._ch)
        items = []
        if contest_id:
            res = await lobby.fetch_customized_contest_by_contest_id(
                pb.ReqFetchCustomizedContestByContestId(contest_id=int(contest_id)))
            _check_error(res, "fetchCustomizedContestByContestId")
            if res.HasField("contest_info"):
                ci = res.contest_info
                items = [(ci.unique_id, ci.contest_name, ci.contest_id)]
        else:
            res = await lobby.fetch_customized_contest_list(pb.ReqFetchCustomizedContestList())
            _check_error(res, "fetchCustomizedContestList")
            items = [(ct.unique_id, ct.contest_name, ct.contest_id) for ct in res.contests]
            if unique_id:
                items = [it for it in items if str(it[0]) == str(unique_id)]
                if not items:
                    items = [(int(unique_id), "", 0)]
        out = []
        for uid, raw_name, cid in items:
            full = (raw_name or "").strip() or f"比赛{uid}"
            name = ms_contest_label(full)
            try:
                lr = await lobby.fetch_customized_contest_game_live_list(
                    pb.ReqFetchCustomizedContestGameLiveList(unique_id=int(uid)))
            except Exception as e:
                log.debug(f"[ms] 比赛直播列表失败 uid={uid}: {e}")
                lr = None
            if lr is not None and not (lr.HasField("error") and lr.error.code):
                for lv in lr.live_list:
                    row = self._live_row(lv, contest=name)
                    row["contest_full"] = full
                    row["contest_id"] = cid
                    out.append(row)
            try:
                rr = await lobby.fetch_customized_contest_game_records(
                    pb.ReqFetchCustomizedContestGameRecords(unique_id=int(uid)))
            except Exception as e:
                log.debug(f"[ms] 比赛牌谱索引失败 uid={uid}: {e}")
                continue
            if rr.HasField("error") and rr.error.code:
                continue
            for rg in list(rr.record_list)[:record_limit]:
                row = ms_record_row(rg, contest=name)
                row["live"] = False
                row["contest_full"] = full
                row["contest_id"] = cid
                out.append(row)
        return out

    def get_account_states(self, account_ids: list) -> dict:
        """查询一批账号的在线/对局状态（fetchAccountState，无需观战权限）。

        返回 {account_id: {"is_online": bool, "playing_uuid": str|None,
                           "category": int, "meta": dict|None}}
        网络异常或无结果返回 {}。
        """
        if not account_ids:
            return {}
        try:
            return self._spawn(lambda: self._fetch_account_states(account_ids), 30)
        except Exception as e:
            log.warning(f"[ms] 状态查询失败: {e}")
            return {}

    async def _fetch_account_states(self, account_ids: list) -> dict:
        lobby = Lobby(self._ch)
        req = pb.ReqAccountList()
        req.account_id_list.extend(account_ids)
        res = await lobby.fetch_account_state(req)
        out = {}
        if res.HasField("error") and res.error.code != 0:
            return out
        for st in res.states:
            playing_uuid = None
            category = 0
            meta = None
            if st.HasField("playing"):
                playing_uuid = st.playing.game_uuid or None
                category = st.playing.category or 0
                if st.playing.HasField("meta"):
                    meta = {
                        "room_id": st.playing.meta.room_id,
                        "mode_id": st.playing.meta.mode_id,
                    }
            out[st.account_id] = {
                "is_online": st.is_online,
                "playing_uuid": playing_uuid,
                "category": category,
                "meta": meta,
            }
        return out

    def get_game_record(self, uuid_str: str) -> dict:
        """拉取已结束对局的完整牌谱（GameDetailRecords.actions）。
        返回 {"head": {...}, "actions": [GameAction...]}"""
        return self._spawn(lambda: self._fetch_game_record(uuid_str), 30)

    async def _fetch_game_record(self, uuid_str: str) -> dict:
        lobby = Lobby(self._ch)
        req = pb.ReqGameRecord()
        req.game_uuid = uuid_str
        req.client_version_string = self._cvs
        res = await lobby.fetch_game_record(req)
        _check_error(res, "fetchGameRecord")
        head_info = {}
        if res.HasField("head"):
            h = res.head
            acc_by_seat = {}
            for a in h.accounts:
                acc_by_seat[a.seat] = a
            point_by_seat = {}
            pt_by_seat = {}
            if h.HasField("result"):
                for p in h.result.players:
                    point_by_seat[p.seat] = ms_final_point(p)
                    pt_by_seat[p.seat] = float(p.grading_score)
            players = []
            tiers = []
            for seat in range(4):
                a = acc_by_seat.get(seat)
                if a is None:
                    continue
                tiers.append(a.level.id // 100 % 100)
                players.append({
                    "seat": seat,
                    "account_id": a.account_id or 0,
                    "nickname": a.nickname or "",
                    "level": a.level.id,
                    "rank": ms_rank_name(a.level.id),
                    # 服务端给的终局绝对点数（不是相对原点的净胜点）
                    "final_point": point_by_seat.get(seat, 0),
                    "grading_score": pt_by_seat.get(seat, 0.0),
                })
            head_info = {
                "uuid": h.uuid,
                "start_time": h.start_time,
                "end_time": h.end_time,
                "category": h.config.category,
                "field": ms_room_name(h.config, tiers),
                "players": players,
            }
        # res.data 是 Any 包装的 GameDetailRecords
        actions = []
        type_name, gd = decode_action_data(res.data)
        if type_name == "GameDetailRecords":
            for a in gd.actions:
                actions.append({"type": a.type, "result": a.result})
        return {"head": head_info, "actions": actions}

    def get_game_detail(self, uuid_str: str) -> dict:
        """完整牌谱 + 选手信息，交给 ms_live 逐局解析。

        未结束时服务器回 1203（牌谱只在终局后生成），这里原样抛 MSRpcError，
        由调用方继续轮询，不做任何"猜"的兜底。
        """
        return self._spawn(lambda: self._fetch_game_detail(uuid_str), 30)

    async def _fetch_game_detail(self, uuid_str: str) -> dict:
        lobby = Lobby(self._ch)
        req = pb.ReqGameRecord()
        req.game_uuid = uuid_str
        req.client_version_string = self._cvs
        res = await lobby.fetch_game_record(req)
        _check_error(res, "fetchGameRecord")
        out = {"uuid": uuid_str, "head_obj": None, "detail": None,
               "names": [], "field": "", "result": []}
        if res.HasField("head"):
            h = res.head
            out["head_obj"] = h
            acc = {a.seat: a for a in h.accounts}
            tiers = [acc[s].level.id // 100 % 100 for s in range(4) if s in acc]
            out["names"] = [acc[s].nickname if s in acc else "" for s in range(4)]
            out["field"] = ms_room_name(h.config, tiers)
            if h.HasField("result"):
                # [(座位, 终局绝对点数, 段位分变化)]
                # grading_score 是段位分变化——比赛场那几场服务端填的是 0。
                out["result"] = [(int(p.seat), ms_final_point(p),
                                  float(p.grading_score))
                                 for p in h.result.players]
            out["start_time"] = h.start_time
            out["end_time"] = h.end_time
        tn, det = decode_action_data(res.data)
        if tn == "GameDetailRecords":
            out["detail"] = det
        return out

    def get_observe_auth(self, uuid_str: str) -> dict:
        """申请观战票：{token, location, delay}。

        用的是网页客户端实测在调的 Lobby.fetchOBToken(uuid)；`createGameObserveAuth`
        对本账号一律 1216，而且抓包看客户端根本不经过它，别再往那边敲。
        location 就是观战节点的 wss 地址（与登录网关同主机的 /ob，实测通用）。
        """
        return self._spawn(lambda: self._fetch_observe_auth(uuid_str), 20)

    async def _fetch_observe_auth(self, uuid_str: str) -> dict:
        lobby = Lobby(self._ch)
        res = await lobby.fetch_ob_token(pb.ReqFetchOBToken(uuid=uuid_str))
        _check_error(res, "fetchOBToken")
        return {"token": res.token, "location": self.observe_endpoint(),
                "delay": res.delay, "create_time": res.create_time,
                "start_time": res.start_time}

    def observe_endpoint(self) -> str:
        """观战节点 = 登录网关同主机的 /ob（实测 route-2/4/5/6 都收同一张票）。"""
        host = ""
        m = re.match(r"wss?://([^/]+)", (self._ch._endpoint if self._ch else "") or "")
        if m:
            host = m.group(1)
        return f"wss://{host or 'route-2.maj-soul.com'}/ob"


# 简易 HTTP 会话（复用代理配置）


def _session():
    import aiohttp
    return aiohttp.ClientSession()


class MahjongSoulWatcher:
    """雀魂开局/结束检测（后台轮询线程）。

    检测源用 fetchGameLiveList：每个 filter_id 返回该场进行中的对局（含各家
    account_id 和昵称），按订阅账号命中即可开局，无需好友关系、无需观战票。
    fetchAccountState 的 playing 字段实测在 CN 服从不下发（订阅号、插件自己的号、
    直播列表现捞的对局三种都验过），只能当补充源，不能依赖。
    """

    # 对局离开 live list 后容忍的轮数；单次拉取抖动不该被当成散场
    END_GRACE = 2
    # live list 每个模式只有 100 行（实测玉之间最旧只见 4 分多钟），掉出列表 ≠ 散场，
    # 所以散场以"牌谱可读"为准。牌谱那条路整个坏掉时，才退回到离场计时。
    END_UNCONFIRMED = 45 * 60
    # 一场对局最长留档，超过就丢弃（掉线/异常时的兜底）
    GAME_TTL = 6 * 3600
    # 牌谱接口连续报"读不了"的错误多少次才彻底放弃这条路
    RECORD_FAIL_LIMIT = 3
    # 这两个 code 是时机问题不是权限问题，等着就好：
    #   1203 = 终局后牌谱还没生成；540 = 对局还在进行中（实测掉出 live list
    #   的进行中局一律回 540，旧版把它当接口坏掉，3 次就把牌谱这条路整个关了，
    #   于是散场退化成 45 分钟计时、终局点数也丢了）
    RECORD_WAIT_CODES = (1203, 540)

    def __init__(self, client, notifier, poll_interval: int = 30,
                 on_new_game=None):
        self.client = client
        # notifier(text, account_ids, field)：由 main 决定推给哪些会话
        self._notifier = notifier
        # 发现新的订阅对局时把这场交给主程序（和天凤同一条路：主程序去挂观战流收终局）
        self.on_new_game = on_new_game
        self._interval = max(15, int(poll_interval))
        self.watching: dict[int, str] = {}   # account_id → 昵称
        self._games: dict[str, dict] = {}    # uuid → 对局状态
        self._thread = None
        self._stop = threading.Event()
        # fetchGameRecord 真报错才关掉这条路；RECORD_WAIT_CODES 只是时机未到
        self.record_detail = True
        self._record_fails = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def add(self, account_id, nickname: str = "") -> None:
        self.watching[int(account_id)] = nickname or str(account_id)

    def remove(self, account_id) -> bool:
        return self.watching.pop(int(account_id), None) is not None

    def clear(self) -> None:
        self.watching.clear()
        self._games.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> list:
        """当前跟踪中的对局，供 wgstat 展示。"""
        out = []
        for g in self._games.values():
            names = " ".join(
                p.get("nickname", "?") for p in g["players"] if p.get("nickname"))
            if not names:
                names = " ".join(self.watching.get(a, str(a)) for a in sorted(g["accounts"]))
            out.append({
                "mid": ms_short_id(g["uuid"]),
                "uuid": g["uuid"],
                "field": g["field"],
                "players": names,
                "since": time.strftime("%H:%M", time.localtime(g["start_ts"])),
                "accounts": sorted(g["accounts"]),
            })
        return out

    def tracked_game(self, code: str) -> Optional[dict]:
        """按四位码取正在跟踪的那场，返回 wg/观战会话能直接用的对局行。

        live list 只有 100 行，开局四分钟后就查不到了；这种场 wg <四位码> 还想
        逐局播，就得从这里的在局表拿完整 uuid。
        """
        code = (code or "").lower()
        for g in self._games.values():
            if ms_short_id(g["uuid"]).lower() != code:
                continue
            return {
                "uuid": g["uuid"],
                "field": g["field"],
                "mode": g.get("mode") or 0,
                "players": list(g["players"]),
                "start_time": g["start_ts"],
                "live": True,
            }
        return None

    def forget(self, uuid_str: str) -> bool:
        """主程序那条自动观战流已把终局播掉了：这场不用再等牌谱。"""
        return self._games.pop(str(uuid_str or ""), None) is not None

    def _announce_new(self, game: dict, members: list) -> None:
        """刚发现的一场交给 on_new_game；给的是快照，之后轮询线程怎么改都不影响它。"""
        if not self.on_new_game:
            return
        try:
            self.on_new_game({
                "uuid": game["uuid"],
                "field": game.get("field") or "",
                "mode": game.get("mode") or 0,
                "players": list(game.get("players") or []),
                "accounts": set(members or ()),
                "start_time": game.get("start_ts"),
                "live": True,
            })
        except Exception as e:
            log.debug(f"[ms] on_new_game 失败: {e}")

    # ---- 轮询 ----

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                log.warning(f"[ms] 轮询异常: {type(e).__name__}: {e}")
            self._stop.wait(self._interval)

    def _poll_once(self) -> None:
        if not self.watching:
            return
        if not self.client.authed and not self.client.login():
            log.info(f"[ms] 未登录，跳过本轮: {self.client.last_error}")
            return
        live = self._live_by_uuid()
        playing: dict[str, list[int]] = {}
        for uuid_str, row in live.items():
            members = sorted({int(p["account_id"]) for p in row.get("players", [])
                              if int(p.get("account_id") or 0) in self.watching})
            if members:
                playing[uuid_str] = members
        try:
            states = self.client.get_account_states(list(self.watching))
        except Exception as e:
            log.debug(f"[ms] fetchAccountState 失败（不影响 live list 主源）: {e}")
            states = {}
        for aid, st in states.items():
            uuid_str = st.get("playing_uuid")
            if uuid_str:
                playing.setdefault(uuid_str, []).append(int(aid))

        now = time.time()
        for uuid_str, members in playing.items():
            game = self._games.get(uuid_str)
            if game is None:
                game = self._open_game(uuid_str, members, live.get(uuid_str))
                self._games[uuid_str] = game
                # 一发现就交给主程序挂观战流：终局靠流里的终局帧，不等牌谱
                self._announce_new(game, members)
            game["accounts"].update(members)
            game["last_seen"] = now
            game["gone"] = 0
            if not game["pushed"]:
                game["pushed"] = True
                self._push_start(game)

        for uuid_str, game in list(self._games.items()):
            if uuid_str in playing:
                continue
            game["gone"] += 1
            if game["gone"] < self.END_GRACE:
                continue
            if self.record_detail:
                # 掉出 100 行窗口和真散场在 live list 里一模一样，只有牌谱能分开两者
                head = self._try_record(uuid_str)
                if head is None:
                    continue
                self._games.pop(uuid_str, None)
                if game["pushed"]:
                    self._push_end(game, head)
            elif now - game["last_seen"] > self.END_UNCONFIRMED:
                self._games.pop(uuid_str, None)
                if game["pushed"]:
                    self._push_end(game, None)

        for uuid_str, game in list(self._games.items()):
            if now - game["start_ts"] > self.GAME_TTL:
                log.info(f"[ms] 对局 {uuid_str} 超时留档，丢弃")
                self._games.pop(uuid_str, None)

    def _live_by_uuid(self) -> dict:
        """live list 索引（uuid → 对局行）；拉不到就返回空表，本轮没有开局可检测。"""
        try:
            return {g["uuid"]: g for g in self.client.get_live_list(max_age=45)}
        except Exception as e:
            log.debug(f"[ms] live list 拉取失败: {e}")
            return {}

    def _open_game(self, uuid_str: str, members: list[int], lg: Optional[dict]) -> dict:
        players = list((lg or {}).get("players", []))
        field = (lg or {}).get("field") or "未知场"
        three = field.startswith("三") or len(players) == 3
        start_pt = {}
        for aid in members:
            try:
                info = self.client.lookup_player(account_id=aid)
            except Exception as e:
                log.debug(f"[ms] 段位分基线取不到 {aid}: {e}")
                info = {}
            if info:
                start_pt[aid] = info
        return {
            "uuid": uuid_str,
            "field": field,
            "mode": (lg or {}).get("mode") or 0,
            "players": players,
            "three": three,
            "accounts": set(members),
            # 开局时刻取 live list 的真实 start_time：发现可能比开局晚半分钟，
            # 用它算"用时"和列表里的开始时间才对得上。
            "start_ts": (lg or {}).get("start_time") or time.time(),
            "last_seen": time.time(),
            "gone": 0,
            "pushed": False,
            "start_pt": start_pt,
        }

    # ---- 推送 ----

    def _seats(self, game: dict) -> tuple[list, list]:
        """(这一场里被订阅的昵称, 同桌各家昵称)。"""
        players = game["players"]
        names = [p.get("nickname") or "?" for p in players]
        if not names:
            names = [self.watching.get(a, str(a)) for a in sorted(game["accounts"])]
        hits = [p.get("nickname") for p in players
                if p.get("account_id") in game["accounts"] and p.get("nickname")]
        if not hits:
            hits = [self.watching.get(a) for a in sorted(game["accounts"])
                    if self.watching.get(a)]
        return hits, names

    def _push_start(self, game: dict) -> None:
        hits, names = self._seats(game)
        mid = ms_short_id(game["uuid"])
        players = game.get("players") or []
        bits = []
        rates = []
        if len(names) == len(players) and players:
            three = game.get("three")
            if three is None:
                three = len(players) == 3
            # 每家一次 fetchAccountInfo + 一次 fetchAccountStatisticInfo：
            # 这线程本来就是轮询线程，慢一点不影响别处
            bits = self.client.seat_bits(players, bool(three))
            rates = self.client.seat_rates(players, game.get("mode"))
        self._notifier(room_start_text(mid, ms_field_label(game["field"]),
                                       names, now_hm(), hits, bits, rates),
                       set(game["accounts"]), game["field"], mid)

    def _try_record(self, uuid_str: str) -> Optional[dict]:
        """已结束对局的牌谱 head（含各家终局点数和段位分）；服务器还没给读就返回 None。"""
        if not self.record_detail:
            return None
        try:
            rec = self.client.get_game_record(uuid_str)
        except MSRpcError as e:
            if e.code in self.RECORD_WAIT_CODES:
                # "还不能读"（进行中 / 牌谱未生成），跟权限没关系：这场读不到，下场接着试
                self._record_fails = 0
            else:
                self._record_fails += 1
                if self._record_fails >= self.RECORD_FAIL_LIMIT:
                    self.record_detail = False
                    log.info(f"[ms] 牌谱接口连续 {self._record_fails} 次报错"
                             f"（{e}），改用段位分变化播报")
            return None
        except Exception as e:
            log.debug(f"[ms] 牌谱拉取失败: {e}")
            return None
        self._record_fails = 0
        head = rec.get("head") or {}
        if not head.get("players"):
            return None
        return head

    def _push_end(self, game: dict, head: Optional[dict] = None) -> None:
        mid = ms_short_id(game["uuid"])
        minutes = max(1, int((time.time() - game["start_ts"]) // 60))
        focus = self._seats(game)[0]
        if head is None:
            head = self._try_record(game["uuid"])
        if not head:
            notes = [f"注: 用时{minutes}分，这场没接入实时观战流，牌谱也还没可读",
                     "注: 下面只是段位分变化，服务端没给名次和点数"]
            notes += [self._pt_line(game, aid) for aid in sorted(game["accounts"])]
            self._notifier(finish_text(mid, [], focus, notes),
                           set(game["accounts"]), game["field"], mid)
            return
        has_pt = any(abs(float(p.get("grading_score") or 0.0)) > 1e-6
                     for p in head["players"])
        rows = rank_rows([(p.get("nickname") or "?",
                            int(p.get("final_point") or 0),
                            float(p.get("grading_score") or 0.0))
                          for p in head["players"]])
        notes = [f"注: 用时{minutes}分，名次和点数取自终局牌谱"
                 + ("" if has_pt else "（这场服务端没给段位分）")]
        self._notifier(finish_text(mid, rows, focus, notes),
                       set(game["accounts"]), game["field"], mid)

    def _pt_line(self, game: dict, aid: int) -> str:
        """牌谱/观战流都没交代名次时，用这一局前后的段位分变化来讲。"""
        before = game["start_pt"].get(aid) or {}
        nick = self.watching.get(aid) or before.get("nickname") or str(aid)
        try:
            after = self.client.lookup_player(account_id=aid)
        except Exception:
            after = {}
        if not before or not after:
            return f"{nick} 名次不可得（段位分查不到）"
        # live list 没命中时不知道三人/四人场，靠"哪条段位动了"反推，
        # 默认场名那一档排在前面。
        order = [("rank", "pt"), ("rank3", "pt3")]
        order.sort(key=lambda k: 0 if (k[0] == "rank3") == game["three"] else 1)
        field = None
        for rk, pk in order:
            if after.get(rk) != before.get(rk):
                field = (rk, pk)
                break
            b, a = before.get(pk), after.get(pk)
            if b is not None and a is not None and a != b:
                field = (rk, pk)
                break
        if field is None:
            field = ("rank3", "pt3") if game["three"] else ("rank", "pt")
        rk, pk = field
        if after.get(rk) != before.get(rk):
            return f"{nick} {before.get(rk)}→{after.get(rk)} {after.get(pk, 0)}"
        delta = (after.get(pk) or 0) - (before.get(pk) or 0)
        return f"{nick} {after.get(rk)} {after.get(pk, 0)} ({delta:+d})"
