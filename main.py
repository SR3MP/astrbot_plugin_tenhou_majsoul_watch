"""
天凤 / 雀魂 云观战插件

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

  ctset <玩家名> [节点] <文案>  设定制文案；节点 start/round/big/finish，省略为 finish
  ctdel <玩家名> [节点]        删定制文案（省略节点删全部）
  ctl                          列出本群定制文案
  report                       立即出本群 PT 日/周/月图片榜
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import time
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Image
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .tenhou_client import LogReplayParser
from .broadcast import (now_hm, start_text, is_qq_session, is_qq_platform,
                        md_text, event_head, apply_custom_broadcast,
                        localize_broadcast, name_hit, split_point_mark)
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
    koromo_blocked_reason,
    ms_field_label,
    ms_format_pt,
    ms_level_text,
    ms_short_id,
    set_koromo_token,
    set_display_config,
)
from .koromo_stats import match_point_rules
from .ms_live import MsGameSession, hule_head_line
from . import report as report_mod
from . import report_image
from .web_api import WatchWebAPI


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
msaddc <比赛号>          登记比赛场，wg 可跟踪、订阅玩家在该场开局会播报
msdelc <比赛号>          取消登记比赛场

ctset <玩家名> [节点] <文案>  设定制文案：start/round/big/finish，省略为 finish
ctdel <玩家名> [节点]         删定制文案（省略节点删全部）
ctl                           列出本群定制文案
report                        立即出本群 PT 日/周/月图片榜（定时报告在配置里开）
"""


PLUGIN_NAME = "astrbot_plugin_tenhou_majsoul_watch"
STATE_FILE_NAME = "sub_state.json"
# 登记的比赛场上限：每场每轮要 2 个 RPC（直播列表 + 牌谱索引），别让人一次登记几十个
MS_CONTEST_LIMIT = 5


def _plugin_data_dir() -> str:
    """插件数据目录 data/plugin_data/<插件名>/，由 AstrBot 统一管理和迁移。"""
    try:
        from astrbot.api.star import StarTools

        path = StarTools.get_data_dir(PLUGIN_NAME)
        if path:
            return str(path)
    except Exception as e:
        logger.debug(f"[tenhou] StarTools.get_data_dir 不可用，按默认规则推算: {e}")
    root = os.environ.get("ASTRBOT_ROOT") or os.getcwd()
    path = os.path.join(root, "data", "plugin_data", PLUGIN_NAME)
    os.makedirs(path, exist_ok=True)
    return path


@register(PLUGIN_NAME, "SR3MP", "天凤/雀魂云观战", "3.15.3")
class TenhouWatch(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # 保留配置对象给 WebUI 读写（Star 基类不保存 config）
        self.config = config
        # PT 定时报告配置（默认关闭，避免未经确认就往群里发东西）
        rp_cfg = (config or {}).get("report") or {}
        self._report_enabled = bool(rp_cfg.get("enabled", False))
        self._report_hour = max(0, min(23, int(rp_cfg.get("hour", 12) or 12)))
        self._report_minute = max(0, min(59, int(rp_cfg.get("minute", 0) or 0)))
        self._report_task = None
        self._report_last_day = ""
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
        # 牌谱屋反爬要 cap token：配置里填了就带上，没填就只按 AID 查
        set_koromo_token(str(((config or {}).get("koromo") or {}).get("token", "")))
        # 播报显示增强（数据来源/安定标签/雀豪彩蛋/大牌文案）
        display = (config or {}).get("display") or {}
        self._point_alert = bool(display.get("point_alert", False))
        point_rules = display.get("point_rules", []) or []
        if isinstance(point_rules, str):
            try:
                point_rules = json.loads(point_rules)
            except (ValueError, TypeError):
                point_rules = []
        if not self._point_alert:
            point_rules = []   # 开关关闭时一条不发（即使规则里留着缺省文案）
        self._point_rules = point_rules
        set_display_config(
            str(display.get("rate_source", "majsoul")),
            display.get("tiers", []),
            bool(display.get("sanhaoyi_image", False)),
            point_rules,
            bool(display.get("tier_labels", False)),
        )
        self._sanhaoyi_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "assets", "sanhaoyi.jpg")
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
            on_new_game=self._ms_new_game,
            contest_ids=self._ms_contest_ids_for_watch,
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
        # 登记的比赛场对局缓存（按比赛号直查，与上面那份分开存）
        self._ms_contest_reg: Optional[list[dict]] = None
        self._ms_contest_reg_at = 0.0
        # 主循环在 initialize() 里抓取后才能安全调度推送，这里先不起线程
        self._loop = None
        self._hydrate_task = None
        # 平台已被停用的会话，只提示一次，避免每条播报都刷同样的警告
        self._push_dead: set[str] = set()
        # 事件里确认过是 QQ 官方的实例名。刚重启时平台还没注册，主动推送
        # 拿不到实例列表；记住这些前缀，避免 Markdown 转换被跳过。
        self._qq_session_ids: set[str] = set()

        # 个人定制文案: session → {玩家名: 终局点评文案模板}。按 session+玩家
        # 隔离，避免同一玩家在不同群的定制串台。随 sub_state 一起落盘。
        self._custom_text: dict[str, dict[str, str]] = {}
        # 订阅/监听状态落盘（持久化）：存 data/plugin_data/<插件名>/，
        # 不写插件自身目录——插件目录升级时会被整目录替换，也便于用户迁移与审计。
        self._data_dir = _plugin_data_dir()
        self._state_file = os.path.join(self._data_dir, STATE_FILE_NAME)
        self._history_file = os.path.join(self._data_dir, report_mod.HISTORY_FILE_NAME)
        # 旧版写在插件目录里，读到就一次性搬走，搬完留 .migrated 备查
        self._legacy_state_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), STATE_FILE_NAME
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

    def _migrate_keys(self, store: dict) -> None:
        """迁移旧版纯数字 key；已有完整 key 时合并而不是整块覆盖。"""
        for key in list(store.keys()):
            if ":" in key:
                continue
            nk = self._normalize_sid(key)
            if nk == key:
                continue
            old = store.pop(key)
            cur = store.get(nk)
            if isinstance(old, dict) and isinstance(cur, dict):
                merged = dict(old)
                merged.update(cur)   # 完整 key 的数据优先
                store[nk] = merged
            else:
                store[nk] = old

    async def initialize(self) -> None:
        """插件激活后调用：拉起雀魂/天凤轮询线程。"""
        # 监听线程要靠这个 loop 把推送调度回主循环；在别的线程里
        # get_event_loop() 会直接抛 RuntimeError，旧版就是这么把推送全吞掉的。
        self._loop = asyncio.get_running_loop()
        # 迁移旧版纯数字 session key 为完整 origin（订阅、雀魂订阅、定制文案
        # 都要迁，漏掉定制文案会让旧文案永久失配）
        for store in (self._subs, self._ms_subs, self._custom_text):
            self._migrate_keys(store)
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
        # 旧状态里只存了账号数字，登录后把昵称补回来（推送显示用）。
        # 顺带在激活时就登录一次：旧版只在"有订阅"或"打指令"时才登，
        # 于是刚配好账号、还没订阅时 wgls 一直显示未登录，昵称反查也只能
        # 落到已经挂掉的牌谱屋上。登录放后台任务，不拖慢插件激活。
        self._hydrate_task = asyncio.create_task(self._ms_warmup())
        # PT 定时报告：默认关闭；开启后每天在配置时刻抓快照并推送变动榜
        if self._report_enabled:
            self._report_task = asyncio.create_task(self._report_loop())
        # AstrBot 内置 WebUI 管理页：注册后端 API（前端在 pages/tenhou-watch）
        try:
            self._web_api = WatchWebAPI(self)
            self._web_api.register_routes(self.context)
            logger.info("[tenhou] WebUI 管理页已注册")
        except Exception as e:
            logger.warning(f"[tenhou] WebUI 管理页注册失败（不影响群功能）: {e}")
        logger.info(
            f"[tenhou] 初始化完成: 天凤订阅{len(self._subs)}个, "
            f"雀魂订阅{len(self._ms_subs)}个（开局/终局自动播，逐局播报要 wg）"
            + (f"，PT报告每天{self._report_hour:02d}:{self._report_minute:02d}" if self._report_enabled else "")
        )

    async def _ms_warmup(self) -> None:
        """激活/热重载后主动登录雀魂，再补齐订阅昵称。

        没配账号就跳过；登录失败把原因写进日志，并留 3 次退避重试。
        不再让"没有订阅所以不登录"演成"账号没生效"。
        """
        if not self._ms_client.has_credentials:
            logger.info("[tenhou] 未配置雀魂账号，跳过登录（昵称反查不可用，只能按 AID）")
            return
        ok = await asyncio.to_thread(self._ms_client.ensure_connection)
        for attempt in (1, 2, 3):
            if ok:
                break
            logger.warning(
                f"[tenhou] 雀魂登录失败（第{attempt}次）: "
                f"{self._ms_client.last_error or '原因未知'}，60 秒后重试")
            await asyncio.sleep(60)
            ok = await asyncio.to_thread(self._ms_client.ensure_connection)
        if ok:
            logger.info(f"[tenhou] 雀魂账号登录成功（自己 AID={self._ms_client.self_aid}）")
        else:
            logger.error(
                f"[tenhou] 雀魂登录连续失败，已停止重试: "
                f"{self._ms_client.last_error or '原因未知'}")
        await self._hydrate_ms_names()

    def _ms_nick_hint(self) -> str:
        """昵称反查失败时的补充说明：区分"被反爬拦了"和"真的没这个人"。"""
        if not self._ms_client.has_credentials:
            head = "未配置雀魂账号，昵称反查只能用牌谱屋"
        elif not self._ms_client.authed:
            head = "雀魂未登录（" + (self._ms_client.last_error or "原因未知") + "）"
        else:
            head = "已登录，但官方昵称搜索没命中"
        reason = koromo_blocked_reason()
        if reason:
            head += (f"{reason}；可换网络重试，"
                     "或在插件配置 koromo.token 填写牌谱屋 key")
        return head + "\n也可直接用 AID: mspt <AID> 或 /msadd $<AID>"

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
    def _state_source(self) -> tuple[str, bool]:
        """返回待读取的状态文件，以及它是否为旧版插件目录内路径。"""
        if os.path.exists(self._state_file):
            return self._state_file, False
        legacy = self._legacy_state_file
        if legacy and os.path.exists(legacy):
            return legacy, True
        return "", False

    def _migrate_state_file(self, legacy: str) -> None:
        """把旧版插件目录里的状态文件搬到 plugin_data，旧文件改名留档。"""
        try:
            self._save_state()
            os.replace(legacy, legacy + ".migrated")
            logger.info(f"[tenhou] 订阅状态已迁移到 {self._state_file}")
        except Exception as e:
            logger.warning(f"[tenhou] 订阅状态迁移未完成（旧文件保留）: {e}")

    def _load_state(self) -> None:
        """启动时恢复各群订阅（天凤对局跟踪不恢复，需重新 wg）。"""
        path, is_legacy = self._state_source()
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logger.warning(f"[tenhou] 读取订阅状态失败: {e}")
            return

        subs = raw.get("th", {}) or {}
        ms_subs = raw.get("ms", {}) or {}
        ms_names = raw.get("ms_names", {}) or {}
        custom = raw.get("custom_text", {}) or {}
        _nodes = ("start", "round", "big", "finish")
        if isinstance(custom, dict):
            for sid, cmap in custom.items():
                if not isinstance(cmap, dict):
                    continue
                norm = {}
                for k, v in cmap.items():
                    k = str(k)
                    if isinstance(v, dict):
                        # 新结构：{玩家: {节点: 模板}}，只留有内容的节点
                        kept = {}
                        for n in _nodes:
                            nv = v.get(n)
                            if n == "big" and isinstance(nv, dict):
                                # big 可是结构化 {和出/点出/两者: [规则]}，保留
                                rules = {}
                                for kk in ("和出", "点出", "两者"):
                                    arr = nv.get(kk)
                                    if isinstance(arr, list) and arr:
                                        rules[kk] = arr
                                if rules:
                                    kept[n] = rules
                            elif n == "finish" and isinstance(nv, dict):
                                # finish 可是结构化 {1/2/3/4/*: 模板}，按名次分档
                                ranks = {}
                                for rk, rv in nv.items():
                                    if str(rk) not in ("1", "2", "3", "4", "*", "通用"):
                                        continue
                                    rv2 = str(rv or "").strip()
                                    if rv2:
                                        ranks[str(rk)] = rv2
                                if ranks:
                                    kept[n] = ranks
                            elif str(nv or "").strip():
                                kept[n] = str(nv).strip()
                        if kept:
                            norm[k] = kept
                    elif str(v or "").strip():
                        # 旧结构：{玩家: 点评模板} → 视作 finish
                        norm[k] = str(v).strip()
                if norm:
                    self._custom_text[str(sid)] = norm
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
            contests = [int(c) for c in (sub.get("contests") or []) if str(c).isdigit()]
            # 只登记了比赛场的会话也要留住，否则重启后 wg 又找不到那些比赛
            if not accounts and not contests:
                continue
            self._ms_subs[sid] = {
                "accounts": accounts,
                "enabled": bool(sub.get("enabled", True)),
                "filters": list(sub.get("filters", [])),
                "contests": contests,
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

        if is_legacy:
            self._migrate_state_file(path)

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
                    "contests": sorted(int(c) for c in (sub.get("contests") or [])),
                }
            # 昵称只在订阅时拿得到，重启后要靠它显示，所以一并落盘
            names = {
                str(aid): nick
                for aid, nick in self._ms_watcher.watching.items() if nick
            }
            custom = {
                sid: dict(cmap) for sid, cmap in self._custom_text.items() if cmap
            }
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"th": th, "ms": ms, "ms_names": names, "custom_text": custom},
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
                "accounts": set(), "enabled": True, "filters": [], "contests": [],
            }
        else:
            self._ms_subs[session].setdefault("contests", [])
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

    def _platform_state(self, session: str) -> str:
        """会话前缀那个平台现在是什么状态：loaded / pending / disabled。

        - loaded：适配器已注册，正常发。
        - pending：配置里是启用的但实例还没注册（重启后适配器加载中），值得重试。
        - disabled：配置里已停用或整个平台被删掉，重试永远不会成功——盲试 36 次
          只会白占三分钟，还把播报全丢了（曾对停用的 OneBot 群连试 3 分钟）。
        读不到配置就按 pending 处理，保持原来的重试行为。
        """
        pid = str(session or "").split(":", 1)[0].strip()
        if not pid:
            return "loaded"
        try:
            for p in self.context.platform_manager.platform_insts:
                if getattr(p.meta(), "id", "") == pid:
                    return "loaded"
        except Exception:
            return "pending"
        try:
            for cfg in (self.context.platform_manager.platforms_config or []):
                if str(cfg.get("id") or "") == pid:
                    return "pending" if cfg.get("enable") else "disabled"
        except Exception:
            return "pending"
        return "disabled"

    def _skip_dead_session(self, session: str) -> None:
        """平台已停用：这条不发也不重试，同一会话只提示一次。"""
        if session in self._push_dead:
            return
        self._push_dead.add(session)
        logger.warning(
            f"[push] 会话所属平台未启用，跳过播报（重新启用平台后自动恢复）: {session}")

    def _qq_platform_ids(self) -> set:
        """所有 QQ 官方平台实例的 ID（即 unified_msg_origin 前缀）。

        AstrBot 允许给同一类型配多个实例，前缀用的是自定义实例名（如「虎皮A梦」），
        拿 event.get_platform_name() 才能知道真实类型。这里遍历实例，按类型反查 ID，
        供没有 event 的主动推送判断是否要走 Markdown 转换。
        """
        ids = set()
        try:
            for p in self.context.platform_manager.platform_insts:
                meta = p.meta()
                if is_qq_platform(meta.name) or is_qq_platform(
                        getattr(meta, "adapter_display_name", "")):
                    if meta.id:
                        ids.add(meta.id)
        except Exception:
            pass
        return ids

    def _focus_names(self, session: str) -> set[str]:
        """本会话订阅的玩家名（播报加粗用），只认真实订阅名单不猜文案。

        天凤订阅存玩家名；雀魂订阅存 AID，昵称从 watcher 的反查表里取。
        """
        names: set[str] = set()
        sub = self._subs.get(session) or {}
        for p in (sub.get("players") or ()):
            if p:
                names.add(str(p))
        ms_sub = self._ms_subs.get(session) or {}
        watching = getattr(self._ms_watcher, "watching", {}) or {}
        for aid in (ms_sub.get("accounts") or ()):
            names.add(str(aid))
            nick = watching.get(aid) or watching.get(str(aid))
            if nick and str(nick) != str(aid):
                names.add(str(nick))
        return names

    def _md(self, session: str, text: str, platform_type: str = "") -> str:
        """QQ 官方把 Plain 当 Markdown 发，这里只在 QQ 会话做排版转换。

        判断优先级：事件真实类型 > 平台实例 ID > 会话前缀兜底。
        """
        sid = str(session or "").split(":", 1)[0].strip()
        qq = (is_qq_platform(platform_type) if platform_type else False)
        if not qq:
            qq = sid in self._qq_session_ids or sid in self._qq_platform_ids()
        if not qq:
            qq = is_qq_session(session)
        return md_text(text, self._focus_names(session)) if qq else text

    def _maybe_md(self, event, text: str) -> str:
        platform_type = ""
        try:
            platform_type = event.get_platform_name()
        except Exception:
            pass
        if is_qq_platform(platform_type):
            # 事件带的是真实平台类型，比主动推送时反查实例可靠；记下实例名，
            # 下次 watcher 在平台注册完成前推送也能按 Markdown 排版。
            self._qq_session_ids.add(str(event.session).split(":", 1)[0].strip())
        return self._md(str(event.session), text, platform_type)

    def _reply(self, event, text: str):
        return event.make_result().message(self._maybe_md(event, text))

    def _session_custom(self, session: str, text: str) -> str:
        """手动查询（wg 等）的回执也套用本群定制文案。

        只对含【对局开始】等节点标记的播报格式生效，普通回复原样返回。
        """
        try:
            return apply_custom_broadcast(text, self._custom_text.get(session))
        except Exception:
            return text

    async def _push(self, session: str, text: str, meta: dict = None) -> None:
        if self._platform_state(session) == "disabled":
            self._skip_dead_session(session)
            return
        # watcher 的文案是按「全场所有订阅玩家」拼的，转发到本群前先把主角
        # 名单和顺位点评换成只有本群订阅的人（定制文案必须在这之后套）
        text = localize_broadcast(text, self._focus_names(session))
        # 个人定制文案：按本群配置重写开局/小局/大牌/终局四类节点播报
        text = apply_custom_broadcast(text, self._custom_text.get(session), meta=meta)
        err = ""
        for i in range(self.PUSH_TRIES):
            # 刚重启时平台实例可能还没注册完，第一次发出去的会被 QQ 当纯 Markdown
            # 解析（# 变标题、引用丢失）。平台在重试间隔里就绪后必须重新排版。
            payload = self._md(session, text)
            try:
                if await self.context.send_message(
                        session, MessageChain([Plain(payload)])):
                    self._push_dead.discard(session)
                    return
                err = "send_message 返回 False"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            if i == 0:
                logger.info(f"[push] 平台未就绪，稍后重发: {session}")
            await asyncio.sleep(self.PUSH_RETRY_WAIT)
            # 等待期间平台被停用/删除就别再耗下去，剩下的尝试也不会成功
            if self._platform_state(session) == "disabled":
                self._skip_dead_session(session)
                return
        logger.warning(f"推送丢弃（{self.PUSH_TRIES} 次未发出）: {session} {err}")

    async def _push_image(self, session: str, path: str) -> None:
        """给会话单独推一张本地图片（三家雀豪一发图）。"""
        if not os.path.isfile(path):
            logger.warning(f"[ms] 图片不存在，跳过发图: {path}")
            return
        if self._platform_state(session) == "disabled":
            self._skip_dead_session(session)
            return
        try:
            await self.context.send_message(
                session, MessageChain([Image.fromFileSystem(path)]))
        except Exception as e:
            logger.warning(f"[ms] 发图失败: {type(e).__name__}: {e}")

    def _ms_notify(self, text: str, account_ids: set, field: str,
                   mid: str = "", extra: dict = None, meta: dict = None) -> None:
        """雀魂 watcher 的推送回调（在轮询线程里调用）。

        只推给真正订阅了这局选手、且筛选通过的会话。旧版广播给所有
        开启推送的群，一个群订阅的人开局，别的群也会收到。
        extra 可带 sanhaoyi=True，开局那条之后给每个会话补发一张图。
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
            # 大牌通报按全场订阅玩家触发，这里只发给真正订阅了和牌/放铳
            # 那位玩家的群。优先用 AID 精确判断（昵称可能重复或互相包含），
            # 老数据没有 AID 时才退回昵称匹配。
            actor_aid = str((meta or {}).get("aid") or "")
            actor = str((meta or {}).get("who") or "")
            if actor_aid:
                if actor_aid not in {str(a) for a in subscribed}:
                    continue
            elif actor and not name_hit(actor, self._focus_names(session)):
                continue
            if mid and is_finish_text(text):
                if self._ms_end_covered(session, mid):
                    continue
                self._ms_mark(self._ms_ends_seen, session, mid)
            elif mid and "【对局开始】" in text:
                if self._ms_marked(self._ms_starts_seen, session, mid):
                    continue
                self._ms_mark(self._ms_starts_seen, session, mid)

            async def _send(dst=session):
                await self._push(dst, text, meta=meta)
                if extra and extra.get("sanhaoyi"):
                    await self._push_image(dst, self._sanhaoyi_path)

            try:
                asyncio.run_coroutine_threadsafe(_send(), loop)
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
        focus = self._ms_auto_focus(acc)
        mid = ms_short_id(uuid_str)
        s = MsGameSession(uuid_str,
                          self._ms_auto_push(uuid_str, acc,
                                             str(game.get("field") or ""), mid),
                          self._ms_client, game,
                          watchers=focus,
                          on_hule=lambda hand, names: self._ms_hule_texts(
                              hand, names, focus, mid,
                              players=game.get("players") or []))
        self._ms_auto[uuid_str] = s
        s.start()

    def _ms_hule_texts(self, hand: dict, names: list, focus: set,
                       mid: str = "", players: list = None) -> list:
        """功能4：未 wg 的自动流里，被订阅玩家和出/点出超阈值点数 → 大牌通报。

        通报带和牌那一行的描述（局名 + 和牌家 + 自摸/荣和 + 点数 + 番符），
        文案接在后面：`#mc361【大牌通报】12:06` / `东4局 … 26000 8番20符，笑嘻了。`
        点出用同一行和牌描述，只换文案（如「急哭了」）。
        和出：和牌家是订阅玩家，取其点数变化（after-before，正数）。
        点出：荣和时点炮家是订阅玩家，取和牌家收益（即放铳损失）为正数。
        文案由 display.point_rules 匹配；带哨兵前缀交回自动流出口放行。
        """
        if not focus:
            return []
        hules = hand.get("hules") or []
        before = hand.get("before") or []
        after = hand.get("after") or []
        out: list[str] = []
        if not hules:
            return out

        def delta(seat: int) -> int:
            try:
                return int(after[seat]) - int(before[seat])
            except (IndexError, TypeError, ValueError):
                return 0

        ld = hand.get("last_discard") or []
        seen = set()
        plist = players or []

        def seat_aid(seat: int) -> str:
            try:
                return str(plist[seat].get("account_id") or "")
            except (IndexError, TypeError, AttributeError):
                return ""

        for hu in hules:
            seat = int(hu.get("seat") or 0)
            who = names[seat] if 0 <= seat < len(names) else ""
            gain = abs(delta(seat))
            head = hule_head_line(hand, names, hu)   # 和牌通报行，点数后不带「点」

            def emit(kind: str, texts, actor: str = "", actor_aid: str = "") -> None:
                for t in texts:
                    key = (head, kind, t)
                    if key in seen:
                        continue
                    seen.add(key)
                    body = f"{head}，{t}。" if head else t
                    line = (event_head(mid, "大牌通报", now_hm())
                            + "\n" + body) if mid else body
                    # 把「和出/点出 + 本局点数 + 触发玩家」编码进哨兵前缀，
                    # 供定制文案分档匹配、推送层按群过滤；旧三段格式仍可解析。
                    out.append(self._POINT_MARK
                               + f"{kind}|{gain}|{actor}|{actor_aid}|" + line)

            # 和出：和牌家本人是否被订阅
            if who and any(who == f or (f and f in who) for f in focus):
                emit("和出", match_point_rules("和出", gain, self._point_rules),
                     who, seat_aid(seat))
            # 点出：荣和时点炮家是否被订阅（自摸没有点炮家）
            if not hu.get("zimo") and ld and ld[1] == hu.get("hu"):
                fromseat = int(ld[0])
                loser = names[fromseat] if 0 <= fromseat < len(names) else ""
                if loser and any(loser == f or (f and f in loser) for f in focus):
                    emit("点出", match_point_rules("点出", gain, self._point_rules),
                         loser, seat_aid(fromseat))
        return out

    def _ms_auto_focus(self, account_ids) -> set:
        """终局那句点评的主角：这局里被订阅的那几个人的昵称。"""
        return {self._ms_watcher.watching.get(a, "")
                for a in (account_ids or ())} - {""}

    # 功能4文案的哨兵：自动流只放行【对局完成】，带这个前缀的单独放行再剥掉
    _POINT_MARK = "\x00POINT\x00"
    _PT_IMG_MARK = "\x00PTIMG\x00"   # _run_report 回执：后面跟图片路径，指令改发图

    def _ms_split_point_mark(self, text: str) -> tuple[str, dict | None]:
        """剥掉大牌通报的哨兵前缀，返回 (正文, {"kind","gain"})。

        实现放在 broadcast.py（纯文本层），这里只做转发，方便单独测。
        """
        return split_point_mark(text, self._POINT_MARK)

    def _ms_auto_push(self, uuid_str: str, account_ids, field: str, mid: str):
        """自动那条流的出口：放行【对局完成】和功能4大点数文案，其余不发。"""
        async def push(text: str) -> None:
            if text.startswith(self._POINT_MARK):
                body, meta = self._ms_split_point_mark(text)
                self._ms_notify(body, account_ids, field, "", meta=meta)
                return
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
        # 大牌通报（功能4）只走自动流，wg 这条不挂 on_hule：
        # 手动围观只要逐局结算，别再额外刷大牌通报。
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

    def _ms_contest_ids(self) -> list[int]:
        """本插件登记过的比赛号（跨会话去重），供 wg / 监控线程使用。"""
        ids = set()
        for sub in self._ms_subs.values():
            for c in sub.get("contests") or []:
                try:
                    ids.add(int(c))
                except (TypeError, ValueError):
                    continue
        return sorted(ids)

    def _ms_contest_ids_for_watch(self) -> list[int]:
        """监控线程每轮额外侦测的比赛号，按登记顺序取前 MS_CONTEST_LIMIT 场。"""
        return self._ms_contest_ids()[:MS_CONTEST_LIMIT]

    async def _ms_registered_contest_games(self, force: bool = False) -> list[dict]:
        """登记的比赛场：按比赛号直查进行中 + 最近终局的对局。

        默认那份 fetchCustomizedContestList 只给"账号参加过的比赛"，
        msls <比赛号> 和这里都走 contest_id 直查，不受这个限制。
        """
        ids = self._ms_contest_ids()
        if not ids:
            return []
        now = time.time()
        if force or now - self._ms_contest_reg_at > 60:
            rows: list[dict] = []
            for cid in ids[:MS_CONTEST_LIMIT]:
                try:
                    rows += await asyncio.to_thread(
                        self._ms_client.get_contest_rows, 3, cid)
                except Exception as e:
                    logger.warning(f"[ms] 比赛 {cid} 拉取失败: {e}")
            self._ms_contest_reg = rows
            self._ms_contest_reg_at = now
        return list(self._ms_contest_reg or [])

    async def _ms_all_games(self, force: bool = False) -> list[dict]:
        """段位场 + 比赛场，wg <四位码> 就是在这份合并列表里找。"""
        games = await self._ms_live_games(force)
        games += await self._ms_contest_games(force)
        games += await self._ms_registered_contest_games(force)
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
        yield self._reply(event, HELP_TEXT)

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
        contests = ms_sub.get("contests") or []
        lines.append("比赛场: " + (", ".join(str(c) for c in contests) if contests else "无"))
        if self._ms_client.authed:
            lines.append("雀魂登录: 已认证")
        elif not self._ms_client.has_credentials:
            lines.append("（未配置雀魂账号：无法监控对局，昵称反查仅牌谱屋可用，请在插件配置中填写账号）")
        else:
            err = self._ms_client.last_error or "尚未完成登录"
            lines.append(f"（雀魂未登录：{err}。账号已配置，会自动重试；也可发 wgstat 看状态）")

        yield self._reply(event, "\n".join(lines))

    # ==================================================================
    #  指令: thadd / thdel / thfilter
    # ==================================================================
    @filter.command("thadd")
    async def thadd(self, event: AstrMessageEvent, player_id: str = ""):
        """thadd <玩家名>：订阅天凤玩家，他开局/散场时本群自动播一条。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield self._reply(event, "用法: thadd <玩家名>\n示例: thadd 张三")
            return
        session = str(event.session)
        sub = self._get_sub(session)
        sub["players"].add(player_id)
        self._th_sync_watcher()
        self._save_state()
        yield self._reply(event, f"已订阅天凤ID: {player_id}")

    @filter.command("thdel")
    async def thdel(self, event: AstrMessageEvent, player_id: str = ""):
        """thdel <玩家名>：取消订阅这个天凤玩家。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield self._reply(event, "用法: thdel <玩家名>")
            return
        session = str(event.session)
        sub = self._get_sub(session)
        if player_id in sub["players"]:
            sub["players"].discard(player_id)
            self._th_sync_watcher()
            self._save_state()
            yield self._reply(event, f"已删除订阅: {player_id}")
        else:
            yield self._reply(event, f"未找到订阅: {player_id}")

    @filter.command("thfilter")
    async def thfilter(self, event: AstrMessageEvent, rest: GreedyStr):
        """thfilter <场名关键字>…：只自动播报场名匹配这些关键字的天凤对局；不带参数查看，all 清空。"""
        args = rest.split()
        session = str(event.session)
        sub = self._get_sub(session)
        if not args:
            current = ", ".join(sub["filters"]) if sub["filters"] else "不限"
            yield self._reply(event,
                f"当前天凤筛选: {current}\n"
                "用法: thfilter <场名关键字> [关键字...]\n"
                "按场名子串匹配，如: thfilter 鳳南 鳳東\n"
                "清空: thfilter all|不限|无"
            )
            return
        if args[0].lower() in ("all", "不限", "无"):
            sub["filters"] = []
            self._save_state()
            yield self._reply(event, "天凤筛选已清空。")
            return
        sub["filters"] = list(args)
        self._save_state()
        yield self._reply(event, f"天凤筛选已设为: {', '.join(args)}")

    # ==================================================================
    #  指令: 个人定制文案 ctset / ctdel / ctl
    # ==================================================================
    _CT_NODES = {
        "start": "开局", "round": "小局/流局", "big": "大牌通报", "finish": "终局",
    }

    @filter.command("ctset")
    async def ctset(self, event: AstrMessageEvent, rest: GreedyStr):
        """ctset <玩家名> [节点] <文案>：设定制文案，写入即生效。

        节点可选 start/round/big/finish（省略默认 finish）。
        可用 {name}{raw}{head}{body} 及各节点专属占位符；详见 WebUI。
        """
        args = (rest or "").strip()
        if not args:
            yield self._reply(event,
                "用法: ctset <玩家名> [节点] <文案>\n"
                "  节点: start开局 / round小局 / big大牌 / finish终局(默认)\n"
                "例: ctset 豆沙团子 finish {name}喜提{place}名，点数{score}\n"
                "例: ctset 豆沙团子 start 🚨{name}开打{game}\n"
                "查看: ctl  删除: ctdel <玩家名> [节点]")
            return
        parts = args.split(None, 2)
        if len(parts) < 2:
            yield self._reply(event, "用法: ctset <玩家名> [节点] <文案>")
            return
        name, node, tpl = parts[0].strip(), "finish", ""
        if len(parts) == 3 and parts[1].strip().lower() in self._CT_NODES:
            node = parts[1].strip().lower()
            tpl = parts[2].strip()
        else:
            tpl = " ".join(parts[1:]).strip()
        if not tpl:
            yield self._reply(event, "文案不能为空")
            return
        session = str(event.session)
        cmap = self._custom_text.setdefault(session, {})
        entry = cmap.get(name)
        if isinstance(entry, dict):
            entry[node] = tpl
        else:
            # 旧字符串视作 finish；新节点覆盖它
            upgraded = {"finish": entry} if entry else {}
            upgraded[node] = tpl
            cmap[name] = upgraded
        self._save_state()
        yield self._reply(event,
            f"已为「{name}」设置{self._CT_NODES[node]}定制文案:\n{tpl}")

    @filter.command("ctdel")
    async def ctdel(self, event: AstrMessageEvent, rest: GreedyStr = None):
        """ctdel <玩家名> [节点]：删某玩家定制文案的节点；省略节点删全部。"""
        args = (rest or "").strip()
        session = str(event.session)
        cmap = self._custom_text.get(session)
        if not cmap:
            yield self._reply(event, "本群没有定制文案。")
            return
        parts = args.split()
        name = parts[0].strip() if parts else ""
        node = parts[1].strip().lower() if len(parts) > 1 else ""
        if not name:
            yield self._reply(event,
                "用法: ctdel <玩家名> [节点]\n当前定制: " +
                (", ".join(sorted(cmap)) if cmap else "空"))
            return
        if name not in cmap:
            yield self._reply(event, f"「{name}」没有定制文案。当前: " +
                              (", ".join(sorted(cmap)) or "空"))
            return
        entry = cmap[name]
        if node and node in self._CT_NODES and isinstance(entry, dict):
            entry.pop(node, None)
            if not entry:
                del cmap[name]
            what = f"{self._CT_NODES[node]}节点"
        else:
            del cmap[name]
            what = "全部"
        if not cmap:
            self._custom_text.pop(session, None)
        self._save_state()
        yield self._reply(event, f"已删除「{name}」的{what}定制文案。")

    @filter.command("ctl")
    async def ctl(self, event: AstrMessageEvent):
        """ctl：列出本群所有定制文案（按玩家×节点）。"""
        session = str(event.session)
        cmap = self._custom_text.get(session) or {}
        if not cmap:
            yield self._reply(event,
                "本群没有定制文案。\n"
                "设置: ctset <玩家名> [节点] <文案>\n"
                "  节点: start开局 / round小局 / big大牌 / finish终局(默认)")
            return
        lines = [f"本群定制文案 ({len(cmap)} 名玩家):"]
        for n, v in sorted(cmap.items()):
            if isinstance(v, dict):
                for node, tpl in v.items():
                    label = self._CT_NODES.get(node, node)
                    lines.append(f"  - {n} [{label}]: {tpl}")
            else:
                lines.append(f"  - {n} [终局]: {v}")
        lines.append("删除: ctdel <玩家名> [节点]")
        yield self._reply(event, "\n".join(lines))

    # ==================================================================
    #  指令: msadd / msdel / msfilter / mspt
    # ==================================================================
    @filter.command("msadd")
    async def msadd(self, event: AstrMessageEvent, player_id: str = ""):
        """msadd <AID|昵称> 或 msadd $<AID>：订阅雀魂玩家，他开局/散场时本群自动播一条。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield self._reply(event,
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
                        yield self._reply(event,
                            f"未找到玩家: {player_id}\n{self._ms_nick_hint()}")
                        return
                    account_id = found["account_id"]
                    nickname = found.get("nickname", raw)
                except Exception as e:
                    yield self._reply(event, f"雀魂查询失败: {e}")
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
                    yield self._reply(event, "\n".join(lines))
                    return
                else:
                    yield self._reply(event,
                        f"未找到玩家: {player_id}\n{self._ms_nick_hint()}")
                    return

        # 已登录且还没拿到昵称，尝试补全（用于显示）
        if logged_in and not nickname:
            try:
                info = await asyncio.to_thread(
                    self._ms_client.lookup_player, account_id, None)
                nickname = info.get("nickname", "")
            except Exception as e:
                yield self._reply(event, f"雀魂查询失败: {e}")
                return

        ms_sub["accounts"].add(account_id)
        self._ms_watcher.add(account_id, nickname)
        self._save_state()

        label = f"{account_id}" + (f"（{nickname}）" if nickname else "")
        yield self._reply(event, f"已订阅雀魂AID: {label}")

    @filter.command("msdel")
    async def msdel(self, event: AstrMessageEvent, player_id: str = ""):
        """msdel <AID|昵称> 或 $<AID>：取消订阅这个雀魂玩家（重名时需用 $<AID> 指定）。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield self._reply(event,
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
                yield self._reply(event,
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
                    yield self._reply(event, f"未找到雀魂订阅: {player_id}")
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
            yield self._reply(event, f"已删除雀魂订阅: {label}")
        else:
            yield self._reply(event,
                f"未找到雀魂订阅: {self._ms_watcher.watching.get(account_id) or account_id}")

    @filter.command("msfilter")
    async def msfilter(self, event: AstrMessageEvent, rest: GreedyStr):
        """msfilter <场名关键字>…：只自动播报场名匹配这些关键字的雀魂对局；不带参数查看，all/不限 清空。"""
        args = rest.split()
        session = str(event.session)
        ms_sub = self._get_ms_sub(session)
        if not args:
            current = ", ".join(ms_sub["filters"]) if ms_sub["filters"] else "不限"
            yield self._reply(event,
                f"当前雀魂筛选: {current}\n"
                "用法: msfilter <场名关键字> [关键字...]\n"
                "按场名子串匹配，如: msfilter 金 玉 王座\n"
                "清空: msfilter all|不限"
            )
            return
        if [a.lower() for a in args] == ["all"] or list(args) == ["不限"]:
            ms_sub["filters"] = []
            self._save_state()
            yield self._reply(event, "雀魂筛选已清空。")
            return
        ms_sub["filters"] = list(args)
        self._save_state()
        yield self._reply(event, f"雀魂筛选已设为: {', '.join(args)}")

    @filter.command("mspt")
    async def mspt(self, event: AstrMessageEvent, player_id: str = ""):
        """mspt <AID|昵称> 或 $<AID>：查雀魂段位和 PT；未登录时按昵称走牌谱屋，重名会列候选。"""
        player_id = (player_id or "").strip()
        if not player_id:
            yield self._reply(event,
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
                    yield self._reply(event, ms_format_pt(cands[0]))
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
                    yield self._reply(event, "\n".join(lines))
                    return
                # 0 条：继续走 lookup_player 兜底（可能命中观战列表）

            stats = await asyncio.to_thread(
                self._ms_client.lookup_player, account_id, raw)
            if not stats:
                if account_id is not None and not logged_in:
                    yield self._reply(event,
                        "未登录时无法按 AID 查询。\n"
                        "请在插件配置里填写雀魂账号，或改用昵称: mspt <昵称>"
                    )
                    return
                if account_id is None:
                    yield self._reply(event,
                        f"未找到玩家: {player_id}\n{self._ms_nick_hint()}")
                    return
                yield self._reply(event, f"未找到玩家: {player_id}")
                return
            yield self._reply(event, ms_format_pt(stats))
        except Exception as e:
            yield self._reply(event, f"雀魂查询失败: {e}")

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
        def _aids(items) -> set:
            """订阅账号可能是 int 也可能是 str，统一成 int 集合再比对。"""
            out = set()
            for a in (items or ()):
                try:
                    out.add(int(a))
                except (TypeError, ValueError):
                    pass
            return out

        lines.append(f"天凤订阅: {len(sub['players'])} 人")
        f = sub.get("filters") or []
        lines.append(f"天凤筛选: {', '.join(f) if f else '不限'}")
        lines.append(f"天凤推送: {'开启' if sub['enabled'] else '关闭'}")
        lines.append(f"天凤监控: {'运行中' if self._th_watcher.running else '停止'}")
        th_players = set(sub.get("players") or ())
        th_snapshot = self._th_watcher.snapshot()
        th_auto_games = [g for g in th_snapshot
                         if th_players & set(g.get("hits") or ())]
        th_wg_hits = {str(g["wg"]).upper(): set(g.get("hits") or ())
                      for g in th_snapshot}
        th_auto_all = [s for s in self._th_auto.values() if not s.finished]
        th_auto_mine = [s for s in th_auto_all
                        if th_players & th_wg_hits.get(str(s.wg_id).upper(), set())]
        lines.append(f"天凤自动观战流: 本群 {len(th_auto_mine)} 场"
                     f"（全局 {len(th_auto_all)}/{self._TH_MAX_AUTO}）")
        if th_auto_games:
            lines.append(f"天凤进行中对局（本群） {len(th_auto_games)} 场:")
            for g in th_auto_games:
                lines.append(f"  #{g['mid']} {g['rule']} 始于{g['since']} {g['players']}")
        else:
            lines.append("天凤进行中对局（本群）: 无")
        lines.append("")
        if ms_tracked:
            lines.append(f"雀魂跟踪 {len(ms_tracked)} 场:")
            for s in ms_tracked.values():
                lines.append(f"  {s.brief()}")
        else:
            lines.append("雀魂跟踪: 无")
        ms_aids = _aids(ms_sub.get("accounts"))
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
        games_all = self._ms_watcher.snapshot()
        games = [g for g in games_all if ms_aids & _aids(g.get("accounts"))]
        ms_uuid_aids = {str(g["uuid"]): _aids(g.get("accounts")) for g in games_all}
        ms_auto_all = [s for s in self._ms_auto.values() if not s.finished]
        ms_auto_mine = [s for s in ms_auto_all
                        if ms_aids & ms_uuid_aids.get(str(s.uuid), set())]
        lines.append(f"雀魂自动观战流: 本群 {len(ms_auto_mine)} 场"
                     f"（全局 {len(ms_auto_all)}/{self._MS_MAX_AUTO}）")
        if games:
            lines.append(f"雀魂进行中对局（本群） {len(games)} 场:")
            for g in games:
                lines.append(f"  #{g['mid']} {g['field']} 始于{g['since']} {g['players']}")
        else:
            lines.append("雀魂进行中对局（本群）: 无")
        if not self._ms_watcher.record_detail:
            lines.append("雀魂牌谱权限: 不可用")

        yield self._reply(event, "\n".join(lines))

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
        text = self._th_start_text(game, wg, focus) + "\n" + self._watch_note("天凤", mid)
        yield self._session_custom(session, text)

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
            text = (start_text(mid, ms_field_label(game.get("field") or ""),
                               names, now_hm(), self._ms_focus(session, game),
                               (), seats=False)
                    + "\n" + self._watch_note("雀魂", mid))
            yield self._session_custom(session, text)
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
                    lines.append("  无")
            yield self._reply(event, "\n".join(lines))
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
                yield self._reply(event, text)
            return

        hits = await self._th_games_for_mid(arg.lower()) if is_short else []

        if is_short and hits:
            if len(hits) > 1:
                yield self._reply(event,
                    f"四位码 {arg.lower()} 对应 {len(hits)} 场对局，未开始跟踪。"
                    "改用完整 WG 码:\n"
                    + "\n".join(f"  wg {g['wg']}" for g in hits)
                )
                return
            async for text in self._wg_begin(session, hits[0]):
                yield self._reply(event, text)
            return

        if is_short and not (is_lobby_word or is_lobby_num):
            yield self._reply(event,
                "未定位到天凤对局，未开始跟踪。可用 wg 列出可跟踪对局。"
            )
            return

        # 雀魂完整 uuid（250927-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx）
        if re.fullmatch(r"\d{6}-[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", arg):
            async for text in self._ms_begin(session, await self._ms_games_for_uuid(arg)):
                yield self._reply(event, text)
            return

        if is_token:
            wg_id = arg.upper()
            await self._th_recent_games()
            game = self._th_find_by_wg(wg_id)
            async for text in self._wg_begin(session, game, wg_id):
                yield self._reply(event, text)
            return

        if is_lobby_word or is_lobby_num:
            lb = parse_lobby(arg)
            games = await asyncio.to_thread(self._th_scan, [lb])
            if not games:
                # C 码带的规则后缀（如 4237）不是列表文件名，扫全段位房更实在
                games = await self._th_recent_games(force=True)
            hits = self._th_match(session, games)
            if not hits:
                yield self._reply(event,
                    f"大厅 {lb} 没有匹配的进行中对局。只匹配订阅玩家且通过筛选的对局。\n"
                    "可用 wg <四位码> 指定，或 thadd <玩家名> 订阅玩家。"
                )
                return
            if len(hits) > 1:
                yield self._reply(event,
                    f"大厅 {lb} 命中 {len(hits)} 场，请指定其一:\n"
                    + "\n".join(self._th_hit_lines(hits))
                )
                return
            async for text in self._wg_begin(session, hits[0]):
                yield self._reply(event, text)
            return

        # 玩家名
        hits = self._th_match(session, await self._th_recent_games(), name=arg)
        if not hits:
            yield self._reply(event,
                f"未找到「{arg}」正在进行的对局。天凤昵称区分大小写。\n"
                "也可用 wg <四位码|8位WG码|大厅码> 指定。"
            )
            return
        if len(hits) > 1:
            yield self._reply(event,
                f"「{arg}」有 {len(hits)} 场进行中，请指定其一:\n"
                + "\n".join(self._th_hit_lines(hits))
            )
            return
        async for text in self._wg_begin(session, hits[0]):
            yield self._reply(event, text)

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
                yield self._reply(event,
                    f"比赛号 {arg} 未查到对局。比赛号是客户端里的数字 ID，"
                    "且只列本账号参加过的比赛。")
                return
        else:
            contest = await self._ms_contest_games(force=True)
        if not ranked and not contest:
            err = self._ms_client.last_error or "未登录/列表为空"
            yield self._reply(event,
                f"未拉到雀魂直播列表（{err}）。\n"
                "订阅的玩家开局仍会自动播报。"
            )
            return
        session = str(event.session)
        sub = self._get_ms_sub(session)
        accounts = set(sub.get("accounts") or [])
        # 查比赛号就是为了拿四位码去 wg，顺手登记，省一次 msaddc
        note = self._ms_register_contest(sub, int(arg)) if by_id else ""
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
        if note:
            lines.append(note)
        yield self._reply(event, "\n".join(lines))

    def _ms_register_contest(self, sub: dict, cid: int) -> str:
        """msls <比赛号> 查到对局后登记这个比赛场，返回给用户的提示语。"""
        reg = sub.setdefault("contests", [])
        if cid in reg:
            return ""
        if len(self._ms_contest_ids()) >= MS_CONTEST_LIMIT:
            return (f"登记已满 {MS_CONTEST_LIMIT} 场，未登记比赛 {cid}；"
                    "可先 msdelc 取消不用的场。")
        reg.append(cid)
        self._save_state()
        return f"已登记比赛 {cid}，wg m<四位码> 可跟踪该场对局；不需要时发 msdelc {cid}。"

    @filter.command("msaddc")
    async def msadd_contest(self, event: AstrMessageEvent, contest_id: str = ""):
        """msaddc <比赛号>：登记比赛场，wg 可跟踪，订阅玩家在该场开局会播报。"""
        cid = (contest_id or "").strip().lstrip("#")
        if not cid.isdigit():
            yield self._reply(event,
                "用法: msaddc <比赛号>\n"
                "比赛号是雀魂比赛页里的数字，可先用 msls <比赛号> 确认能查到对局")
            return
        session = str(event.session)
        reg = self._get_ms_sub(session).setdefault("contests", [])
        if int(cid) in reg:
            yield self._reply(event, f"比赛 {cid} 已在登记中。")
            return
        if len(self._ms_contest_ids()) >= MS_CONTEST_LIMIT:
            yield self._reply(event,
                f"最多同时登记 {MS_CONTEST_LIMIT} 场比赛，先用 msdelc 取消不用的。")
            return
        if not await asyncio.to_thread(self._ms_client.ensure_connection):
            yield self._reply(event,
                "雀魂未登录，无法校验比赛号：" + (self._ms_client.last_error or "原因未知"))
            return
        try:
            rows = await asyncio.to_thread(
                self._ms_client.get_contest_rows, 3, int(cid))
        except Exception as e:
            yield self._reply(event, f"比赛 {cid} 查询失败: {e}")
            return
        reg.append(int(cid))
        self._save_state()
        # 登记表变了，按比赛号直查的那份缓存立刻作废
        self._ms_contest_reg = None
        self._ms_contest_reg_at = 0.0

        name = next((r.get("contest_full") for r in rows if r.get("contest_full")), "")
        live = [r for r in rows if r.get("live", True) and r.get("uuid")]
        lines = [f"已登记比赛 {cid}" + (f"（{name}）" if name else "")]
        if live:
            lines.append(f"当前进行中 {len(live)} 场:")
            lines += self._ms_hit_lines(live, 5)
        else:
            lines.append("当前没有进行中的对局。")
        lines.append("订阅玩家在该场开局会播报；也可 wg m<四位码> 手动跟踪。")
        yield self._reply(event, "\n".join(lines))

    @filter.command("msdelc")
    async def msdel_contest(self, event: AstrMessageEvent, contest_id: str = ""):
        """msdelc <比赛号>：取消登记的比赛场。"""
        cid = (contest_id or "").strip().lstrip("#")
        if not cid.isdigit():
            yield self._reply(event, "用法: msdelc <比赛号>")
            return
        reg = self._get_ms_sub(str(event.session)).setdefault("contests", [])
        if int(cid) not in reg:
            yield self._reply(event, f"比赛 {cid} 没有登记。")
            return
        reg.remove(int(cid))
        self._save_state()
        self._ms_contest_reg = None
        self._ms_contest_reg_at = 0.0
        yield self._reply(event, f"已取消登记比赛 {cid}。")

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
            yield self._reply(event, "当前未跟踪任何对局。")
            return
        if not wg_id:
            for s in sess.values():
                s.stop()
            for s in ms_sess.values():
                s.stop()
            self._th_sessions.pop(session, None)
            self._ms_sessions.pop(session, None)
            yield self._reply(event,
                f"已取消全部跟踪（天凤{len(sess)} 场、雀魂{len(ms_sess)} 场）。")
            return
        key = wg_id.lower()
        for mid, s in list(ms_sess.items()):
            if mid == key or s.uuid.lower() == wg_id.lower():
                s.stop()
                ms_sess.pop(mid, None)
                if not ms_sess:
                    self._ms_sessions.pop(session, None)
                yield self._reply(event, f"已取消跟踪 #{mid}（雀魂）。")
                return
        target = self._th_pick(sess, wg_id)
        if not target:
            yield self._reply(event,
                f"未找到跟踪中的对局: {wg_id}\n当前: "
                + ", ".join(list(sess.keys()) + list(ms_sess.keys()))
            )
            return
        sess[target].stop()
        sess.pop(target)
        if not sess:
            self._th_sessions.pop(session, None)
        yield self._reply(event, f"已取消跟踪 #{target}（天凤）。")

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
            yield self._reply(event, "推送已全部启用")
        elif level == "1":
            sub["enabled"] = True
            yield self._reply(event, "天凤推送已启用")
        elif level == "2":
            ms_sub["enabled"] = True
            yield self._reply(event, "雀魂推送已启用")
        else:
            sub["enabled"] = True
            ms_sub["enabled"] = True
            yield self._reply(event, "推送已全部启用")
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
            yield self._reply(event, "推送已全部禁用")
        elif level == "1":
            sub["enabled"] = False
            yield self._reply(event, "天凤推送已禁用")
        elif level == "2":
            ms_sub["enabled"] = False
            yield self._reply(event, "雀魂推送已禁用")
        else:
            sub["enabled"] = False
            ms_sub["enabled"] = False
            yield self._reply(event, "推送已全部禁用")
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
            yield self._reply(event, "当前未跟踪任何对局，无法查询比分。")
            return
        wg_id = (wg_id or "").strip()
        if wg_id:
            key = wg_id.lower()
            for mid, s in ms_sess.items():
                if mid == key or s.uuid.lower() == key:
                    yield self._reply(event, s.brief())
                    return
            target = self._th_pick(sess, wg_id)
            if not target:
                yield self._reply(event,
                    f"未找到跟踪中的对局: {wg_id}\n当前: "
                    + ", ".join(list(sess.keys()) + list(ms_sess.keys()))
                )
                return
            yield self._reply(event, sess[target].brief())
            return
        yield self._reply(event,
            "\n".join([s.brief() for s in sess.values()]
                      + [s.brief() for s in ms_sess.values()])
        )

    @filter.command("wglog")
    async def wglog(self, event: AstrMessageEvent, code: str = ""):
        """wglog <牌谱码或链接>：查那局天凤牌谱，逐局回放和了/流局与各家点数。"""
        code = (code or "").strip()
        if not code:
            yield self._reply(event,
                "用法: wglog <牌谱码或链接>\n"
                "示例: wglog 2024010100gm-0000-0000-00000000"
            )
            return
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, LogReplayParser(code).parse)
        yield self._reply(event, result)

    # ==================================================================
    #  PT 定时报告：快照采集 + 日/周/月变动榜
    # ==================================================================
    def _all_subscribed_aids(self) -> list:
        """所有会话订阅过的雀魂 AID（去重），快照按这个集合抓。"""
        aids = set()
        for sub in self._ms_subs.values():
            for a in (sub.get("accounts") or ()):
                try:
                    aids.add(int(a))
                except (TypeError, ValueError):
                    continue
        return sorted(aids)

    def _collect_snapshot(self) -> dict:
        """同步抓一轮订阅账号的段位/PT（在线程里跑）。查不到的账号跳过。"""
        players = {}
        watching = getattr(self._ms_watcher, "watching", {}) or {}
        for aid in self._all_subscribed_aids():
            try:
                stats = self._ms_client.lookup_player(account_id=aid)
            except Exception as e:
                logger.debug(f"[report] AID {aid} PT 查询失败: {e}")
                continue
            entry = report_mod.player_from_stats(aid, stats)
            if not entry:
                continue
            if not entry["name"] or entry["name"] == str(aid):
                nick = watching.get(aid) or watching.get(str(aid)) or ""
                if nick:
                    entry["name"] = str(nick)
            players[str(aid)] = entry
        return players

    async def _snapshot_now(self) -> tuple[str, dict]:
        """抓一轮快照并存盘，返回 (今天日期, history)。"""
        players = await asyncio.to_thread(self._collect_snapshot)
        history = report_mod.load_history(self._history_file)
        today = report_mod.today_key()
        report_mod.record_snapshot(history, today, players)
        report_mod.prune(history, today)
        if not report_mod.save_history(self._history_file, history):
            logger.warning("[report] 快照落盘失败")
        return today, history

    def _report_text(self, label: str, days: int, history: dict,
                     today: str, aids=None) -> str:
        """生成某一周期的变动榜文本。"""
        changes = report_mod.compute_changes(history, today, days, aids)
        baseline = report_mod.pick_baseline(history, today, days)
        return report_mod.format_report(label, changes, baseline, today)

    def _report_specs(self, today, history, aids, week: bool, month: bool) -> list:
        """构造日/周/月报告的数据（每份一份 dict，供文本/图片共用）。"""
        specs = [("日报", 1)]
        if week:
            specs.append(("周报", 7))
        if month:
            specs.append(("月报", 30))
        out = []
        for label, days in specs:
            out.append({
                "label": label,
                "changes": report_mod.compute_changes(history, today, days, aids),
                "baseline": report_mod.pick_baseline(history, today, days),
                "today": today,
            })
        return out

    def _report_image(self, specs: list, tag: str) -> str:
        """把报告渲染成一张图片，返回路径；失败返回空串（调用方回退文字）。"""
        try:
            # 用完整 session 的哈希而不是截尾群号：不同平台实例的同一群号
            # 不能共用一张图，否则定时报告会互相覆盖、群里可能收到别群榜单
            digest = hashlib.sha1(str(tag).encode("utf-8")).hexdigest()[:16]
            path = os.path.join(self._data_dir, f"pt_report_{digest}.png")
            tmp = path + ".tmp"
            if not report_image.render_report_png(specs, tmp):
                return ""
            os.replace(tmp, path)   # 原子落盘，避免并发时读到半张图
            return path
        except Exception as e:
            logger.warning(f"[report] 报告图片生成失败: {type(e).__name__}: {e}")
            return ""

    async def _run_report(self, manual: bool = False, session: str = "") -> str:
        """抓快照 + 逐会话推送日/周/月。默认推图片，出图失败回退文字。

        manual=True 且传了 session 时，榜单只取该群订阅的账号（按群隔离）；
        未传 session 的手动调用（旧接口兼容）仍用全量账号。
        返回值：手动模式且出图成功时是「_PT_IMG_MARK+图片路径」，指令据此发图；
        否则是文字摘要（定时模式返回完整文本，仅用于日志）。
        """
        aids_all = self._all_subscribed_aids()
        if not aids_all:
            return "没有订阅任何雀魂账号，无法生成 PT 报告。"

        # 手动触发按群隔离：只统计发起会话自己订阅的账号
        aids_manual = aids_all
        if manual and session:
            sub = self._ms_subs.get(session) or {}
            aids_manual = []
            for a in (sub.get("accounts") or ()):
                try:
                    aids_manual.append(int(a))
                except (TypeError, ValueError):
                    continue
            if not aids_manual:
                return "本群没有订阅任何雀魂账号，无法生成 PT 报告。"

        today, history = await self._snapshot_now()
        now = datetime.datetime.now()
        week = now.weekday() == 0 or manual
        month = now.day == 1 or manual

        # 手动：按本群账号出一张图；指令拿到路径后发图
        if manual:
            specs = self._report_specs(today, history, aids_manual, True, True)
            img = self._report_image(specs, session or "manual")
            if img:
                return self._PT_IMG_MARK + img
            return "\n\n".join(
                report_mod.format_report(s["label"], s["changes"], s["baseline"], today)
                for s in specs)

        # 定时：每个会话按自己的订阅账号出图，发图失败/出图失败都回退文字
        for session, sub in list(self._ms_subs.items()):
            if not sub.get("enabled", True) or not (sub.get("accounts") or ()):
                continue
            aids = sub.get("accounts") or ()
            specs = self._report_specs(today, history, aids, week, month)
            img = self._report_image(specs, session)
            sent = False
            if img:
                sent = await self._push_image_ok(session, img)
            if not sent:
                text = "\n\n".join(
                    report_mod.format_report(s["label"], s["changes"], s["baseline"], today)
                    for s in specs)
                try:
                    await self._push(session, text)
                    sent = True
                except Exception as e:
                    logger.warning(f"[report] 推送失败 {session}: {e}")
            if not sent:
                logger.warning(f"[report] 会话推送未成功: {session}")
        logger.info("[report] 已推送 PT 日报图片（周报/月报按日期附带）")
        return "\n\n".join(
            report_mod.format_report(s["label"], s["changes"], s["baseline"], today)
            for s in self._report_specs(today, history, aids_all, week, month))

    async def _push_image_ok(self, session: str, path: str) -> bool:
        """发图并返回是否成功（供报告回退文字判断）。"""
        if not os.path.isfile(path):
            return False
        if self._platform_state(session) == "disabled":
            self._skip_dead_session(session)
            return False
        try:
            ok = await self.context.send_message(
                session, MessageChain([Image.fromFileSystem(path)]))
            if ok:
                self._push_dead.discard(session)
            return bool(ok)
        except Exception as e:
            logger.warning(f"[report] 发图失败: {type(e).__name__}: {e}")
            return False

    async def _report_loop(self) -> None:
        """每天在配置时刻抓一次快照并推送；同一天只发一次。"""
        logger.info("[report] 定时报告任务已启动")
        try:
            while True:
                now = datetime.datetime.now()
                target = now.replace(hour=self._report_hour,
                                     minute=self._report_minute, second=0, microsecond=0)
                if now >= target:
                    target += datetime.timedelta(days=1)
                wait = (target - now).total_seconds()
                await asyncio.sleep(max(1.0, wait))
                day = report_mod.today_key()
                if day == self._report_last_day:
                    continue
                self._report_last_day = day
                try:
                    await self._run_report(manual=False)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[report] 定时报告执行失败: {e}")
        except asyncio.CancelledError:
            logger.info("[report] 定时报告任务已停止")

    @filter.command("report")
    async def report(self, event: AstrMessageEvent):
        """report：立即抓一次 PT 快照并出日/周/月变动榜（不需开启定时）。"""
        yield self._reply(event, "正在抓取订阅账号 PT 快照，请稍候…")
        try:
            text = await self._run_report(manual=True, session=str(event.session))
        except Exception as e:
            yield self._reply(event, f"PT 报告生成失败: {e}")
            return
        if text.startswith(self._PT_IMG_MARK):
            img_path = text[len(self._PT_IMG_MARK):]
            if os.path.isfile(img_path):
                yield event.image_result(img_path)
                return
            yield self._reply(event, "PT 报告图片生成后丢失，请重试 report")
            return
        yield self._reply(event, text)

    async def terminate(self):
        # 热重载时旧实例的登录/补昵称任务要停掉，否则它会继续退避重试并刷日志
        if self._hydrate_task and not self._hydrate_task.done():
            self._hydrate_task.cancel()
        if self._report_task and not self._report_task.done():
            self._report_task.cancel()
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
