"""
天凤 / 雀魂 云观战插件（纯 WebSocket 版）

指令格式参照 wg 云观战体系:

  wg              云观战
  wghelp          查看此帮助
  wgls            查看当前群订阅列表

  thadd <玩家名> 订阅天凤玩家→ 开局/散场自动播报（散场尽量带名次）
  thdel <玩家名> 删除订阅
  thfilter        筛选天凤场（如 鳳南 四般東）

  msadd <AID|昵称> 订阅雀魂（昵称自动反查AID，观战延迟约5分钟）
  msadd $<AID>    订阅雀魂AID（并非UID）
  msdel <AID|昵称> 删除雀魂订阅（昵称按本群已订阅的账号来对）
  msfilter        筛选雀魂场
  mspt <AID|昵称>  查询雀魂PT（未登录时仅支持昵称）

  wgstat          查看当前全部对局
  wg              列出已跟踪对局 + 订阅玩家正在打的场（天凤流延迟约5分钟）
  wg <四位码>     用推送里的 #四位码 开始观战播报，例: wg a1b2
  wg m<四位码>    雀魂那场的码带 m 前缀，例: wg m1a2b（雀魂码列表见 msls）
  wg <玩家名|WG码|大厅码>  同样可以指定要跟踪的天凤对局
  wg <雀魂UUID>   直接跟踪那场雀魂直播（四位码查不到时用它）
  msls [场名|比赛号] 列出玉之间/王座之间/比赛场的可跟踪对局（含四位码）
  wg- [WGID]      取消跟踪
  wgon/wgoff      启用/禁用推送
  wginfo [WGID]   查询当前比分

  wglog <码或链接>  查询天凤牌谱详情
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .tenhou_client import LogReplayParser
from .broadcast import now_hm, start_text
from .th_live import (
    ThGameSession,
    ThWatcher,
    end_render,
    fetch_games,
    format_players,
    game_type_name,
    is_finish_text,
    parse_lobby,
    short_id,
)
from .majsoul_client import (
    MahjongSoulClient,
    MahjongSoulWatcher,
    MS_LIVE_FID_FIELD,
    ms_field_label,
    ms_format_pt,
    ms_level_text,
    ms_short_id,
)
from .ms_live import MsGameSession


HELP_TEXT = """\
wg                       列出进行中对局
wg <四位码>              跟踪天凤对局
wg m<四位码>             跟踪雀魂对局
wg <WG码|大厅码|玩家名>  跟踪天凤对局
wg <雀魂UUID>            跟踪雀魂对局
wg- [码]                 取消跟踪
wgon / wgoff [0|1|2]     自动播报开关 1=天凤 2=雀魂 0=两者
wginfo [码]              查当前比分
wgstat                   查本群订阅与跟踪状态
wgls                     查订阅列表
wghelp                   查指令用法
wglog <牌谱码|链接>      查天凤牌谱

thadd <玩家名>           订阅天凤玩家
thdel <玩家名>           取消订阅天凤玩家
thfilter [场...]         只播报指定天凤场，all 清空

msadd <AID|昵称|$AID>    订阅雀魂玩家
msdel <AID|昵称|$AID>    取消订阅雀魂玩家
msfilter [场...]         只播报指定雀魂场，all 清空
mspt <AID|昵称|$AID>     查雀魂段位和 PT
msls [场名|比赛号]       列出雀魂可跟踪对局
"""


@register("astrbot_plugin_tenhou_watch", "SR3MP", "天凤/雀魂云观战(纯WS)", "3.13.2")
class TenhouWatch(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # 天凤: session_id → {WGID → 对局跟踪任务}（只有 wg 显式指定才会往里放）
        self._th_sessions: dict[str, dict[str, ThGameSession]] = {}
        # 四位码 → 对局 的反查靠这份全段位房扫描缓存（60 秒内复用）
        self._th_recent: Optional[list[dict]] = None
        self._th_scan_at = 0.0
        # 天凤订阅: session_id → {"players": set, "enabled": bool, "filters": list}
        self._subs: dict[str, dict] = {}
        # 已经自动播报过终局的 WG 码，防重复：session → {完整WG码: 时间}
        self._th_ends_seen: dict[str, dict[str, float]] = {}
        # 每场订阅对局的常驻牌流（不推局内消息，只为拿到终局名次和点位）
        self._th_auto: dict[str, ThGameSession] = {}
        # 天凤自动播报：订阅玩家开局/散场推一条，逐局仍只有 wg 那几场会播
        self._th_watcher = ThWatcher(self._th_notify, lobbies=self._TH_SCAN_LOBBIES,
                                     on_new_game=self._th_new_game)
        # 雀魂账号配置（来自插件配置 _conf_schema.json / WebUI）
        ms_cfg = (config or {}).get("majsoul") or {}
        self._ms_client = MahjongSoulClient(
            email=str(ms_cfg.get("email", "")),
            password=str(ms_cfg.get("password", "")),
            token=str(ms_cfg.get("token", "")),
        )
        poll = int(ms_cfg.get("poll_interval", 30) or 30)
        # 雀魂订阅: session_id → {"accounts": set, "enabled": bool, "filters": list}
        self._ms_subs: dict[str, dict] = {}
        self._ms_watcher = MahjongSoulWatcher(
            self._ms_client, self._ms_notify, poll_interval=poll,
            on_new_game=self._ms_new_game
        )
        # 雀魂观战: session_id → {四位码 → MsGameSession}（同样只有 wg 显式指定才跟踪）
        self._ms_sessions: dict[str, dict[str, MsGameSession]] = {}
        # 每场订阅对局的常驻观战流（局内消息一条不发，只为拿到终局名次）——和天凤同一套路
        self._ms_auto: dict[str, MsGameSession] = {}
        # 已经播过终局的四位码：session → {码: 时间}。订阅散场和 wg 会话会盯同一场，
        # 谁先播谁占坑，另一路补的那条直接丢掉。
        self._ms_ends_seen: dict[str, dict[str, float]] = {}
        self._ms_starts_seen: dict[str, dict[str, float]] = {}
        # 雀魂直播列表缓存（拉一次要发十几个请求，60 秒内复用）
        self._ms_live: Optional[list[dict]] = None
        self._ms_live_at = 0.0
        # 比赛场对局缓存（比赛列表 + 每场最近终局牌谱索引）
        self._ms_contest: Optional[list[dict]] = None
        self._ms_contest_at = 0.0
        # 主循环在 initialize() 里抓取后才能安全调度推送，这里先不起线程
        self._loop = None
        self._hydrate_task = None

        # 订阅/监听状态落盘（持久化），默认存插件目录下
        self._state_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sub_state.json"
        )
        self._load_state()

    def _normalize_sid(self, sid: str) -> str:
        """把旧版纯数字 session key 迁移为完整 unified_msg_origin。

        旧版本用 event.get_session_id()（纯群号）作 key，新版需要
        str(event.session)（形如 <平台ID>:GroupMessage:<群号>）才能
        匹配 context.send_message 的推送目标。
        """
        if ":" in sid:
            return sid
        platform_id = ""
        try:
            for p in self.context.platform_manager.platform_insts:
                if p.meta().id:
                    platform_id = p.meta().id
                    break
        except Exception:
            pass
        if not platform_id:
            return sid    # 没拿到平台实例，猜前缀只会迁移出一个对不上的 key
        return f"{platform_id}:GroupMessage:{sid}"

    async def initialize(self) -> None:
        """插件激活后调用：拉起雀魂/天凤轮询线程。"""
        # 监听线程要靠这个 loop 把推送调度回主循环；在别的线程里
        # get_event_loop() 会直接抛 RuntimeError，旧版就是这么把推送全吞掉的。
        self._loop = asyncio.get_running_loop()
        # 迁移旧版纯数字 session key 为完整 origin
        for key in list(self._subs.keys()):
            if ":" not in key:
                nk = self._normalize_sid(key)
                if nk != key:
                    self._subs[nk] = self._subs.pop(key)
        for key in list(self._ms_subs.keys()):
            if ":" not in key:
                nk = self._normalize_sid(key)
                if nk != key:
                    self._ms_subs[nk] = self._ms_subs.pop(key)
        self._save_state()
        # 把恢复的雀魂账号喂给轮询器，再拉起线程（线程一跑就会用到 self._loop）
        for sid, sub in self._ms_subs.items():
            for acc in sub.get("accounts", []):
                if int(acc) not in self._ms_watcher.watching:
                    self._ms_watcher.add(int(acc))
        self._ms_watcher.start()
        # 天凤：把各会话订阅的玩家名并成一个监听集合，开局/散场自动播报
        self._th_sync_watcher()
        self._th_watcher.start()
        # 旧状态里只存了账号数字，登录后把昵称补回来（推送显示用）
        self._hydrate_task = asyncio.create_task(self._hydrate_ms_names())
        logger.info(
            f"[tenhou] 初始化完成: 天凤订阅{len(self._subs)}个, "
            f"雀魂订阅{len(self._ms_subs)}个（开局/终局自动播，逐局播报要 wg）"
        )

    async def _hydrate_ms_names(self) -> None:
        """登录后按 AID 补齐订阅昵称并落盘。

        推送里只有数字账号很难认，旧状态文件又没存昵称。
        """
        missing = [
            aid for aid, nick in self._ms_watcher.watching.items()
            if nick == str(aid)
        ]
        if not missing:
            return
        ok = await asyncio.to_thread(self._ms_client.ensure_connection)
        if not ok:
            return
        for aid in missing:
            try:
                info = await asyncio.to_thread(self._ms_client.lookup_player, aid, None)
            except Exception as e:
                logger.warning(f"[ms] 补全昵称失败 AID={aid}: {e}")
                continue
            nick = (info or {}).get("nickname") or ""
            if nick:
                self._ms_watcher.add(aid, nick)
        self._save_state()

    # ==================================================================
    #  持久化: 订阅 + 监听状态
    # ==================================================================
    def _load_state(self) -> None:
        """启动时恢复各群订阅（天凤对局跟踪不恢复，需重新 wg）。"""
        try:
            if not os.path.exists(self._state_file):
                return
            with open(self._state_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logger.warning(f"[tenhou] 读取订阅状态失败: {e}")
            return

        subs = raw.get("th", {}) or {}
        ms_subs = raw.get("ms", {}) or {}
        ms_names = raw.get("ms_names", {}) or {}
        for sid, sub in subs.items():
            players = set(sub.get("players", []))
            if not players:
                continue
            self._subs[sid] = {
                "players": players,
                "enabled": bool(sub.get("enabled", True)),
                "filters": list(sub.get("filters", [])),
            }
        for sid, sub in ms_subs.items():
            accounts = set(sub.get("accounts", []))
            if not accounts:
                continue
            self._ms_subs[sid] = {
                "accounts": accounts,
                "enabled": bool(sub.get("enabled", True)),
                "filters": list(sub.get("filters", [])),
            }
        # 恢复昵称（只认已订阅的账号），否则重启后推送里只剩一串数字
        for sid, sub in ms_subs.items():
            for acc in sub.get("accounts", []):
                try:
                    aid = int(acc)
                except (TypeError, ValueError):
                    continue
                nick = ms_names.get(str(aid)) or ""
                if nick and nick != str(aid):
                    self._ms_watcher.add(aid, nick)

        logger.info(
            f"[tenhou] 已恢复订阅: 天凤{len(self._subs)}个会话, "
            f"雀魂{len(self._ms_subs)}个会话"
        )

    def _save_state(self) -> None:
        """把当前订阅写回磁盘。"""
        try:
            th = {}
            for sid, sub in self._subs.items():
                th[sid] = {
                    "players": sorted(sub.get("players", [])),
                    "enabled": bool(sub.get("enabled", True)),
                    "filters": list(sub.get("filters", [])),
                }
            ms = {}
            for sid, sub in self._ms_subs.items():
                ms[sid] = {
                    "accounts": sorted(sub.get("accounts", [])),
                    "enabled": bool(sub.get("enabled", True)),
                    "filters": list(sub.get("filters", [])),
                }
            # 昵称只在订阅时拿得到，重启后要靠它显示，所以一并落盘
            names = {
                str(aid): nick
                for aid, nick in self._ms_watcher.watching.items() if nick
            }
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"th": th, "ms": ms, "ms_names": names},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            logger.warning(f"[tenhou] 保存订阅状态失败: {e}")

    # ==================================================================
    #  天凤对局跟踪（只在 wg 显式指定后启动）
    # ==================================================================
    _TH_SCAN_LOBBIES = ("0", "1", "2", "3", "4")

    _TH_SEEN_TTL = 6 * 3600.0

    def _th_mark(self, store: dict, session: str, wg: str) -> None:
        """记下"这个群已经播过这场的开局/终局"，顺手清掉过期条目。"""
        wg = str(wg or "").upper()
        if not wg:
            return
        d = store.setdefault(session, {})
        now = time.time()
        for k in [k for k, ts in d.items() if now - ts > self._TH_SEEN_TTL]:
            d.pop(k, None)
        d[wg] = now

    def _th_marked(self, store: dict, session: str, wg: str) -> bool:
        wg = str(wg or "").upper()
        d = store.get(session) or {}
        return bool(wg) and time.time() - d.get(wg, 0) < self._TH_SEEN_TTL

    def _th_make_push(self, session: str, wg: str = ""):
        async def push(text: str) -> None:
            sub = self._subs.get(session)
            if sub and not sub.get("enabled", True):
                return
            # 终局只播一次：会话、自动跟踪、散场采样谁先到谁占这个坑
            if wg and is_finish_text(text):
                if self._th_marked(self._th_ends_seen, session, wg):
                    return
                self._th_mark(self._th_ends_seen, session, wg)
            await self._push(session, text)

        return push

    def _th_prune(self, session: str) -> None:
        """清掉已经结束/被取消的跟踪任务，避免字典里堆尸体。"""
        sess = self._th_sessions.get(session)
        if not sess:
            return
        for mid, s in list(sess.items()):
            if s.finished or s._task is None:
                sess.pop(mid, None)
        if not sess:
            self._th_sessions.pop(session, None)

    def _th_track(self, session: str, game: dict) -> ThGameSession:
        """开始跟踪一场对局，重复 wg 同一个码不会起两份。"""
        self._th_prune(session)
        sess = self._th_sessions.setdefault(session, {})
        mid = short_id(game["wg"])
        old = sess.get(mid)
        if old and old.running:
            return old
        s = ThGameSession(
            game["wg"],
            self._th_make_push(session, game["wg"]),
            names=game.get("names") or [],
            room=str(game.get("room", "") or ""),
            rule=str(game.get("rule", "") or ""),
            watchers=set((self._subs.get(session) or {}).get("players") or ()),
            # 段位房列表快照：区分局间空档和码失效，别一空就判终局
            listed=self._th_listed,
        )
        s.start()
        sess[mid] = s
        return s

    async def _th_listed(self, wg: str) -> bool:
        """这局还在不在段位房列表。

        观战循环本身不知道列表，没订阅 thadd 的群更是拿不到 watcher 的
        快照，所以快照查不到时再自己扫一遍段位房（一分钟一缓）。
        """
        if self._th_watcher.is_listed(wg):
            return True
        want = str(wg or "").upper()
        games = await self._th_recent_games()
        return any(str(g.get("wg", "")).upper() == want for g in games)

    _TH_MAX_AUTO = 8

    def _th_new_game(self, game: dict) -> None:
        """watcher 线程发现一场新的订阅对局：到主循环给它挂一条常驻牌流。"""
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._th_auto_track(game), loop)
        except Exception as e:
            logger.warning(f"[th] 自动观战挂不上: {e}")

    async def _th_auto_track(self, game: dict) -> None:
        """每场订阅对局都实际订一条牌流，终局的名次和点位全靠它。

        这条会话 push=None，局内消息一条不发；逐局播报仍然只认显式 wg。
        """
        wg = str(game.get("wg", "") or "").upper()
        if not wg:
            return
        for k, s in list(self._th_auto.items()):
            if s.finished or s._task is None:
                self._th_auto.pop(k, None)
        if wg in self._th_auto:
            return
        if len(self._th_auto) >= self._TH_MAX_AUTO:
            logger.info(f"[th] 自动观战已有 {len(self._th_auto)} 场，"
                        f"{short_id(wg)} 只用采样收终局")
            return
        s = ThGameSession(
            wg,
            push=None,
            names=game.get("names") or [],
            room=str(game.get("room", "") or ""),
            rule=str(game.get("rule", "") or ""),
            listed=self._th_listed,
            on_finish=self._th_auto_finish,
        )
        self._th_auto[wg] = s
        s.start()

    def _th_auto_finish(self, p: dict) -> None:
        """常驻牌流收了终局：按各群自己的订阅玩家渲染，一群一条，只播一次。"""
        wg = str(p.get("wg", "") or "").upper()
        self._th_auto.pop(wg, None)
        loop = self._loop
        if loop is None:
            return
        mid = short_id(wg)
        seats = {n for n in (p.get("names") or []) if n}
        rule_name = game_type_name(str(p.get("rule", "") or ""))
        for session, sub in list(self._subs.items()):
            if not sub.get("enabled", True):
                continue
            players = set(sub.get("players") or ())
            if not players or not (players & seats):
                continue
            filters = sub.get("filters") or []
            if filters and rule_name and not any(f in rule_name for f in filters):
                continue
            if self._th_marked(self._th_ends_seen, session, wg):
                continue
            self._th_mark(self._th_ends_seen, session, wg)
            try:
                asyncio.run_coroutine_threadsafe(
                    self._push(session, end_render(mid, p, players)), loop)
            except Exception as e:
                logger.warning(f"[th] 自动终局推送失败: {e}")

    def _th_auto_stop_all(self) -> None:
        """插件卸载时把常驻牌流收干净。"""
        for s in list(self._th_auto.values()):
            try:
                s.stop()
            except Exception:
                pass
        self._th_auto.clear()

    def _th_pick(self, sess: dict, key: str) -> str:
        """按四位码或完整 WG 码在已跟踪的对局里挑一场（大小写都认）。"""
        key = (key or "").strip()
        low, up = key.lower(), key.upper()
        return next(
            (m for m in sess if m == low or sess[m].wg_id.upper() == up), ""
        )

    def _th_rule_ok(self, session: str, rule: str) -> bool:
        filters = self._subs.get(session, {}).get("filters") or []
        if not filters:
            return True
        name = game_type_name(rule)
        return any(f in name for f in filters)

    def _th_scan(self, lobbies) -> list[dict]:
        """拉取若干大厅的进行中对局，单个大厅失败不影响其它（同步，放线程里跑）。"""
        games, seen = [], set()
        for lb in lobbies:
            try:
                rows = fetch_games(lb)
            except Exception as e:
                logger.warning(f"[th] 大厅 {lb} 列表拉取失败: {e}")
                continue
            for g in rows:
                if g["wg"] in seen:
                    continue
                seen.add(g["wg"])
                games.append(g)
        return games

    async def _th_recent_games(self, force: bool = False) -> list[dict]:
        """最近一次全段位房扫描的结果，一分钟内复用，避免每条指令都打一遍天凤。"""
        now = time.time()
        if force or now - self._th_scan_at > 60 or self._th_recent is None:
            self._th_recent = await asyncio.to_thread(
                self._th_scan, self._TH_SCAN_LOBBIES
            )
            self._th_scan_at = time.time()
        return self._th_recent

    async def _th_games_for_mid(self, code: str) -> list[dict]:
        """四位码 → 进行中的对局（撞码时可能返回多场，交给用户用完整 WG 码区分）。"""
        code = code.lower()
        hits = [g for g in await self._th_recent_games() if short_id(g["wg"]) == code]
        if not hits:
            hits = [
                g for g in await self._th_recent_games(force=True)
                if short_id(g["wg"]) == code
            ]
        return hits

    def _th_match(self, session: str, games: list[dict], name: str = "") -> list[dict]:
        """按订阅玩家（或指定名字）+ 场筛选挑出对局。"""
        players = {name} if name else (
            self._subs.get(session, {}).get("players") or set()
        )
        out = []
        for g in games:
            hit = next(
                (p for p in players if any(p in n or n in p for n in g["names"])), ""
            )
            if not hit:
                continue
            # 显式点名某局时不再叠加筛选，否则会把自己指定的场也挡掉
            if not name and not self._th_rule_ok(session, g.get("rule", "")):
                continue
            g["hit"] = hit
            out.append(g)
        return out

    def _th_find_by_wg(self, wg_id: str) -> Optional[dict]:
        """在扫描结果里按完整 WG 码定位对局，拿到选手和规则（拿不到返回 None）。"""
        want = wg_id.strip().upper()
        return next((g for g in (self._th_recent or []) if g["wg"].upper() == want), None)

    # ==================================================================
    #  通用订阅管理
    # ==================================================================
    def _get_sub(self, session: str) -> dict:
        if session not in self._subs:
            self._subs[session] = {"players": set(), "enabled": True, "filters": []}
        return self._subs[session]

    def _get_ms_sub(self, session: str) -> dict:
        if session not in self._ms_subs:
            self._ms_subs[session] = {
                "accounts": set(), "enabled": True, "filters": [],
            }
        return self._ms_subs[session]

    # ==================================================================
    #  推送
    # ==================================================================
    # context.send_message 找不到 session 对应的平台时不抛异常，只回 False 并打一条
    # Core WARN，消息就地丢掉。重启后平台适配器要几十秒才注册完，而雀魂轮询线程
    # 十几秒就发现对局了，开局那条正好落在这个空窗里（实测 14:42:49 丢过一条），
    # 所以拿到 False 就等一会儿重发，最多等 PUSH_TRIES * PUSH_RETRY_WAIT 秒。
    # 适配器正在加载时 send_message 直接抛异常（15:17:49 实测丢过一条，异常文本是空的，
    # 一秒后 aiocqhttp 才报「已连接」），异常和 False 同样处理：接着重发，别就地丢。
    PUSH_TRIES = 36
    PUSH_RETRY_WAIT = 5.0

    async def _push(self, session: str, text: str) -> None:
        err = ""
        for i in range(self.PUSH_TRIES):
            try:
                if await self.context.send_message(
                        session, MessageChain([Plain(text)])):
                    return
                err = "send_message 返回 False"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            if i == 0:
                logger.info(f"[push] 平台未就绪，稍后重发: {session}")
            await asyncio.sleep(self.PUSH_RETRY_WAIT)
        logger.warning(f"推送丢弃（{self.PUSH_TRIES} 次未发出）: {session} {err}")

    def _ms_notify(self, text: str, account_ids: set, field: str,
                   mid: str = "") -> None:
        """雀魂 watcher 的推送回调（在轮询线程里调用）。

        只推给真正订阅了这局选手、且筛选通过的会话。旧版广播给所有
        开启推送的群，一个群订阅的人开局，别的群也会收到。
        """
        loop = self._loop
        if loop is None:
            logger.warning("[ms] 主循环未就绪，丢弃推送")
            return
        for session, sub in self._ms_subs.items():
            if not sub.get("enabled", True):
                continue
            subscribed = set(sub.get("accounts", set()))
            if not subscribed:
                continue
            if account_ids and not (subscribed & account_ids):
                continue
            filters = sub.get("filters") or []
            if filters and not any(f in (field or "") for f in filters):
                continue
            if mid and is_finish_text(text):
                if self._ms_end_covered(session, mid):
                    continue
                self._ms_mark(self._ms_ends_seen, session, mid)
            elif mid and "【对局开始】" in text:
                if self._ms_marked(self._ms_starts_seen, session, mid):
                    continue
                self._ms_mark(self._ms_starts_seen, session, mid)
            try:
                asyncio.run_coroutine_threadsafe(self._push(session, text), loop)
            except Exception as e:
                logger.warning(f"雀魂推送失败: {e}")

    # ==================================================================
    #  天凤自动播报（订阅玩家开局/散场）
    # ==================================================================
    def _th_end_covered(self, session: str, wg: str) -> bool:
        """这场的终局已播过、或马上会由 wg 会话/常驻牌流播：别再补一条。"""
        if self._th_marked(self._th_ends_seen, session, wg):
            return True
        up = str(wg or "").upper()
        if any(s.wg_id.upper() == up
               for s in (self._th_sessions.get(session) or {}).values()):
            return True
        auto = self._th_auto.get(up)
        return bool(auto) and not auto.finished

    def _th_notify(self, text: str, names: set, rule_name: str = "",
                   wg: str = "") -> None:
        """ThWatcher 的推送回调（在轮询线程里调用）。

        只推给订阅了这场某个选手、且场筛选通过的会话。
        """
        loop = self._loop
        if loop is None:
            logger.warning("[th] 主循环未就绪，丢弃推送")
            return
        for session, sub in self._subs.items():
            if not sub.get("enabled", True):
                continue
            players = set(sub.get("players") or [])
            if not players or (names and not (players & names)):
                continue
            filters = sub.get("filters") or []
            if filters and rule_name and not any(f in rule_name for f in filters):
                continue
            if wg and is_finish_text(text):
                if self._th_end_covered(session, wg):
                    continue
                self._th_mark(self._th_ends_seen, session, wg)
            try:
                asyncio.run_coroutine_threadsafe(self._push(session, text), loop)
            except Exception as e:
                logger.warning(f"[th] 自动播报推送失败: {e}")

    def _th_sync_watcher(self) -> None:
        """把所有会话订阅的天凤玩家名并进 watcher 的监听集合。"""
        names = set()
        for sub in self._subs.values():
            names |= set(sub.get("players") or [])
        self._th_watcher.sync(names)

    # ==================================================================
    #  雀魂观战：跟踪管理
    # ==================================================================
    _MS_SEEN_TTL = 6 * 3600.0

    def _ms_seen(self, store: dict, session: str, mid: str,
                 mark: bool = False) -> bool:
        mid = str(mid or "").lower()
        if not mid:
            return False
        now = time.time()
        if not mark:
            return now - store.get(session, {}).get(mid, 0) < self._MS_SEEN_TTL
        d = store.setdefault(session, {})
        for k in [k for k, ts in d.items() if now - ts > self._MS_SEEN_TTL]:
            d.pop(k, None)
        d[mid] = now
        return True

    def _ms_mark(self, store: dict, session: str, mid: str) -> None:
        self._ms_seen(store, session, mid, mark=True)

    def _ms_marked(self, store: dict, session: str, mid: str) -> bool:
        return self._ms_seen(store, session, mid)

    def _ms_end_covered(self, session: str, mid: str) -> bool:
        """这场的终局已播过、或 wg 会话正盯着（它自己会播）：别再补一条。"""
        if self._ms_marked(self._ms_ends_seen, session, mid):
            return True
        s = (self._ms_sessions.get(session) or {}).get(str(mid or "").lower())
        return bool(s) and not s.finished

    def _ms_make_push(self, session: str, mid: str = ""):
        async def push(text: str) -> None:
            sub = self._ms_subs.get(session)
            if sub and not sub.get("enabled", True):
                return
            if mid:
                # 同一场的开局/终局，订阅散场和 wg 会话各播一次就算重了
                if is_finish_text(text):
                    if self._ms_marked(self._ms_ends_seen, session, mid):
                        return
                    self._ms_mark(self._ms_ends_seen, session, mid)
                elif "【对局开始】" in text:
                    if self._ms_marked(self._ms_starts_seen, session, mid):
                        return
                    self._ms_mark(self._ms_starts_seen, session, mid)
            await self._push(session, text)

        return push

    _MS_MAX_AUTO = 8

    def _ms_new_game(self, game: dict) -> None:
        """watcher 线程发现一场新的订阅对局：到主循环给它挂一条常驻观战流。"""
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._ms_auto_track(game), loop)
        except Exception as e:
            logger.warning(f"[ms] 自动观战挂不上: {e}")

    async def _ms_auto_track(self, game: dict) -> None:
        """每场订阅对局实际订一条观战流，终局名次靠流里的终局帧，不等牌谱。

        这条会话只把【对局完成】交回订阅那条推送，局内消息一条不发；
        逐局播报仍然只认显式 wg——和天凤 `_th_auto_track` 一模一样。
        """
        uuid_str = str(game.get("uuid", "") or "")
        if not uuid_str:
            return
        for k, s in list(self._ms_auto.items()):
            if s.finished:
                self._ms_auto.pop(k, None)
        if uuid_str in self._ms_auto:
            return
        # 群里已经有人 wg 过这场：那条会话本来就会播终局，别再开第二条流
        if any(s.uuid == uuid_str and not s.finished
               for sess in self._ms_sessions.values() for s in sess.values()):
            return
        if len(self._ms_auto) >= self._MS_MAX_AUTO:
            logger.info(f"[ms] 自动观战已有 {len(self._ms_auto)} 场，"
                        f"{ms_short_id(uuid_str)} 只用牌谱收终局")
            return
        acc = set(game.get("accounts") or ())
        s = MsGameSession(uuid_str,
                          self._ms_auto_push(uuid_str, acc,
                                             str(game.get("field") or ""),
                                             ms_short_id(uuid_str)),
                          self._ms_client, game,
                          watchers=self._ms_auto_focus(acc))
        self._ms_auto[uuid_str] = s
        s.start()

    def _ms_auto_focus(self, account_ids) -> set:
        """终局那句点评的主角：这局里被订阅的那几个人的昵称。"""
        return {self._ms_watcher.watching.get(a, "")
                for a in (account_ids or ())} - {""}

    def _ms_auto_push(self, uuid_str: str, account_ids, field: str, mid: str):
        """自动那条流的出口：只放行【对局完成】，播完让 watcher 别再等牌谱。"""
        async def push(text: str) -> None:
            if not is_finish_text(text):
                return
            self._ms_notify(text, account_ids, field, mid)
            self._ms_watcher.forget(uuid_str)

        return push

    def _ms_prune(self, session: str) -> None:
        sess = self._ms_sessions.get(session)
        if not sess:
            return
        for mid, s in list(sess.items()):
            if s.finished:
                sess.pop(mid, None)
        if not sess:
            self._ms_sessions.pop(session, None)

    def _ms_track(self, session: str, game: dict) -> MsGameSession:
        """开始跟踪一场雀魂对局（同一场重复 wg 不会起两份）。"""
        self._ms_prune(session)
        sess = self._ms_sessions.setdefault(session, {})
        mid = ms_short_id(game["uuid"])
        old = sess.get(mid)
        if old and old.running:
            return old
        # 自动那条流让位给这条：同一场不并两条连接
        auto = self._ms_auto.pop(str(game["uuid"]), None)
        if auto is not None and not auto.finished:
            auto.stop()
        s = MsGameSession(game["uuid"], self._ms_make_push(session, mid),
                          self._ms_client, game, watchers=self._ms_focus(session, game))
        sess[mid] = s
        s.start()
        return s

    def _ms_focus(self, session: str, game: dict) -> set:
        """这一场里被订阅的那些人的昵称：终局点评、开场白都靠它挑主角。"""
        acc = set()
        for a in ((self._ms_subs.get(session) or {}).get("accounts") or ()):
            try:
                acc.add(int(a))
            except (TypeError, ValueError):
                pass
        if not acc:
            return set()
        focus = {p.get("nickname", "") for p in (game.get("players") or [])
                 if int(p.get("account_id") or 0) in acc}
        focus |= {self._ms_watcher.watching.get(a, "") for a in acc}
        return {n for n in focus if n}

    async def _ms_live_games(self, force: bool = False) -> list[dict]:
        """段位场进行中对局列表，60 秒内复用缓存。"""
        now = time.time()
        if force or self._ms_live is None or now - self._ms_live_at > 60:
            try:
                self._ms_live = await asyncio.to_thread(self._ms_client.get_live_list)
            except Exception as e:
                logger.warning(f"[ms] 直播列表拉取失败: {e}")
                self._ms_live = self._ms_live or []
            self._ms_live_at = time.time()
        return list(self._ms_live or [])

    async def _ms_contest_games(self, force: bool = False, contest_id: int = 0,
                                unique_id: str = "") -> list[dict]:
        """比赛场对局：进行中的 + 每场最近终局的（终局那些只能走牌谱回放）。

        给了 contest_id / unique_id 就只查那一场比赛，不碰默认缓存。
        """
        keyed = bool(contest_id or unique_id)
        now = time.time()
        if keyed or force or self._ms_contest is None or now - self._ms_contest_at > 60:
            try:
                rows = await asyncio.to_thread(
                    lambda: self._ms_client.get_contest_rows(3, contest_id, unique_id))
            except Exception as e:
                logger.warning(f"[ms] 比赛场列表拉取失败: {e}")
                rows = []
            if keyed:
                return rows
            self._ms_contest = rows
            self._ms_contest_at = time.time()
        return list(self._ms_contest or [])

    async def _ms_all_games(self, force: bool = False) -> list[dict]:
        """段位场 + 比赛场，wg <四位码> 就是在这份合并列表里找。"""
        games = await self._ms_live_games(force)
        games += await self._ms_contest_games(force)
        return games

    async def _ms_games_for_mid(self, code: str) -> list[dict]:
        code = (code or "").lower()
        hits = [g for g in await self._ms_all_games()
                if ms_short_id(g["uuid"]).lower() == code]
        if not hits:
            hits = [g for g in await self._ms_all_games(force=True)
                    if ms_short_id(g["uuid"]).lower() == code]
        if not hits:
            # live list 只留 100 行，开了四分钟就查不到；订阅那边还在局里的场
            # 照样能观战，别因为列表里没有就拒了 wg。
            tracked = self._ms_watcher.tracked_game(code)
            if tracked:
                hits = [tracked]
        return hits

    async def _ms_games_for_uuid(self, uuid_str: str) -> list[dict]:
        want = uuid_str.strip().lower()
        hits = [g for g in await self._ms_all_games(force=True)
                if g["uuid"].lower() == want]
        if hits:
            return hits
        # 列表里没有也照单跟踪：牌谱回放只认 uuid
        return [{"uuid": want, "field": "未知场", "players": [], "start_time": 0}]

    def _ms_hit_lines(self, games: list[dict], limit: int = 12) -> list[str]:
        out = []
        for g in games[:limit]:
            players = " ".join(
                f"{p.get('nickname', '')}({p.get('rank', '')})"
                for p in (g.get("players") or []) if p.get("nickname"))
            if g.get("live") is False:
                when = (time.strftime("%m-%d %H:%M", time.localtime(g["end_time"]))
                        if g.get("end_time") else "?")
                state = f"终局于{when}，回放牌谱"
            else:
                when = (time.strftime("%H:%M", time.localtime(g["start_time"]))
                        if g.get("start_time") else "?")
                state = f"始于{when}"
            out.append(f"  #{ms_short_id(g['uuid'])} {g.get('field', '')} {state} {players}")
        if len(games) > limit:
            out.append(f"  …（共 {len(games)} 场）")
        return out

    # ==================================================================
    #  指令: wg / wghelp
    # ==================================================================
    @filter.command("wghelp")
    async def wghelp(self, event: AstrMessageEvent):
        """wghelp：打印全部指令的用法清单。"""
        yield event.make_result().message(HELP_TEXT)

    # ==================================================================
    #  指令: wgls
    # ==================================================================
    @filter.command("wgls")
    async def wgls(self, event: AstrMessageEvent):
        """wgls：查看本群订阅的天凤玩家和雀魂账号，以及各自的场筛选。"""
        session = str(event.session)
        sub = self._get_sub(session)
        ms_sub = self._get_ms_sub(session)

        th_players = sub["players"]
        ms_accounts = ms_sub["accounts"]
        ms_watching = "运行中" if self._ms_watcher.running else "停止"

        lines = [f"天凤订阅 ({len(th_players)}):"]
        if th_players:
            lines += [f"  - {p}" for p in sorted(th_players)]
        else:
            lines.append("  - 空")
        f = sub.get("filters") or []
        lines.append(f"天凤筛选: {', '.join(f) if f else '不限'}")
        lines.append(f"天凤推送: {'开启' if sub['enabled'] else '关闭'}")
        lines.append("天凤监控: " + ("运行中" if self._th_watcher.running else "停止"))
        lines.append("")
        lines.append(f"雀魂订阅 ({len(ms_accounts)}):")
        if ms_accounts:
            watched = self._ms_watcher.watching
            lines += [f"  - {watched.get(a) or a} ({a})" for a in sorted(ms_accounts)]
            f = ms_sub.get("filters") or []
            lines.append(f"雀魂筛选: {', '.join(f) if f else '不限'}")
        else:
            lines.append("  - 空")
            lines.append("雀魂筛选: 不限")
        lines.append(f"雀魂推送: {'开启' if ms_sub['enabled'] else '关闭'}")
        lines.append(f"雀魂监控: {ms_watching}")
        if not self._ms_client.authed:
            lines.append("（雀魂未登录：仅能按昵称查询牌谱屋，无法监控对局。请在插件配置中填写账号）")

        yield event.make_result().message("\n".join(lines))

    # ==================================================================
    #  指令: thadd / thdel / thfilter
    # ==================================================================
    @filter.command("thadd")
    async def thadd(self, event: AstrMessageEvent, player_id: str = ""):
        """thadd <玩家名>：订阅天凤玩家，他开局/散场时本群自动播一条。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield event.make_result().message("用法: thadd <玩家名>\n示例: thadd 张三")
            return
        session = str(event.session)
        sub = self._get_sub(session)
        sub["players"].add(player_id)
        self._th_sync_watcher()
        self._save_state()
        yield event.make_result().message(f"已订阅天凤ID: {player_id}")

    @filter.command("thdel")
    async def thdel(self, event: AstrMessageEvent, player_id: str = ""):
        """thdel <玩家名>：取消订阅这个天凤玩家。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield event.make_result().message("用法: thdel <玩家名>")
            return
        session = str(event.session)
        sub = self._get_sub(session)
        if player_id in sub["players"]:
            sub["players"].discard(player_id)
            self._th_sync_watcher()
            self._save_state()
            yield event.make_result().message(f"已删除订阅: {player_id}")
        else:
            yield event.make_result().message(f"未找到订阅: {player_id}")

    @filter.command("thfilter")
    async def thfilter(self, event: AstrMessageEvent, rest: GreedyStr):
        """thfilter <场名关键字>…：只自动播报场名匹配这些关键字的天凤对局；不带参数查看，all 清空。"""
        args = rest.split()
        session = str(event.session)
        sub = self._get_sub(session)
        if not args:
            current = ", ".join(sub["filters"]) if sub["filters"] else "不限"
            yield event.make_result().message(
                f"当前天凤筛选: {current}\n"
                "用法: thfilter <场名关键字> [关键字...]\n"
                "按场名子串匹配，如: thfilter 鳳南 鳳東\n"
                "清空: thfilter all|不限|无"
            )
            return
        if args[0].lower() in ("all", "不限", "无"):
            sub["filters"] = []
            self._save_state()
            yield event.make_result().message("天凤筛选已清空。")
            return
        sub["filters"] = list(args)
        self._save_state()
        yield event.make_result().message(f"天凤筛选已设为: {', '.join(args)}")

    # ==================================================================
    #  指令: msadd / msdel / msfilter / mspt
    # ==================================================================
    @filter.command("msadd")
    async def msadd(self, event: AstrMessageEvent, player_id: str = ""):
        """msadd <AID|昵称> 或 msadd $<AID>：订阅雀魂玩家，他开局/散场时本群自动播一条。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield event.make_result().message(
                "用法: msadd <AID> / $<AID> / <昵称>\n"
                "示例: msadd 12345678 / msadd $123456 / msadd 张三"
            )
            return
        session = str(event.session)
        ms_sub = self._get_ms_sub(session)
        # 客户端全是同步阻塞的 WS/HTTP 调用，放到线程里跑，别卡住整个 bot
        logged_in = await asyncio.to_thread(self._ms_client.ensure_connection)

        # 解析 $<AID>、<AID> 或 <昵称>（未登录时昵称走牌谱屋反查）
        raw = player_id.lstrip("$")
        account_id = None
        nickname = ""
        if raw.isdigit():
            account_id = int(raw)
        else:
            # 昵称 → 国服 aid：牌谱屋反查，重名时列候选
            if logged_in:
                # 已登录：走官方 RPC 搜索
                try:
                    found = await asyncio.to_thread(
                        self._ms_client.lookup_player, None, raw)
                    if not found.get("account_id"):
                        yield event.make_result().message(f"未找到玩家: {player_id}")
                        return
                    account_id = found["account_id"]
                    nickname = found.get("nickname", raw)
                except Exception as e:
                    yield event.make_result().message(f"雀魂查询失败: {e}")
                    return
            else:
                cands = await asyncio.to_thread(
                    self._ms_client.koromo_search_candidates, raw)
                if len(cands) == 1:
                    account_id = cands[0]["account_id"]
                    nickname = cands[0]["nickname"]
                elif len(cands) > 1:
                    lines = [f"昵称「{raw}」命中 {len(cands)} 个账号（牌谱屋无法区分服务器，可能含非国服）:"]
                    for i, c in enumerate(cands, 1):
                        lvl = ms_level_text(c["rank"], c["pt"], c["max_pt"]) or c["rank"]
                        lines.append(f"{i}. {c['nickname']} | {lvl} | AID {c['account_id']}")
                    lines.append("请用 msadd $<AID> 订阅指定账号。")
                    yield event.make_result().message("\n".join(lines))
                    return
                else:
                    yield event.make_result().message(f"未找到玩家: {player_id}")
                    return

        # 已登录且还没拿到昵称，尝试补全（用于显示）
        if logged_in and not nickname:
            try:
                info = await asyncio.to_thread(
                    self._ms_client.lookup_player, account_id, None)
                nickname = info.get("nickname", "")
            except Exception as e:
                yield event.make_result().message(f"雀魂查询失败: {e}")
                return

        ms_sub["accounts"].add(account_id)
        self._ms_watcher.add(account_id, nickname)
        self._save_state()

        label = f"{account_id}" + (f"（{nickname}）" if nickname else "")
        yield event.make_result().message(f"已订阅雀魂AID: {label}")

    @filter.command("msdel")
    async def msdel(self, event: AstrMessageEvent, player_id: str = ""):
        """msdel <AID|昵称> 或 $<AID>：取消订阅这个雀魂玩家（重名时需用 $<AID> 指定）。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield event.make_result().message(
                "用法: msdel <AID> / $<AID> / <昵称>\n"
                "示例: msdel 12345678 / msdel $123456 / msdel 张三"
            )
            return
        session = str(event.session)
        ms_sub = self._get_ms_sub(session)
        raw = player_id.lstrip("$")
        account_id = None
        if raw.isdigit():
            account_id = int(raw)
        else:
            # 昵称先按本群已订阅的账号来对（监控表里就有，不用联网，也不会误删别人的号）
            hits = [a for a in ms_sub["accounts"]
                    if self._ms_watcher.watching.get(a) == raw]
            if len(hits) > 1:
                yield event.make_result().message(
                    f"昵称「{raw}」在本群订阅里重名: "
                    + " ".join(f"${a}" for a in sorted(hits))
                    + "\n请用 msdel $<AID> 指定要删除的账号。"
                )
                return
            if hits:
                account_id = hits[0]
            else:
                # 订阅时记的是 AID、没记昵称的情况：按 msadd 那套反查一次
                found = await asyncio.to_thread(self._ms_client.lookup_player,
                                                None, raw)
                account_id = found.get("account_id")
                if not account_id:
                    yield event.make_result().message(f"未找到雀魂订阅: {player_id}")
                    return
        if account_id in ms_sub["accounts"]:
            label = self._ms_watcher.watching.get(account_id) or str(account_id)
            ms_sub["accounts"].discard(account_id)
            still = any(account_id in s.get("accounts", set())
                        for s in self._ms_subs.values())
            if not still:
                # 别的群还订着同一个账号时不能停监控
                self._ms_watcher.remove(account_id)
            self._save_state()
            yield event.make_result().message(f"已删除雀魂订阅: {label}")
        else:
            yield event.make_result().message(
                f"未找到雀魂订阅: {self._ms_watcher.watching.get(account_id) or account_id}")

    @filter.command("msfilter")
    async def msfilter(self, event: AstrMessageEvent, rest: GreedyStr):
        """msfilter <场名关键字>…：只自动播报场名匹配这些关键字的雀魂对局；不带参数查看，all/不限 清空。"""
        args = rest.split()
        session = str(event.session)
        ms_sub = self._get_ms_sub(session)
        if not args:
            current = ", ".join(ms_sub["filters"]) if ms_sub["filters"] else "不限"
            yield event.make_result().message(
                f"当前雀魂筛选: {current}\n"
                "用法: msfilter <场名关键字> [关键字...]\n"
                "按场名子串匹配，如: msfilter 金 玉 王座\n"
                "清空: msfilter all|不限"
            )
            return
        if [a.lower() for a in args] == ["all"] or list(args) == ["不限"]:
            ms_sub["filters"] = []
            self._save_state()
            yield event.make_result().message("雀魂筛选已清空。")
            return
        ms_sub["filters"] = list(args)
        self._save_state()
        yield event.make_result().message(f"雀魂筛选已设为: {', '.join(args)}")

    @filter.command("mspt")
    async def mspt(self, event: AstrMessageEvent, player_id: str = ""):
        """mspt <AID|昵称> 或 $<AID>：查雀魂段位和 PT；未登录时按昵称走牌谱屋，重名会列候选。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield event.make_result().message(
                "用法: mspt <AID> / $<AID> / <昵称>\n"
                "示例: mspt 12345678 / mspt $123456 / mspt 张三"
            )
            return
        raw = player_id.lstrip("$")
        if raw.isdigit():
            account_id = int(raw)
        else:
            account_id = None
        logged_in = await asyncio.to_thread(self._ms_client.ensure_connection)
        try:
            # 未登录 + 昵称：先走牌谱屋候选，重名时让用户挑，避免误取非国服账号
            if account_id is None and not logged_in:
                cands = await asyncio.to_thread(
                    self._ms_client.koromo_search_candidates, raw)
                if len(cands) == 1:
                    yield event.make_result().message(ms_format_pt(cands[0]))
                    return
                if len(cands) > 1:
                    lines = [f"昵称「{raw}」命中 {len(cands)} 个账号（牌谱屋无法区分服务器，可能含非国服）:"]
                    for i, c in enumerate(cands, 1):
                        lvl = ms_level_text(c["rank"], c["pt"], c["max_pt"]) or c["rank"]
                        row = f"{i}. {c['nickname']} | {lvl}"
                        if c.get("rank3"):
                            lvl3 = ms_level_text(c["rank3"], c["pt3"], c["max_pt3"]) or c["rank3"]
                            row += f" | 三人 {lvl3}"
                        lines.append(row + f" | AID {c['account_id']}")
                    lines.append("请用 mspt <AID> 查询指定账号，或 msadd $<AID> 直接订阅。")
                    yield event.make_result().message("\n".join(lines))
                    return
                # 0 条：继续走 lookup_player 兜底（可能命中观战列表）

            stats = await asyncio.to_thread(
                self._ms_client.lookup_player, account_id, raw)
            if not stats:
                if account_id is not None and not logged_in:
                    yield event.make_result().message(
                        "未登录时无法按 AID 查询。\n"
                        "请用昵称查询: mspt <昵称>"
                    )
                    return
                yield event.make_result().message(f"未找到玩家: {player_id}")
                return
            yield event.make_result().message(ms_format_pt(stats))
        except Exception as e:
            yield event.make_result().message(f"雀魂查询失败: {e}")

    # ==================================================================
    #  指令: wgstat
    # ==================================================================
    @filter.command("wgstat")
    async def wgstat(self, event: AstrMessageEvent):
        """wgstat：汇总本群的天凤/雀魂跟踪、订阅、推送开关、登录态和进行中对局。"""
        session = str(event.session)
        self._th_prune(session)
        tracked = self._th_sessions.get(session) or {}
        sub = self._get_sub(session)
        ms_sub = self._get_ms_sub(session)

        lines = []
        self._ms_prune(session)
        ms_tracked = self._ms_sessions.get(session) or {}
        if tracked:
            lines.append(f"天凤跟踪 {len(tracked)} 场:")
            for s in tracked.values():
                lines.append(f"  {s.brief()}")
        else:
            lines.append("天凤跟踪: 无")
        lines.append(f"天凤订阅: {len(sub['players'])} 人")
        f = sub.get("filters") or []
        lines.append(f"天凤筛选: {', '.join(f) if f else '不限'}")
        lines.append(f"天凤推送: {'开启' if sub['enabled'] else '关闭'}")
        lines.append(f"天凤监控: {'运行中' if self._th_watcher.running else '停止'}")
        th_auto = [g for g in self._th_watcher.snapshot()
                   if not sub.get("players") or (set(g["hits"]) & sub["players"])]
        lines.append(f"天凤自动观战流: {len(self._th_auto)}/{self._TH_MAX_AUTO} 场")
        if th_auto:
            lines.append(f"天凤进行中对局 {len(th_auto)} 场:")
            for g in th_auto:
                lines.append(f"  #{g['mid']} {g['rule']} 始于{g['since']} {g['players']}")
        else:
            lines.append("天凤进行中对局: 无")
        lines.append("")
        if ms_tracked:
            lines.append(f"雀魂跟踪 {len(ms_tracked)} 场:")
            for s in ms_tracked.values():
                lines.append(f"  {s.brief()}")
        else:
            lines.append("雀魂跟踪: 无")
        lines.append(f"雀魂订阅: {len(ms_sub['accounts'])} 人")
        ms_f = ms_sub.get("filters") or []
        lines.append(f"雀魂筛选: {', '.join(ms_f) if ms_f else '不限'}")
        lines.append(f"雀魂推送: {'开启' if ms_sub['enabled'] else '关闭'}")
        if self._ms_client.authed:
            lines.append("雀魂登录: 已认证")
        else:
            err = self._ms_client.last_error or "未连接"
            lines.append(f"雀魂登录: 未认证（{err}）")
        lines.append(f"雀魂监控: {'运行中' if self._ms_watcher.running else '停止'}")
        ms_auto = [k for k, sess in self._ms_auto.items() if not sess.finished]
        lines.append(f"雀魂自动观战流: {len(ms_auto)}/{self._MS_MAX_AUTO} 场")
        games = self._ms_watcher.snapshot()
        if games:
            lines.append(f"雀魂进行中对局 {len(games)} 场:")
            for g in games:
                lines.append(f"  #{g['mid']} {g['field']} 始于{g['since']} {g['players']}")
        else:
            lines.append("雀魂进行中对局: 无")
        if not self._ms_watcher.record_detail:
            lines.append("雀魂牌谱权限: 不可用")

        yield event.make_result().message("\n".join(lines))

    # ==================================================================
    #  指令: wg <WGID> / wg- / wginfo / wglog
    # ==================================================================
    @staticmethod
    def _watch_note(platform: str, mid: str) -> str:
        """wg 回执的最后那行：延迟多久 + 怎么取消，天凤和雀魂同一句。"""
        return (f"已开启{platform}观战播报，延迟约 5 分钟。"
                f"取消: /wg- {mid}")

    def _th_start_text(self, game: Optional[dict], wg_id: str, focus=()) -> str:
        """【对局开始】回执：只报场名和链接，四家不再重播；列表查不到该场时场名也省成 `天凤`。

        names 只用来在订阅玩家里找主角。
        """
        mid = short_id(wg_id)
        link = f"https://tenhou.net/3/?wg={wg_id}"
        names = [n for n in ((game or {}).get("names") or []) if n]
        rule = str((game or {}).get("rule", "") or "")
        label = f"天凤 {game_type_name(rule)}" if names else "天凤"
        return start_text(mid, label, names, now_hm(), focus, [link], seats=False)

    def _th_hit_lines(self, hits: list[dict]) -> list[str]:
        """列表行：四位码就是发给 wg 的凭证，完整 WG 码只在撞码时才露出。"""
        out = []
        for g in hits[:10]:
            out.append(
                f"  wg {short_id(g['wg'])} | {game_type_name(str(g.get('rule', '')))} | "
                f"{format_players(g)}"
            )
        if len(hits) > 10:
            out.append(f"  …（共 {len(hits)} 场）")
        return out

    async def _wg_begin(self, session: str, game: Optional[dict], wg_id: str = ""):
        """真正开跟踪；回执就是那条【对局开始】，和雀魂同一个骨架。"""
        wg = (wg_id or (game or {}).get("wg", "")).strip().upper()
        if not wg:
            yield "未定位到天凤对局，未开始跟踪。可用 wg 列出可跟踪对局。"
            return
        mid = short_id(wg)
        if mid in (self._th_sessions.get(session) or {}):
            yield f"#{mid} " + self._watch_note("天凤", mid)
            return
        self._th_track(session, {"wg": wg, **(game or {})})
        focus = set((self._subs.get(session) or {}).get("players") or ())
        yield self._th_start_text(game, wg, focus) + "\n" + self._watch_note("天凤", mid)

    async def _ms_begin(self, session: str, hits: list[dict], code: str = ""):
        """开始雀魂那场的跟踪；回执就是那条【对局开始】，和天凤同一个骨架。"""
        if not hits:
            yield "未定位到雀魂对局，未开始跟踪。可用 msls 列出可跟踪对局。"
            return
        if len(hits) > 1:
            who = f"四位码 {code} " if code else "四位码"
            yield (f"{who}对应 {len(hits)} 场对局，未开始跟踪。改用完整 uuid:\n"
                   + "\n".join(f"  wg {g['uuid']}" for g in hits))
            return
        game = hits[0]
        mid = ms_short_id(game["uuid"])
        self._ms_prune(session)
        if mid in (self._ms_sessions.get(session) or {}):
            yield f"#{mid} " + self._watch_note("雀魂", mid)
            return
        if game.get("live") is False:
            # 牌谱索引里的场：早就终局了，没有逐局可播，会话只会去回放
            yield f"#{mid} 对局已结束，逐局结算与终局名次将在牌谱生成后推送。"
        else:
            players = game.get("players") or []
            names = [p.get("nickname", "") for p in players]
            yield (start_text(mid, ms_field_label(game.get("field") or ""),
                              names, now_hm(), self._ms_focus(session, game),
                              (), seats=False)
                   + "\n" + self._watch_note("雀魂", mid))
        self._ms_track(session, game)

    @filter.command("wg")
    async def wg_track(self, event: AstrMessageEvent, arg: str = ""):
        """wg <四位码|8位WG码|大厅码|玩家名|雀魂UUID>：开始逐局播报那场（天凤码 4 位、雀魂码带 m 前缀）；不带参数列已跟踪的对局和订阅玩家正在打的场。"""
        arg = (arg or "").strip()
        session = str(event.session)
        self._th_prune(session)
        tracked = self._th_sessions.get(session) or {}

        if not arg:
            lines = ["已跟踪的对局:"]
            ms_tracked = self._ms_sessions.get(session) or {}
            if tracked or ms_tracked:
                lines += [f"  {s.brief()}" for s in tracked.values()]
                lines += [f"  {s.brief()}" for s in ms_tracked.values()]
            else:
                lines.append("  无")
            players = sorted(self._get_sub(session)["players"])
            if players:
                lines.append("")
                lines.append("订阅玩家正在进行的对局:")
                hits = self._th_match(session, await self._th_recent_games())
                if hits:
                    lines += self._th_hit_lines(hits)
                else:
                    lines.append(
                        f"  无（订阅: {' '.join(players)}，仅查段位房且受筛选限制）"
                    )
            yield event.make_result().message("\n".join(lines))
            return

        # 大厅码有两种写法：L0000 / C0000xxxx 这种带字母的，和 1~5 位数字房号。
        # 纯数字 4 位既可能是房号也可能是四位对局码，先按对局码查，查不到再当房号。
        is_lobby_word = bool(
            re.fullmatch(r"L\d{1,4}", arg, re.I)
            or re.fullmatch(r"C\d{8}", arg, re.I)
        )
        is_lobby_num = arg.isdigit() and len(arg) <= 5
        is_short = bool(re.fullmatch(r"[0-9a-fA-F]{4}", arg))
        # 雀魂码带 m 前缀（见 ms_short_id）：码的形状就说明 wg 哪一边，不用两头查
        is_ms_short = bool(re.fullmatch(r"[mM][0-9a-fA-F]{4}", arg))
        is_token = bool(re.fullmatch(r"[0-9a-fA-F]{8}", arg)) \
            and not (is_lobby_word or is_lobby_num)

        if is_ms_short:
            async for text in self._ms_begin(
                    session, await self._ms_games_for_mid(arg.lower()), arg.lower()):
                yield event.make_result().message(text)
            return

        hits = await self._th_games_for_mid(arg.lower()) if is_short else []

        if is_short and hits:
            if len(hits) > 1:
                yield event.make_result().message(
                    f"四位码 {arg.lower()} 对应 {len(hits)} 场对局，未开始跟踪。"
                    "改用完整 WG 码:\n"
                    + "\n".join(f"  wg {g['wg']}" for g in hits)
                )
                return
            async for text in self._wg_begin(session, hits[0]):
                yield event.make_result().message(text)
            return

        if is_short and not (is_lobby_word or is_lobby_num):
            yield event.make_result().message(
                "未定位到天凤对局，未开始跟踪。可用 wg 列出可跟踪对局。"
            )
            return

        # 雀魂完整 uuid（250927-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx）
        if re.fullmatch(r"\d{6}-[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", arg):
            async for text in self._ms_begin(session, await self._ms_games_for_uuid(arg)):
                yield event.make_result().message(text)
            return

        if is_token:
            wg_id = arg.upper()
            await self._th_recent_games()
            game = self._th_find_by_wg(wg_id)
            async for text in self._wg_begin(session, game, wg_id):
                yield event.make_result().message(text)
            return

        if is_lobby_word or is_lobby_num:
            lb = parse_lobby(arg)
            games = await asyncio.to_thread(self._th_scan, [lb])
            if not games:
                # C 码带的规则后缀（如 4237）不是列表文件名，扫全段位房更实在
                games = await self._th_recent_games(force=True)
            hits = self._th_match(session, games)
            if not hits:
                yield event.make_result().message(
                    f"大厅 {lb} 没有匹配的进行中对局。只匹配订阅玩家且通过筛选的对局。\n"
                    "可用 wg <四位码> 指定，或 thadd <玩家名> 订阅玩家。"
                )
                return
            if len(hits) > 1:
                yield event.make_result().message(
                    f"大厅 {lb} 命中 {len(hits)} 场，请指定其一:\n"
                    + "\n".join(self._th_hit_lines(hits))
                )
                return
            async for text in self._wg_begin(session, hits[0]):
                yield event.make_result().message(text)
            return

        # 玩家名
        hits = self._th_match(session, await self._th_recent_games(), name=arg)
        if not hits:
            yield event.make_result().message(
                f"未找到「{arg}」正在进行的对局。天凤昵称区分大小写。\n"
                "也可用 wg <四位码|8位WG码|大厅码> 指定。"
            )
            return
        if len(hits) > 1:
            yield event.make_result().message(
                f"「{arg}」有 {len(hits)} 场进行中，请指定其一:\n"
                + "\n".join(self._th_hit_lines(hits))
            )
            return
        async for text in self._wg_begin(session, hits[0]):
            yield event.make_result().message(text)

    @filter.command("msls")
    async def ms_live_list(self, event: AstrMessageEvent, arg: str = ""):
        """列出可跟踪的雀魂对局：玉之间、王座之间、比赛场（含四位码）。

        带比赛号（纯数字）时只列那场比赛的进行中对局与最新牌谱。
        """
        arg = (arg or "").strip()
        by_id = arg.isdigit()
        ranked = await self._ms_live_games(force=True)
        if by_id:
            contest = await self._ms_contest_games(contest_id=int(arg))
            if not contest:
                contest = await self._ms_contest_games(unique_id=arg)
            if not contest:
                yield event.make_result().message(
                    f"比赛号 {arg} 未查到对局。比赛号是客户端里的数字 ID，"
                    "且只列本账号参加过的比赛。")
                return
        else:
            contest = await self._ms_contest_games(force=True)
        if not ranked and not contest:
            err = self._ms_client.last_error or "未登录/列表为空"
            yield event.make_result().message(
                f"未拉到雀魂直播列表（{err}）。\n"
                "订阅的玩家开局仍会自动播报。"
            )
            return
        session = str(event.session)
        sub = self._get_ms_sub(session)
        accounts = set(sub.get("accounts") or [])
        filters = [] if by_id else ([arg] if arg else (sub.get("filters") or []))
        # 已终局的行按终局时间排（服务端那个顺序才是"最新几场"的意思）
        newest = lambda g: -int(g.get("end_time") or g.get("start_time") or 0)

        mine = [g for g in ranked
                if any(p.get("account_id") in accounts for p in (g.get("players") or []))]
        mine.sort(key=newest)

        def field_of(g) -> str:
            return g.get("field") or ""

        def in_room(g, room: str) -> bool:
            """这场是不是某个段位组：有请求参数 filter_id 就按它，没拉到才看场名后缀。"""
            by_fid = MS_LIVE_FID_FIELD.get(int(g.get("filter_id") or 0))
            return by_fid == room if by_fid else field_of(g).endswith(room)

        groups: list[tuple[str, list[dict], int]] = []
        if by_id:
            groups.append(("比赛场", sorted(contest, key=newest), 12))
        elif filters:
            groups.append(("/".join(filters),
                           sorted([g for g in ranked
                                   if any(f in field_of(g) for f in filters)], key=newest), 8))
            groups.append(("比赛场", sorted(contest, key=newest), 5))
        else:
            groups.append(("玉之间",
                           sorted([g for g in ranked if in_room(g, "玉")],
                                  key=newest), 6))
            groups.append(("王座之间",
                           sorted([g for g in ranked if in_room(g, "王座")],
                                  key=newest), 4))
            groups.append(("比赛场", sorted(contest, key=newest), 5))

        lines = []
        if mine and not by_id:
            lines.append(f"订阅玩家进行中的对局 ({len(mine)}):")
            lines += self._ms_hit_lines(mine, 4)
        for name, games, limit in groups:
            lines.append(f"{name} ({len(games)}):")
            full = next((g.get("contest_full") for g in games if g.get("contest_full")), "")
            if full:
                cid = next((g.get("contest_id") for g in games if g.get("contest_id")), 0)
                lines.append(f"  {full[:44]}" + (f"（比赛号 {cid}）" if cid else ""))
            lines += self._ms_hit_lines(games, limit) or ["  无"]
        lines.append("wg m<四位码> 开始跟踪，wg- m<四位码> 取消。")
        lines.append("比赛场和拿不到观战票的段位场走终局后的牌谱回放。")
        yield event.make_result().message("\n".join(lines))

    @filter.command("wg-")
    async def wg_untrack(self, event: AstrMessageEvent, wg_id: str = ""):
        """wg- [四位码|8位WG码|雀魂UUID]：取消那场逐局播报；不带参数取消本群全部跟踪。"""
        session = str(event.session)
        self._th_prune(session)
        self._ms_prune(session)
        sess = self._th_sessions.get(session) or {}
        ms_sess = self._ms_sessions.get(session) or {}
        wg_id = (wg_id or "").strip()
        if not sess and not ms_sess:
            yield event.make_result().message("当前未跟踪任何对局。")
            return
        if not wg_id:
            for s in sess.values():
                s.stop()
            for s in ms_sess.values():
                s.stop()
            self._th_sessions.pop(session, None)
            self._ms_sessions.pop(session, None)
            yield event.make_result().message(
                f"已取消全部跟踪（天凤{len(sess)} 场、雀魂{len(ms_sess)} 场）。")
            return
        key = wg_id.lower()
        for mid, s in list(ms_sess.items()):
            if mid == key or s.uuid.lower() == wg_id.lower():
                s.stop()
                ms_sess.pop(mid, None)
                if not ms_sess:
                    self._ms_sessions.pop(session, None)
                yield event.make_result().message(f"已取消跟踪 #{mid}（雀魂）。")
                return
        target = self._th_pick(sess, wg_id)
        if not target:
            yield event.make_result().message(
                f"未找到跟踪中的对局: {wg_id}\n当前: "
                + ", ".join(list(sess.keys()) + list(ms_sess.keys()))
            )
            return
        sess[target].stop()
        sess.pop(target)
        if not sess:
            self._th_sessions.pop(session, None)
        yield event.make_result().message(f"已取消跟踪 #{target}（天凤）。")

    @filter.command("wgon")
    async def wgon(self, event: AstrMessageEvent, level: str = ""):
        """wgon [0|1|2]：开启本群自动播报推送，1 只天凤、2 只雀魂、0 或不带参数两边都开。"""
        session = str(event.session)
        sub = self._get_sub(session)
        ms_sub = self._get_ms_sub(session)
        # 1=只天凤, 2=只雀魂, 0 或无参=两边
        if level == "0":
            sub["enabled"] = True
            ms_sub["enabled"] = True
            yield event.make_result().message("推送已全部启用")
        elif level == "1":
            sub["enabled"] = True
            yield event.make_result().message("天凤推送已启用")
        elif level == "2":
            ms_sub["enabled"] = True
            yield event.make_result().message("雀魂推送已启用")
        else:
            sub["enabled"] = True
            ms_sub["enabled"] = True
            yield event.make_result().message("推送已全部启用")
        self._save_state()

    @filter.command("wgoff")
    async def wgoff(self, event: AstrMessageEvent, level: str = ""):
        """wgoff [0|1|2]：关闭本群自动播报推送，1 只天凤、2 只雀魂、0 或不带参数两边都关。"""
        session = str(event.session)
        sub = self._get_sub(session)
        ms_sub = self._get_ms_sub(session)
        if level == "0":
            sub["enabled"] = False
            ms_sub["enabled"] = False
            yield event.make_result().message("推送已全部禁用")
        elif level == "1":
            sub["enabled"] = False
            yield event.make_result().message("天凤推送已禁用")
        elif level == "2":
            ms_sub["enabled"] = False
            yield event.make_result().message("雀魂推送已禁用")
        else:
            sub["enabled"] = False
            ms_sub["enabled"] = False
            yield event.make_result().message("推送已全部禁用")
        self._save_state()

    @filter.command("wginfo")
    async def wginfo(self, event: AstrMessageEvent, wg_id: str = ""):
        """wginfo [四位码|8位WG码|雀魂UUID]：查跟踪中那场的当前比分；不带参数列全部。"""
        session = str(event.session)
        self._th_prune(session)
        self._ms_prune(session)
        sess = self._th_sessions.get(session) or {}
        ms_sess = self._ms_sessions.get(session) or {}
        if not sess and not ms_sess:
            yield event.make_result().message("当前未跟踪任何对局，无法查询比分。")
            return
        wg_id = (wg_id or "").strip()
        if wg_id:
            key = wg_id.lower()
            for mid, s in ms_sess.items():
                if mid == key or s.uuid.lower() == key:
                    yield event.make_result().message(s.brief())
                    return
            target = self._th_pick(sess, wg_id)
            if not target:
                yield event.make_result().message(
                    f"未找到跟踪中的对局: {wg_id}\n当前: "
                    + ", ".join(list(sess.keys()) + list(ms_sess.keys()))
                )
                return
            yield event.make_result().message(sess[target].brief())
            return
        yield event.make_result().message(
            "\n".join([s.brief() for s in sess.values()]
                      + [s.brief() for s in ms_sess.values()])
        )

    @filter.command("wglog")
    async def wglog(self, event: AstrMessageEvent, code: str = ""):
        """wglog <牌谱码或链接>：查那局天凤牌谱，逐局回放和了/流局与各家点数。"""
        code = (code or "").strip()
        if not code:
            yield event.make_result().message(
                "用法: wglog <牌谱码或链接>\n"
                "示例: wglog 2024010100gm-0000-0000-00000000"
            )
            return
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, LogReplayParser(code).parse)
        yield event.make_result().message(result)

    # ==================================================================
    #  旧指令兼容
    # ==================================================================
    @filter.command("观战")
    async def watch_compat(self, event: AstrMessageEvent, arg: str = ""):
        """观战 [参数]：旧指令，等价于 wg [参数]。"""
        async for _ in self.wg_track(event, arg):
            yield _

    @filter.command("看牌")
    async def replay_compat(self, event: AstrMessageEvent, code: str = ""):
        """看牌 <牌谱码或链接>：旧指令，等价于 wglog，不带参数只提示改用 wglog。"""
        code = (code or "").strip()
        if not code:
            yield event.make_result().message(
                "已升级为 wglog 指令，用法: wglog <牌谱码或链接>"
            )
            return
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, LogReplayParser(code).parse)
        yield event.make_result().message(result)

    @filter.command("停观")
    async def stop_compat(self, event: AstrMessageEvent):
        """停观：旧指令，等价于不带参数的 wg-，取消本群全部跟踪。"""
        session = str(event.session)
        self._th_prune(session)
        sess = self._th_sessions.get(session) or {}
        ms_sess = self._ms_sessions.get(session) or {}
        for s in sess.values():
            s.stop()
        for s in ms_sess.values():
            s.stop()
        if sess or ms_sess:
            self._th_sessions.pop(session, None)
            self._ms_sessions.pop(session, None)
            yield event.make_result().message(
                f"已取消全部跟踪（天凤{len(sess)} 场、雀魂{len(ms_sess)} 场）。")
        else:
            yield event.make_result().message("当前未跟踪任何对局。")

    async def terminate(self):
        for sess in self._th_sessions.values():
            for s in sess.values():
                s.stop()
        self._th_sessions.clear()
        self._th_auto_stop_all()
        self._th_watcher.stop()
        for sess in self._ms_sessions.values():
            for s in sess.values():
                s.stop()
        self._ms_sessions.clear()
        for s in self._ms_auto.values():
            s.stop()
        self._ms_auto.clear()
        self._ms_watcher.stop()
