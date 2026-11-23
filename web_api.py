# -*- coding: utf-8 -*-
"""AstrBot 插件管理页（Plugin Pages）后端 API。

参照 steam_status_monitor_V3 的做法：用 ``context.register_web_api`` 把
一组路由注册进 AstrBot 自带 Dashboard，前端页面放在 ``pages/`` 下，通过
bridge-sdk 的 ``apiGet``/``apiPost`` 调用这些路由。

设计原则：
- 所有读写都复用主插件实例（TenhouWatch）已有的状态与方法，不在本模块
  重新实现查询/解析/落盘逻辑，保证网页操作与群指令口径完全一致。
- 只暴露「状态总览 / 订阅管理 / 定制文案 / 报告配置 / 插件配置」五类接口。
- 敏感配置（账号密码/令牌）在读取接口里打码，写入时若收到打码值则跳过，
  避免把 ``******`` 写回配置。
"""

from __future__ import annotations

import asyncio
import base64
import os
from functools import wraps

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_tenhou_majsoul_watch"

# 读取时打码、写入时忽略打码占位符的敏感键（点对配置扁平 key）
_SECRET_KEYS = {"password", "token"}
_MASK = "******"


def _mask_value(key: str, value):
    """对敏感配置打码；保留后 4 位便于辨认，长度不足则全遮。"""
    if key not in _SECRET_KEYS:
        return value
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 4:
        return _MASK
    return _MASK + s[-4:]


class _PluginPageRequest:
    """暴露 api 处理器用到的 aiohttp request 子集（与 Steam 插件一致）。"""

    @property
    def query(self):
        return request.query or {}

    @property
    def match_info(self):
        return request.path_params or {}

    async def json(self):
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload


def safe_api(handler):
    """统一捕获异常，返回 Plugin Pages 期望的响应对象。"""

    @wraps(handler)
    async def wrapper(*_args, **_kwargs):
        try:
            return await handler(_PluginPageRequest())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[WebUI] API 异常 {handler.__name__}: {e}", exc_info=True)
            return error_response(str(e), status_code=500)

    return wrapper


class WatchWebAPI:
    """注册到 AstrBot Dashboard 的后端 API。"""

    def __init__(self, plugin):
        self.plugin = plugin

    # ------------------------------------------------------------------
    #  路由注册
    # ------------------------------------------------------------------
    def register_routes(self, context):
        def r(method, path, handler):
            context.register_web_api(
                f"/{PLUGIN_NAME}{path}",
                safe_api(handler),
                [method],
                f"Tenhou/Majsoul Watch Page: {handler.__name__}",
            )

        # 状态总览
        r("GET", "/overview", self._api_overview)
        # 订阅管理
        r("GET", "/subscriptions", self._api_subscriptions)
        r("POST", "/th/add", self._api_th_add)
        r("POST", "/th/del", self._api_th_del)
        r("POST", "/th/filter", self._api_th_filter)
        r("POST", "/th/toggle", self._api_th_toggle)
        r("POST", "/ms/add", self._api_ms_add)
        r("POST", "/ms/del", self._api_ms_del)
        r("POST", "/ms/filter", self._api_ms_filter)
        r("POST", "/ms/toggle", self._api_ms_toggle)
        r("POST", "/ms/contest/add", self._api_ms_contest_add)
        r("POST", "/ms/contest/del", self._api_ms_contest_del)
        # 定制文案
        r("GET", "/custom/list", self._api_custom_list)
        r("POST", "/custom/set", self._api_custom_set)
        r("POST", "/custom/del", self._api_custom_del)
        # 报告
        r("GET", "/report/config", self._api_report_config)
        r("POST", "/report/config", self._api_report_config_update)
        r("POST", "/report/run", self._api_report_run)
        r("GET", "/report/history", self._api_report_history)
        # 配置
        r("GET", "/config", self._api_config)
        r("POST", "/config", self._api_config_update)

    # ------------------------------------------------------------------
    #  小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _sessions(p) -> list[str]:
        """所有出现过的会话（天凤/雀魂订阅 + 定制文案）。"""
        seen = set()
        for d in (getattr(p, "_subs", {}), getattr(p, "_ms_subs", {}),
                  getattr(p, "_custom_text", {})):
            seen.update((d or {}).keys())
        return sorted(seen)

    @staticmethod
    def _group_label(session: str) -> str:
        """把 <平台ID>:GroupMessage:<群号> 之类的 session 精简成可读标签。"""
        parts = str(session or "").split(":")
        return parts[-1] if parts else str(session)

    # ------------------------------------------------------------------
    #  状态总览
    # ------------------------------------------------------------------
    async def _api_overview(self, request):
        p = self.plugin
        sessions = self._sessions(p)
        th_total = sum(len((s or {}).get("players", ())) for s in p._subs.values())
        ms_total = sum(len((s or {}).get("accounts", ())) for s in p._ms_subs.values())
        contest_total = sum(len((s or {}).get("contests", ())) for s in p._ms_subs.values())
        custom_total = sum(len((c or {})) for c in p._custom_text.values())

        # 雀魂登录态
        cl = p._ms_client
        if cl.authed:
            ms_login = "已登录"
        elif not cl.has_credentials:
            ms_login = "未配置账号"
        else:
            ms_login = f"未登录（{cl.last_error or '待重试'}）"

        # 当前跟踪中的对局
        th_live = sum(len(d) for d in p._th_sessions.values())
        th_auto = len(p._th_auto)
        ms_live = sum(len(d) for d in p._ms_sessions.values())
        ms_auto = len(p._ms_auto)

        return json_response({
            "th_watching": bool(getattr(p._th_watcher, "running", False)),
            "ms_watching": bool(getattr(p._ms_watcher, "running", False)),
            "ms_login": ms_login,
            "sessions": len(sessions),
            "th_players": th_total,
            "ms_accounts": ms_total,
            "contests": contest_total,
            "custom": custom_total,
            "th_live": th_live,
            "th_auto": th_auto,
            "ms_live": ms_live,
            "ms_auto": ms_auto,
            "report_enabled": bool(getattr(p, "_report_enabled", False)),
            "report_hour": getattr(p, "_report_hour", 12),
            "report_minute": getattr(p, "_report_minute", 0),
            "quota_ms": bool(getattr(p, "_ms_client", None)),
        })

    # ------------------------------------------------------------------
    #  订阅管理
    # ------------------------------------------------------------------
    async def _api_subscriptions(self, request):
        """一次性返回全部会话的天凤/雀魂订阅，前端按会话分组展示。"""
        p = self.plugin
        sessions = self._sessions(p)
        watched = getattr(p._ms_watcher, "watching", {}) or {}
        groups = []
        for sid in sessions:
            th = p._subs.get(sid) or {}
            ms = p._ms_subs.get(sid) or {}
            custom = p._custom_text.get(sid) or {}
            groups.append({
                "session": sid,
                "label": self._group_label(sid),
                "th": {
                    "players": sorted(th.get("players", ())),
                    "filters": list(th.get("filters", ())),
                    "enabled": bool(th.get("enabled", True)),
                },
                "ms": {
                    "accounts": [
                        {"aid": a, "name": watched.get(a) or str(a)}
                        for a in sorted(ms.get("accounts", ()))
                    ],
                    "filters": list(ms.get("filters", ())),
                    "enabled": bool(ms.get("enabled", True)),
                    "contests": sorted(ms.get("contests", ())),
                },
                "custom_count": len(custom),
            })
        return json_response({"groups": groups})

    async def _api_th_add(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        name = str(data.get("name") or "").strip()
        if not sid or not name:
            return error_response("session 和 name 不能为空", status_code=400)
        sub = p._get_sub(sid)
        sub["players"].add(name)
        p._th_sync_watcher()
        p._save_state()
        return json_response({"ok": True, "players": sorted(sub["players"])})

    async def _api_th_del(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        name = str(data.get("name") or "").strip()
        sub = p._get_sub(sid)
        sub["players"].discard(name)
        p._th_sync_watcher()
        p._save_state()
        return json_response({"ok": True, "players": sorted(sub["players"])})

    async def _api_th_filter(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        raw = data.get("filters") or []
        if isinstance(raw, str):
            raw = raw.split()
        sub = p._get_sub(sid)
        sub["filters"] = [str(x).strip() for x in raw if str(x).strip()]
        p._save_state()
        return json_response({"ok": True, "filters": sub["filters"]})

    async def _api_th_toggle(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        sub = p._get_sub(sid)
        sub["enabled"] = bool(data.get("enabled"))
        p._save_state()
        return json_response({"ok": True, "enabled": sub["enabled"]})

    async def _api_ms_add(self, request):
        """网页端只接受明确的 AID（数字）或 $AID，避免昵称歧义。"""
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        raw = str(data.get("aid") or "").strip().lstrip("$")
        if not sid or not raw.isdigit():
            return error_response("需要 session 和纯数字 AID", status_code=400)
        aid = int(raw)
        sub = p._get_ms_sub(sid)
        # 复用客户端解析昵称（失败也不影响订阅，只是显示成 AID）
        nickname = ""
        try:
            if getattr(p._ms_client, "authed", False):
                info = await asyncio.to_thread(p._ms_client.lookup_player, aid, None)
                nickname = (info or {}).get("nickname", "") or ""
        except Exception:
            nickname = ""
        sub["accounts"].add(aid)
        p._ms_watcher.add(aid, nickname)
        p._save_state()
        return json_response({
            "ok": True,
            "aid": aid,
            "name": nickname or str(aid),
        })

    async def _api_ms_del(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        raw = str(data.get("aid") or "").strip().lstrip("$")
        if not raw.isdigit():
            return error_response("需要纯数字 AID", status_code=400)
        aid = int(raw)
        sub = p._get_ms_sub(sid)
        if aid in sub["accounts"]:
            sub["accounts"].discard(aid)
            still = any(aid in s.get("accounts", set()) for s in p._ms_subs.values())
            if not still:
                p._ms_watcher.remove(aid)
            p._save_state()
        return json_response({"ok": True})

    async def _api_ms_filter(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        raw = data.get("filters") or []
        if isinstance(raw, str):
            raw = [] if raw.strip().lower() in ("all", "不限", "") else raw.split()
        sub = p._get_ms_sub(sid)
        sub["filters"] = [str(x).strip() for x in raw if str(x).strip()]
        p._save_state()
        return json_response({"ok": True, "filters": sub["filters"]})

    async def _api_ms_toggle(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        sub = p._get_ms_sub(sid)
        sub["enabled"] = bool(data.get("enabled"))
        p._save_state()
        return json_response({"ok": True, "enabled": sub["enabled"]})

    async def _api_ms_contest_add(self, request):
        from .main import MS_CONTEST_LIMIT
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        raw = str(data.get("contest") or "").strip()
        if not raw.isdigit():
            return error_response("比赛号必须是数字", status_code=400)
        contest = int(raw)
        sub = p._get_ms_sub(sid)
        if contest not in sub.get("contests", []):
            if len(sub.get("contests", [])) >= MS_CONTEST_LIMIT:
                return error_response(f"最多登记 {MS_CONTEST_LIMIT} 个比赛场", status_code=400)
            sub.setdefault("contests", []).append(contest)
            p._save_state()
        return json_response({"ok": True, "contests": sorted(sub.get("contests", []))})

    async def _api_ms_contest_del(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        raw = str(data.get("contest") or "").strip()
        if not raw.isdigit():
            return error_response("比赛号必须是数字", status_code=400)
        contest = int(raw)
        sub = p._get_ms_sub(sid)
        if contest in sub.get("contests", []):
            sub["contests"].remove(contest)
            p._save_state()
        return json_response({"ok": True, "contests": sorted(sub.get("contests", []))})

    # ------------------------------------------------------------------
    #  定制文案
    # ------------------------------------------------------------------
    async def _api_custom_list(self, request):
        p = self.plugin
        result = {}
        for sid, cmap in p._custom_text.items():
            if cmap:
                result[sid] = dict(cmap)
        return json_response({"custom": result})

    _NODES = ("start", "round", "big", "finish")

    async def _api_custom_set(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        name = str(data.get("name") or "").strip()
        node = str(data.get("node") or "finish").strip().lower()
        raw = data.get("text")
        # big 节点允许结构化值：{"和出":[...], "点出":[...]}（每条 {min,max,text}）
        # finish 节点允许结构化值：{"1":..., "2":..., "3":..., "4":..., "*":...}
        # 其它节点继续用字符串模板。做基础清洗，坏结构退化为空串不入库。
        if node == "big" and isinstance(raw, dict):
            text = {}
            for k in ("和出", "点出", "两者"):
                rules = raw.get(k)
                if not isinstance(rules, list):
                    continue
                clean = []
                for r in rules:
                    if not isinstance(r, dict):
                        continue
                    rt = str(r.get("text") or "").strip()
                    if not rt:
                        continue
                    clean.append({
                        "min": str(r.get("min") or "").strip(),
                        "max": str(r.get("max") or "").strip(),
                        "text": rt,
                    })
                if clean:
                    text[k] = clean
        elif node == "finish" and isinstance(raw, dict):
            text = {}
            for k in ("1", "2", "3", "4", "*"):
                v = str(raw.get(k) or "").strip()
                if v:
                    text[k] = v
        else:
            text = str(raw or "").strip()
        if node not in self._NODES:
            node = "finish"
        if not sid or not name or not text:
            return error_response("session、name、text 都不能为空", status_code=400)
        cmap = p._custom_text.setdefault(sid, {})
        entry = cmap.get(name)
        if isinstance(entry, dict):
            entry[node] = text
        else:
            upgraded = {"finish": entry} if entry else {}
            upgraded[node] = text
            cmap[name] = upgraded
        p._save_state()
        return json_response({"ok": True, "name": name, "node": node, "text": text})

    async def _api_custom_del(self, request):
        p = self.plugin
        data = await request.json()
        sid = str(data.get("session") or "")
        name = str(data.get("name") or "").strip()
        node = str(data.get("node") or "").strip().lower()
        cmap = p._custom_text.get(sid)
        if cmap and name in cmap:
            entry = cmap[name]
            if node and node in self._NODES and isinstance(entry, dict):
                entry.pop(node, None)
                if not entry:
                    del cmap[name]
            else:
                del cmap[name]
            if not cmap:
                p._custom_text.pop(sid, None)
            p._save_state()
        return json_response({"ok": True})

    # ------------------------------------------------------------------
    #  报告配置
    # ------------------------------------------------------------------
    async def _api_report_config(self, request):
        p = self.plugin
        return json_response({
            "enabled": bool(getattr(p, "_report_enabled", False)),
            "hour": getattr(p, "_report_hour", 12),
            "minute": getattr(p, "_report_minute", 0),
        })

    async def _api_report_config_update(self, request):
        """改报告开关/时刻，写回配置并即时更新运行中的调度器。"""
        p = self.plugin
        data = await request.json()
        enabled = bool(data.get("enabled", p._report_enabled))
        try:
            hour = max(0, min(23, int(data.get("hour", p._report_hour))))
            minute = max(0, min(59, int(data.get("minute", p._report_minute))))
        except (TypeError, ValueError):
            return error_response("hour/minute 必须是整数", status_code=400)

        # 写回 AstrBot 配置（插件配置页与网页保持一致）
        cfg = getattr(p, "config", None)
        if cfg is not None:
            rp = cfg.get("report")
            if not isinstance(rp, dict):
                rp = {}
                cfg["report"] = rp
            rp["enabled"] = enabled
            rp["hour"] = hour
            rp["minute"] = minute
            if hasattr(cfg, "save_config"):
                cfg.save_config()

        p._report_enabled = enabled
        p._report_hour = hour
        p._report_minute = minute
        # 调度器在 _report_loop 里按分钟轮询，配置改了会自然在下一次生效；
        # 这里只需处理「从关到开」时没有任务的情况。
        if enabled and (not getattr(p, "_report_task", None) or p._report_task.done()):
            p._report_task = asyncio.create_task(p._report_loop())
        elif not enabled and getattr(p, "_report_task", None) and not p._report_task.done():
            p._report_task.cancel()
            p._report_task = None
        return json_response({"ok": True, "enabled": enabled, "hour": hour, "minute": minute})

    async def _api_report_run(self, request):
        """手动生成一次报告（不推群）。按所选群隔离；出图后以内联图片返回。"""
        p = self.plugin
        data = await request.json()
        session = str(data.get("session") or "")
        text = await p._run_report(manual=True, session=session)
        mark = getattr(p, "_PT_IMG_MARK", "\x00PTIMG\x00")
        if text.startswith(mark):
            path = text[len(mark):]
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                return json_response({"ok": True, "image": "data:image/png;base64," + b64})
            return error_response("报告图片生成后丢失，请重试", status_code=500)
        return json_response({"ok": True, "text": text})

    async def _api_report_history(self, request):
        p = self.plugin
        history = report_load(getattr(p, "_history_file", ""))
        return json_response({"history": history})

    # ------------------------------------------------------------------
    #  配置（全部可编辑，敏感项打码）
    # ------------------------------------------------------------------
    def _config_view(self):
        p = self.plugin
        cfg = getattr(p, "config", None) or {}
        view = {}
        for key, val in cfg.items():
            if isinstance(val, dict):
                view[key] = {k: _mask_value(k, v) for k, v in val.items()}
            else:
                view[key] = val
        return view

    async def _api_config(self, request):
        return json_response({"config": self._config_view()})

    async def _api_config_update(self, request):
        """按嵌套键更新配置并落盘；收到打码占位符则跳过该敏感键。

        请求体形如 {"majsoul": {"token": "xxx"}, "report": {"hour": 20}}，
        只覆盖出现的键，未出现的保持不变。
        """
        p = self.plugin
        data = await request.json()
        cfg = getattr(p, "config", None)
        if cfg is None:
            return error_response("插件配置不可用", status_code=500)

        changed = []
        for group, fields in data.items():
            if not isinstance(fields, dict):
                continue
            target = cfg.get(group)
            if not isinstance(target, dict):
                continue
            for key, value in fields.items():
                # 敏感键：收到打码值或留空 → 不改
                if key in _SECRET_KEYS:
                    s = str(value or "")
                    if not s or s.startswith(_MASK):
                        continue
                target[key] = value
                changed.append(f"{group}.{key}")
        if changed and hasattr(cfg, "save_config"):
            cfg.save_config()
        return json_response({"ok": True, "changed": changed, "config": self._config_view()})


def report_load(path: str) -> dict:
    """读取 PT 历史快照（独立小函数，避免重复 import 主模块）。"""
    if not path:
        return {}
    try:
        import json
        import os
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[WebUI] 读取 history 失败: {e}")
        return {}
