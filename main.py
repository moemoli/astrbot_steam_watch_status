from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import Provider
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from .steam_api import SteamApi
from .steam_render import SteamRenderer
from .steam_store import SteamStateStore


@register("astrbot_steam_watch_status", "moemoli", "Steam 状态监控插件", "0.0.1")
class SteamWatch(Star):
    _global_poll_task: asyncio.Task | None = None
    _default_llm_comment_prompt = (
        "你是游戏群里的简短播报助手。"
        "玩家 {display_name} 刚结束《{game_name}》；{duration_text}。"
        "请给一句 8~24 字中文评价，语气自然，不要 emoji，不要引号。"
    )

    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.config = config or {}
        self.steam_web_api_key = str((self.config or {}).get("steam_web_api_key", "")).strip()
        self.steamgriddb_api_key = str((self.config or {}).get("steamgriddb_api_key", "")).strip()
        self.llm_provider_id = str((self.config or {}).get("llm_provider_id", "")).strip()
        self.llm_comment_prompt = str((self.config or {}).get("llm_comment_prompt", "")).strip()
        self.poll_interval_sec = self._parse_poll_interval_sec(
            (self.config or {}).get("poll_interval_sec", "60")
        )
        self._store = SteamStateStore(
            Path(get_astrbot_plugin_data_path()) / "astrbot_steam_watch_status"
        )
        self._api = SteamApi(self.steam_web_api_key, self.steamgriddb_api_key)
        self._renderer = SteamRenderer(self._store.cards_dir())

        self._lock = asyncio.Lock()
        self._stop = False
        self._poll_task: asyncio.Task | None = None
        self._http: aiohttp.ClientSession | None = None

        self._bindings: list[dict] = []
        self._game_subscriptions: list[dict] = []

    async def initialize(self):
        if self._poll_task and not self._poll_task.done():
            logger.warning("steam watch poll task already running on current instance, skip re-initialize")
            return

        if SteamWatch._global_poll_task and not SteamWatch._global_poll_task.done():
            logger.warning("found existing steam watch poll task, cancelling stale task before starting new one")
            SteamWatch._global_poll_task.cancel()
            try:
                await SteamWatch._global_poll_task
            except BaseException:
                pass
            SteamWatch._global_poll_task = None

        self._ensure_data_dir()
        await self._load_state()
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "astrbot-steam-watch-status/0.0.1"},
            trust_env=True,
        )
        self._api.http = self._http
        self._stop = False
        self._poll_task = asyncio.create_task(self._poll_loop())
        SteamWatch._global_poll_task = self._poll_task

    async def terminate(self):
        self._stop = True
        task = self._poll_task
        self._poll_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        if SteamWatch._global_poll_task is task:
            SteamWatch._global_poll_task = None

        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None
        self._api.http = None

    @filter.command("steam")
    async def steam(self, event: AstrMessageEvent, args: GreedyStr = GreedyStr()):
        raw = (str(args) or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：\n"
                "/steam 绑定 [好友码/64位id/好友链接/资料链接]\n"
                "/steam 订阅 [游戏链接/游戏id/游戏名称]"
            )
            return

        parts = raw.split(maxsplit=1)
        action = parts[0].strip()
        payload = parts[1].strip() if len(parts) > 1 else ""

        if action in {"绑定", "订阅","bind", "subscribe", "sub"} and not payload:
            payload = self._extract_payload_from_message(event, action)

        if action in {"绑定", "bind"}:
            msg = await self._handle_bind(event, payload)
            yield event.plain_result(msg)
            return
        if action in {"订阅", "subscribe","sub"}:
            msg = await self._handle_subscribe_game(event, payload)
            yield event.plain_result(msg)
            return

        yield event.plain_result(
            "未知子命令。可用：绑定、订阅\n"
            "示例：/steam 绑定 7656119xxxxxxxxxx\n"
            "示例：/steam 订阅 https://store.steampowered.com/app/730/"
        )

    def _extract_payload_from_message(self, event: AstrMessageEvent, action: str) -> str:
        msg = (event.get_message_str() or "").strip()
        if not msg:
            return ""

        patterns = [
            rf"^\s*/?steam\s+{re.escape(action)}\s+(.+?)\s*$",
            rf"^\s*steam\s+{re.escape(action)}\s+(.+?)\s*$",
            rf"^\s*{re.escape(action)}\s+(.+?)\s*$",
        ]
        for p in patterns:
            m = re.match(p, msg, re.IGNORECASE)
            if m:
                return (m.group(1) or "").strip()
        return ""

    async def _handle_bind(self, event: AstrMessageEvent, raw_target: str) -> str:
        if not event.get_group_id():
            return "请在群聊中执行绑定，才能将 Steam 状态推送到对应群。"
        if not raw_target:
            return "用法：/steam 绑定 [好友码/64位id/好友链接/资料链接]"
        if not self.steam_web_api_key:
            return "未配置 Steam Web API Key，请先在插件配置中填写。"

        await self._ensure_http_client()

        steamid64 = await self._resolve_steamid64(raw_target)
        if not steamid64:
            return "无法识别该 Steam 标识，请检查输入。"

        player = await self._fetch_player_summary(steamid64)
        if not player:
            return (
                "获取玩家信息失败。请确认：\n"
                "1) Steam Web API Key 可用且已去除首尾空格；\n"
                "2) 目标账号资料可公开读取；\n"
                "3) 网络可访问 api.steampowered.com。"
            )

        state, appid, game_name = self._extract_player_state(player)
        now = int(time.time())
        sender_name = event.get_sender_name() or "未知昵称"
        platform = event.get_platform_name() or "unknown"
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()

        record = {
            "id": uuid.uuid4().hex,
            "platform": platform,
            "platform_id": event.get_platform_id(),
            "session": event.unified_msg_origin,
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "steamid64": steamid64,
            "steam_name": str(player.get("personaname") or steamid64),
            "avatar_url": str(player.get("avatarfull") or ""),
            "last_state": state,
            "last_appid": appid,
            "last_game_name": game_name,
            "in_game_since_ts": now if state == "in_game" and appid > 0 else 0,
            "last_change_ts": now,
            "created_ts": now,
        }

        async with self._lock:
            replaced = False
            for idx, old in enumerate(self._bindings):
                if (
                    str(old.get("platform")) == platform
                    and str(old.get("group_id")) == group_id
                    and str(old.get("sender_id")) == sender_id
                ):
                    record["id"] = str(old.get("id") or record["id"])
                    self._bindings[idx] = record
                    replaced = True
                    break
            if not replaced:
                self._bindings.append(record)
            await self._save_state_unlocked()

        state_text = self._state_text(state)
        msg = (
            f"绑定成功：{record['steam_name']} -> 群成员 {sender_name}({sender_id})\n"
            f"绑定群：{group_id}\n"
            f"当前状态：{state_text}"
        )
        if state == "in_game" and appid:
            msg += f"（{game_name}）"
        return msg

    async def _handle_subscribe_game(self, event: AstrMessageEvent, raw_game: str) -> str:
        if not event.get_group_id():
            return "请在群聊中执行订阅，游戏更新会推送到该群。"
        if not raw_game:
            return "用法：/steam 订阅 [游戏链接/游戏id/游戏名称]"

        await self._ensure_http_client()

        app = await self._resolve_app(raw_game)
        if not app:
            return "无法解析游戏，请输入正确的游戏链接、AppID 或游戏名称。"

        platform = event.get_platform_name() or "unknown"
        group_id = event.get_group_id()
        now = int(time.time())
        latest_gid = await self._fetch_latest_news_gid(app["appid"])

        rec = {
            "id": uuid.uuid4().hex,
            "platform": platform,
            "group_id": group_id,
            "session": event.unified_msg_origin,
            "appid": app["appid"],
            "game_name": app["name"],
            "store_url": app["url"],
            "last_news_gid": latest_gid,
            "created_ts": now,
        }

        async with self._lock:
            for old in self._game_subscriptions:
                if (
                    str(old.get("platform")) == platform
                    and str(old.get("group_id")) == group_id
                    and int(old.get("appid") or 0) == int(rec["appid"])
                ):
                    return f"此群已订阅：{rec['game_name']} (AppID: {rec['appid']})"
            self._game_subscriptions.append(rec)
            await self._save_state_unlocked()

        return f"订阅成功：{rec['game_name']} (AppID: {rec['appid']})\n后续该游戏有新更新公告时会在本群推送。"

    async def _ensure_http_client(self) -> None:
        if self._http and not self._http.closed:
            self._api.http = self._http
            return
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "astrbot-steam-watch-status/0.0.1"},
            trust_env=True,
        )
        self._api.http = self._http

    async def _poll_loop(self) -> None:
        try:
            while not self._stop:
                try:
                    await self._poll_player_status_once()
                    await self._poll_game_news_once()
                    await asyncio.sleep(self.poll_interval_sec)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning(f"steam watch poll error: {exc!s}")
                    await asyncio.sleep(20)
        finally:
            current = asyncio.current_task()
            if SteamWatch._global_poll_task is current:
                SteamWatch._global_poll_task = None

    async def _poll_player_status_once(self) -> None:
        async with self._lock:
            bindings = [dict(x) for x in self._bindings if isinstance(x, dict)]

        if not bindings:
            return

        await self._refresh_group_nicknames_for_bindings(bindings)

        ids = []
        for b in bindings:
            sid = str(b.get("steamid64") or "").strip()
            if sid:
                ids.append(sid)

        players = await self._fetch_player_summaries(ids)
        if not players:
            return

        updates: dict[str, dict] = {}
        changes_by_session: dict[str, list[dict]] = {}
        for b in bindings:
            bid = str(b.get("id") or "").strip()
            sid = str(b.get("steamid64") or "").strip()
            if not bid or not sid:
                continue

            player = players.get(sid)
            if not player:
                updates[bid] = b
                continue

            steam_name = str(player.get("personaname") or sid)
            avatar = str(player.get("avatarfull") or "")
            new_state, new_appid, new_game = self._extract_player_state(player)
            old_state = str(b.get("last_state") or "")
            old_appid = int(b.get("last_appid") or 0)

            b["steam_name"] = steam_name
            b["avatar_url"] = avatar

            if not old_state:
                b["last_state"] = new_state
                b["last_appid"] = new_appid
                b["last_game_name"] = new_game
                b["last_change_ts"] = int(time.time())
                updates[bid] = b
                continue

            changed = new_state != old_state or (
                new_state == "in_game" and int(new_appid or 0) != int(old_appid or 0)
            )
            if changed:
                now_ts = int(time.time())
                old_game_name = str(b.get("last_game_name") or "")
                old_in_game_since_ts = int(b.get("in_game_since_ts") or b.get("last_change_ts") or now_ts)
                session_secs = 0
                if old_state == "in_game" and new_state in {"online", "offline"}:
                    session_secs = max(0, now_ts - old_in_game_since_ts)

                session = str(b.get("session") or "").strip()
                if session:
                    changes_by_session.setdefault(session, []).append(
                        {
                            "steam_name": steam_name,
                            "group_nick": str(b.get("sender_name") or "未知成员"),
                            "steamid64": sid,
                            "avatar_url": avatar,
                            "old_state": old_state,
                            "old_appid": int(old_appid or 0),
                            "old_game": old_game_name,
                            "new_state": new_state,
                            "new_appid": int(new_appid or 0),
                            "new_game": new_game or (f"App {new_appid}" if new_appid else ""),
                            "session_secs": session_secs,
                        }
                    )
                b["last_state"] = new_state
                b["last_appid"] = new_appid
                b["last_game_name"] = new_game
                b["last_change_ts"] = now_ts
                if new_state == "in_game" and new_appid > 0:
                    b["in_game_since_ts"] = now_ts
                elif old_state == "in_game" and new_state in {"online", "offline"}:
                    b["in_game_since_ts"] = 0

            updates[bid] = b

        for session, changes in changes_by_session.items():
            await self._push_group_state_changes(session, changes)

        if updates:
            async with self._lock:
                self._bindings = [
                    updates.get(str(old.get("id") or ""), old)
                    for old in self._bindings
                    if isinstance(old, dict)
                ]
                await self._save_state_unlocked()

    async def _refresh_group_nicknames_for_bindings(self, bindings: list[dict]) -> None:
        grouped: dict[tuple[str, str, str], list[dict]] = {}
        for binding in bindings:
            platform = str(binding.get("platform") or "")
            platform_id = str(binding.get("platform_id") or "")
            group_id = str(binding.get("group_id") or "")
            if not group_id:
                continue
            key = (platform, platform_id, group_id)
            grouped.setdefault(key, []).append(binding)

        for (platform, platform_id, group_id), members in grouped.items():
            nickname_map = await self._fetch_group_nickname_map(
                platform=platform,
                platform_id=platform_id,
                group_id=group_id,
            )
            if not nickname_map:
                continue

            for binding in members:
                sender_id = str(binding.get("sender_id") or "")
                latest = nickname_map.get(sender_id)
                if latest:
                    normalized = latest.strip()
                    if normalized:
                        binding["sender_name"] = normalized

    async def _fetch_group_nickname_map(
        self,
        *,
        platform: str,
        platform_id: str,
        group_id: str,
    ) -> dict[str, str]:
        if platform != "aiocqhttp":
            return {}

        try:
            platform_inst = None
            if platform_id:
                platform_inst = self.context.get_platform_inst(platform_id)
            if not platform_inst:
                platform_inst = self.context.get_platform("aiocqhttp")
            if not platform_inst or not hasattr(platform_inst, "bot"):
                return {}

            bot = getattr(platform_inst, "bot", None)
            if not bot:
                return {}

            result = await bot.call_action(
                action="get_group_member_list",
                group_id=int(group_id),
                no_cache=False,
            )
        except Exception as exc:
            logger.debug(f"fetch group member list failed: {exc!s}")
            return {}

        data = result
        if isinstance(result, dict) and "data" in result:
            data = result.get("data")
        if not isinstance(data, list):
            return {}

        out: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id") or "").strip()
            if not user_id:
                continue
            card = str(item.get("card") or "").strip()
            nick = str(item.get("nickname") or item.get("nick") or "").strip()
            name = card or nick
            if name:
                out[user_id] = name
        return out

    async def _push_group_state_changes(self, session: str, changes: list[dict]) -> None:
        if not session or not changes:
            return

        enriched_list = await asyncio.gather(
            *(self._build_change_entry(c, session=session) for c in changes),
            return_exceptions=True,
        )

        enriched: list[dict] = []
        for item in enriched_list:
            if isinstance(item, Exception) or not isinstance(item, dict):
                continue
            enriched.append(item)

        if not enriched:
            return

        card = await self._render_batch_status_card(enriched)
        if not card:
            return
        await self.context.send_message(session, MessageChain().file_image(card))

    async def _build_change_entry(self, change: dict, *, session: str) -> dict:
        steam_name = str(change.get("steam_name") or "未知")
        group_nick = str(change.get("group_nick") or "未知成员")
        display_name = f"{steam_name}({group_nick})"
        old_state = str(change.get("old_state") or "")
        new_state = str(change.get("new_state") or "")
        old_appid = int(change.get("old_appid") or 0)
        old_game = str(change.get("old_game") or "")
        appid = int(change.get("new_appid") or 0)
        game_name = str(change.get("new_game") or "")
        steamid64 = str(change.get("steamid64") or "")
        avatar_url = str(change.get("avatar_url") or "")
        session_secs = int(change.get("session_secs") or 0)

        avatar = await self._fetch_image_pil(avatar_url)
        cover = None
        playtime_text = ""
        comment_text = ""

        if new_state == "in_game" and appid > 0:
            playtime_text = await self._fetch_playtime_text(steamid64=steamid64, appid=appid)
            cover = await self._fetch_cover_image(appid)
            status_desc = f"开始游戏：{game_name}"
        elif old_state == "in_game" and new_state in {"online", "offline"}:
            if old_appid > 0:
                cover = await self._fetch_cover_image(old_appid)
            if old_game:
                game_name = old_game
            if session_secs > 0:
                playtime_text = f"本次游戏时长：{self._format_duration(session_secs)}"
            else:
                playtime_text = "本次游戏时长：未知"
            comment_text = await self._generate_llm_comment(
                session=session,
                display_name=display_name,
                game_name=game_name or "该游戏",
                duration_text=playtime_text,
            )
            status_desc = f"结束游戏 -> {self._state_text(new_state)}"
        else:
            status_desc = f"{self._state_text(old_state)} -> {self._state_text(new_state)}"

        return {
            "display_name": display_name,
            "status_desc": status_desc,
            "game_name": game_name,
            "playtime_text": playtime_text,
            "comment_text": comment_text,
            "avatar": avatar,
            "cover": cover,
            "new_state": new_state,
        }

    async def _generate_llm_comment(
        self,
        *,
        session: str,
        display_name: str,
        game_name: str,
        duration_text: str,
    ) -> str:
        if not session:
            return ""
        provider = self._resolve_comment_provider(session)
        if not provider or not isinstance(provider, Provider):
            return ""

        prompt = self._build_llm_comment_prompt(
            display_name=display_name,
            game_name=game_name,
            duration_text=duration_text,
        )
        try:
            resp = await asyncio.wait_for(provider.text_chat(prompt=prompt), timeout=15)
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = re.sub(r"\s+", " ", text)
            text = text.replace("\n", " ").strip(" \"'“”‘’")
            if len(text) > 28:
                text = text[:28].rstrip("，。,.!?！？") + "。"
            return text
        except Exception as exc:
            logger.debug(f"llm comment generate failed: {exc!s}")
            return ""

    def _resolve_comment_provider(self, session: str):
        if self.llm_provider_id:
            try:
                provider = self.context.get_provider_by_id(self.llm_provider_id)
                if provider is not None:
                    return provider
            except Exception as exc:
                logger.debug(f"resolve llm provider by id failed: {exc!s}")
        return self.context.get_using_provider(umo=session)

    def _build_llm_comment_prompt(
        self,
        *,
        display_name: str,
        game_name: str,
        duration_text: str,
    ) -> str:
        template = self.llm_comment_prompt or self._default_llm_comment_prompt
        payload = {
            "display_name": display_name,
            "game_name": game_name,
            "duration_text": duration_text,
        }
        try:
            return template.format(**payload)
        except Exception:
            return self._default_llm_comment_prompt.format(**payload)

    @staticmethod
    def _format_duration(seconds: int) -> str:
        total = max(0, int(seconds))
        hours, rem = divmod(total, 3600)
        minutes, sec = divmod(rem, 60)
        if hours > 0:
            return f"{hours}小时{minutes}分"
        if minutes > 0:
            return f"{minutes}分{sec}秒"
        return f"{sec}秒"

    async def _poll_game_news_once(self) -> None:
        async with self._lock:
            subs = [dict(x) for x in self._game_subscriptions if isinstance(x, dict)]
        if not subs:
            return

        updates: dict[str, dict] = {}
        for s in subs:
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            appid = int(s.get("appid") or 0)
            if appid <= 0:
                updates[sid] = s
                continue

            latest = await self._fetch_latest_news(appid)
            if not latest:
                updates[sid] = s
                continue

            old_gid = str(s.get("last_news_gid") or "")
            new_gid = str(latest.get("gid") or "")
            if old_gid and new_gid and old_gid != new_gid:
                title = str(latest.get("title") or "新公告")
                url = str(latest.get("url") or "")
                game_name = str(s.get("game_name") or f"App {appid}")
                author = str(latest.get("author") or "Steam News")
                contents = str(latest.get("contents") or "")
                date_ts = int(latest.get("date") or 0)
                card = await self._render_news_card(
                    appid=appid,
                    game_name=game_name,
                    title=title,
                    author=author,
                    date_ts=date_ts,
                    contents=contents,
                )

                text = f"[Steam更新] {game_name}\n{title}"
                if url:
                    text += f"\n{url}"
                chain = MessageChain().message(text)
                if card:
                    chain.file_image(card)
                await self.context.send_message(str(s.get("session") or ""), chain)

            s["last_news_gid"] = new_gid or old_gid
            updates[sid] = s

        if updates:
            async with self._lock:
                self._game_subscriptions = [
                    updates.get(str(old.get("id") or ""), old)
                    for old in self._game_subscriptions
                    if isinstance(old, dict)
                ]
                await self._save_state_unlocked()

    async def _resolve_steamid64(self, raw: str) -> str | None:
        return await self._api.resolve_steamid64(raw)

    async def _fetch_player_summary(self, steamid64: str) -> dict | None:
        return await self._api.fetch_player_summary(steamid64)

    async def _fetch_player_summaries(self, steamids: list[str]) -> dict[str, dict]:
        return await self._api.fetch_player_summaries(steamids)

    def _extract_player_state(self, player: dict) -> tuple[str, int, str]:
        return self._api.extract_player_state(player)

    def _state_text(self, state: str) -> str:
        return self._api.state_text(state)

    async def _fetch_playtime_text(self, steamid64: str, appid: int) -> str:
        return await self._api.fetch_playtime_text(steamid64, appid)

    async def _resolve_app(self, raw: str) -> dict | None:
        return await self._api.resolve_app(raw)

    async def _fetch_latest_news_gid(self, appid: int) -> str:
        return await self._api.fetch_latest_news_gid(appid)

    async def _fetch_latest_news(self, appid: int) -> dict | None:
        return await self._api.fetch_latest_news(appid)

    async def _render_playing_card(
        self,
        *,
        steam_name: str,
        group_name: str,
        game_name: str,
        avatar_url: str,
        appid: int,
        playtime_text: str,
    ) -> str | None:
        cover = await self._fetch_cover_image(appid)
        avatar = await self._fetch_image_pil(avatar_url)
        return await self._renderer.render_playing_card(
            steam_name=steam_name,
            group_name=group_name,
            game_name=game_name,
            playtime_text=playtime_text,
            cover=cover,
            avatar=avatar,
        )

    async def _render_batch_status_card(self, entries: list[dict]) -> str | None:
        return await self._renderer.render_batch_status_card(entries)

    async def _render_news_card(
        self,
        *,
        appid: int,
        game_name: str,
        title: str,
        author: str,
        date_ts: int,
        contents: str,
    ) -> str | None:
        cover = await self._fetch_cover_image(appid)
        return await self._renderer.render_news_card(
            appid=appid,
            game_name=game_name,
            title=title,
            author=author,
            date_ts=date_ts,
            contents=contents,
            cover=cover,
        )

    async def _fetch_cover_image(self, appid: int):
        return await self._api.fetch_cover_image(appid)

    async def _fetch_image_pil(self, url: str):
        return await self._api.fetch_image_pil(url)

    def _ensure_data_dir(self) -> None:
        self._store.ensure_data_dir()

    async def _load_state(self) -> None:
        self._bindings, self._game_subscriptions = await self._store.load_state()

    async def _save_state_unlocked(self) -> None:
        await self._store.save_state(self._bindings, self._game_subscriptions)

    @staticmethod
    def _parse_poll_interval_sec(raw: object) -> int:
        try:
            val = int(str(raw).strip())
        except Exception:
            val = 60
        if val < 10:
            return 10
        return val
