from __future__ import annotations

import asyncio
import io
import re

import aiohttp

from astrbot.api import logger

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


STEAM_ID64_BASE = 76561197960265728


class SteamApi:
    def __init__(self, steam_web_api_key: str, steamgriddb_api_key: str):
        self.steam_web_api_key = (steam_web_api_key or "").strip()
        self.steamgriddb_api_key = (steamgriddb_api_key or "").strip()
        self.http: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession | None:
        return self.http

    async def resolve_steamid64(self, raw: str) -> str | None:
        text = self._normalize_target(raw)
        if not text:
            return None

        m = re.search(r"steamcommunity\.com/profiles/(\d{17})(?:/|$)", text, re.IGNORECASE)
        if m:
            return m.group(1)

        m = re.search(r"steamcommunity\.com/id/([^/?#]+)(?:/|$)", text, re.IGNORECASE)
        if m:
            return await self._resolve_vanity(m.group(1))

        m = re.search(r"steamcommunity\.com/addfriend/(\d+)(?:/|$)", text, re.IGNORECASE)
        if m:
            acc = int(m.group(1))
            return str(STEAM_ID64_BASE + acc)

        if "s.team/p/" in text.lower():
            from_link = await self._resolve_s_team_link(text)
            if from_link:
                return from_link

        if re.fullmatch(r"\d{17}", text):
            return text

        if re.fullmatch(r"\d{1,12}", text):
            val = int(text)
            if val > STEAM_ID64_BASE:
                return str(val)
            return str(STEAM_ID64_BASE + val)

        return await self._resolve_vanity(text)

    @staticmethod
    def _normalize_target(raw: str | None) -> str:
        text = (raw or "").strip()
        text = text.strip("\"'")
        text = text.strip("<>")
        text = text.strip()
        return text

    async def _resolve_short_link_to_steamid(self, url: str) -> str | None:
        http = self._http()
        if not http:
            return None
        try:
            async with http.get(url, allow_redirects=True) as resp:
                final_url = str(resp.url)
                page_text = await resp.text(errors="ignore")

            from_final = await self._resolve_steamid_from_any_url(final_url)
            if from_final:
                return from_final

            return await self._extract_steamid_from_text(page_text)
        except Exception:
            return None

    async def _resolve_s_team_link(self, url: str) -> str | None:
        u = self._normalize_target(url)
        out = await self._resolve_short_link_to_steamid(u)
        if out:
            return out

        m = re.search(r"(https?://s\.team/p/[A-Za-z0-9\-]+)", u, re.IGNORECASE)
        if m:
            out = await self._resolve_short_link_to_steamid(m.group(1))
            if out:
                return out
        return None

    async def _resolve_steamid_from_any_url(self, url: str) -> str | None:
        if not url:
            return None
        m = re.search(r"steamcommunity\.com/profiles/(\d{17})(?:/|$)", url, re.IGNORECASE)
        if m:
            return m.group(1)

        m = re.search(r"steamcommunity\.com/addfriend/(\d+)(?:/|$)", url, re.IGNORECASE)
        if m:
            return str(STEAM_ID64_BASE + int(m.group(1)))

        m = re.search(r"steamcommunity\.com/id/([^/?#]+)(?:/|$)", url, re.IGNORECASE)
        if m:
            return await self._resolve_vanity(m.group(1))
        return None

    async def _extract_steamid_from_text(self, text: str) -> str | None:
        if not text:
            return None
        m = re.search(r"steamcommunity\.com/profiles/(\d{17})", text, re.IGNORECASE)
        if m:
            return m.group(1)

        m = re.search(r"steamcommunity\.com/addfriend/(\d+)", text, re.IGNORECASE)
        if m:
            return str(STEAM_ID64_BASE + int(m.group(1)))

        m = re.search(r"steamcommunity\.com/id/([^/?#\"'\s]+)", text, re.IGNORECASE)
        if m:
            return await self._resolve_vanity(m.group(1))
        return None

    async def _resolve_vanity(self, vanity: str) -> str | None:
        http = self._http()
        if not vanity or not self.steam_web_api_key or not http:
            return None
        try:
            api = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
            params = {
                "key": self.steam_web_api_key,
                "vanityurl": vanity,
            }
            async with http.get(api, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
            obj = (data or {}).get("response") or {}
            if int(obj.get("success") or 0) == 1:
                sid = str(obj.get("steamid") or "").strip()
                if sid:
                    return sid
            return None
        except Exception:
            return None

    async def fetch_player_summary(self, steamid64: str) -> dict | None:
        m = await self.fetch_player_summaries([steamid64])
        return m.get(steamid64)

    async def fetch_player_summaries(self, steamids: list[str]) -> dict[str, dict]:
        http = self._http()
        if not steamids or not self.steam_web_api_key or not http:
            return {}
        uniq = sorted({s for s in steamids if s})
        out: dict[str, dict] = {}
        for i in range(0, len(uniq), 100):
            batch = uniq[i : i + 100]
            try:
                api = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
                params = {
                    "key": self.steam_web_api_key,
                    "steamids": ",".join(batch),
                }
                async with http.get(api, params=params) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                players = (((data or {}).get("response") or {}).get("players") or [])
                for p in players:
                    sid = str((p or {}).get("steamid") or "").strip()
                    if sid:
                        out[sid] = p
            except Exception:
                continue
        return out

    @staticmethod
    def extract_player_state(player: dict) -> tuple[str, int, str]:
        game_name = str(player.get("gameextrainfo") or "").strip()
        gameid_raw = str(player.get("gameid") or "").strip()
        appid = int(gameid_raw) if gameid_raw.isdigit() else 0
        if game_name or appid:
            return "in_game", appid, game_name or f"App {appid}"
        personastate = int(player.get("personastate") or 0)
        if personastate == 0:
            return "offline", 0, ""
        return "online", 0, ""

    @staticmethod
    def state_text(state: str) -> str:
        if state == "in_game":
            return "游戏中"
        if state == "online":
            return "在线"
        if state == "offline":
            return "离线"
        return state or "未知"

    async def fetch_playtime_text(self, steamid64: str, appid: int) -> str:
        http = self._http()
        if not steamid64 or appid <= 0 or not self.steam_web_api_key or not http:
            return "游戏时长：未知"
        try:
            api = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
            params = {
                "key": self.steam_web_api_key,
                "steamid": steamid64,
                "include_appinfo": 0,
                "include_played_free_games": 1,
            }
            async with http.get(api, params=params) as resp:
                if resp.status != 200:
                    return "游戏时长：未知"
                data = await resp.json(content_type=None)
            games = (((data or {}).get("response") or {}).get("games") or [])
            for g in games:
                if int((g or {}).get("appid") or 0) == int(appid):
                    mins = int((g or {}).get("playtime_forever") or 0)
                    hours = mins / 60
                    return f"游戏时长：{hours:.1f} 小时"
            return "游戏时长：未知"
        except Exception:
            return "游戏时长：未知"

    async def resolve_app(self, raw: str) -> dict | None:
        http = self._http()
        text = (raw or "").strip()
        if not text:
            return None

        m = re.search(r"store\.steampowered\.com/app/(\d+)", text, re.IGNORECASE)
        if m:
            appid = int(m.group(1))
            name = await self.fetch_app_name(appid)
            return {
                "appid": appid,
                "name": name or f"App {appid}",
                "url": f"https://store.steampowered.com/app/{appid}/",
            }

        if re.fullmatch(r"\d+", text):
            appid = int(text)
            name = await self.fetch_app_name(appid)
            return {
                "appid": appid,
                "name": name or f"App {appid}",
                "url": f"https://store.steampowered.com/app/{appid}/",
            }

        if not http:
            return None
        try:
            api = "https://store.steampowered.com/api/storesearch"
            params = {
                "term": text,
                "l": "schinese",
                "cc": "cn",
            }
            async with http.get(api, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
            items = (data or {}).get("items") or []
            if not items:
                return None
            item = items[0] or {}
            appid = int(item.get("id") or 0)
            if appid <= 0:
                return None
            name = str(item.get("name") or f"App {appid}")
            return {
                "appid": appid,
                "name": name,
                "url": f"https://store.steampowered.com/app/{appid}/",
            }
        except Exception:
            return None

    async def fetch_app_name(self, appid: int) -> str:
        http = self._http()
        if appid <= 0 or not http:
            return ""
        try:
            api = "https://store.steampowered.com/api/appdetails"
            params = {
                "appids": str(appid),
                "l": "schinese",
                "cc": "cn",
            }
            async with http.get(api, params=params) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json(content_type=None)
            obj = (data or {}).get(str(appid)) or {}
            if not obj.get("success"):
                return ""
            inner = obj.get("data") or {}
            return str(inner.get("name") or "")
        except Exception:
            return ""

    async def fetch_latest_news_gid(self, appid: int) -> str:
        latest = await self.fetch_latest_news(appid)
        if not latest:
            return ""
        return str(latest.get("gid") or "")

    async def fetch_latest_news(self, appid: int) -> dict | None:
        http = self._http()
        if appid <= 0 or not http:
            return None
        try:
            api = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
            params = {
                "appid": appid,
                "count": 1,
                "maxlength": 300,
                "format": "json",
            }
            async with http.get(api, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
            newsitems = (((data or {}).get("appnews") or {}).get("newsitems") or [])
            if not newsitems:
                return None
            return newsitems[0]
        except Exception:
            return None

    async def fetch_cover_image(self, appid: int):
        if appid <= 0:
            return None
        urls: list[str] = []
        http = self._http()

        if self.steamgriddb_api_key and http:
            try:
                api = f"https://www.steamgriddb.com/api/v2/grids/steam/{appid}"
                headers = {"Authorization": f"Bearer {self.steamgriddb_api_key}"}
                async with http.get(api, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        arr = (data or {}).get("data") or []
                        if arr:
                            url = str((arr[0] or {}).get("url") or "").strip()
                            if url:
                                urls.append(url)
            except Exception as exc:
                logger.debug(f"steamgriddb cover fetch failed: {exc!s}")

        urls.extend(
            [
                f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900_2x.jpg",
                f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
            ]
        )

        for url in urls:
            img = await self.fetch_image_pil(url)
            if img is not None:
                return img
        return None

    async def fetch_grid_image_by_id(self, grid_id: int):
        if grid_id <= 0:
            return None

        http = self._http()
        if not http or not self.steamgriddb_api_key:
            return None

        headers = {"Authorization": f"Bearer {self.steamgriddb_api_key}"}
        candidate_apis = [
            f"https://www.steamgriddb.com/api/v2/grids/{grid_id}",
            f"https://www.steamgriddb.com/api/v2/grids/id/{grid_id}",
        ]

        for api in candidate_apis:
            try:
                async with http.get(api, headers=headers) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json(content_type=None)

                data = (payload or {}).get("data")
                candidate_urls: list[str] = []

                if isinstance(data, dict):
                    url = str(data.get("url") or "").strip()
                    thumb = str(data.get("thumb") or "").strip()
                    if url:
                        candidate_urls.append(url)
                    if thumb:
                        candidate_urls.append(thumb)
                elif isinstance(data, list):
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        url = str(item.get("url") or "").strip()
                        thumb = str(item.get("thumb") or "").strip()
                        if url:
                            candidate_urls.append(url)
                        if thumb:
                            candidate_urls.append(thumb)
                        if candidate_urls:
                            break

                for url in candidate_urls:
                    img = await self.fetch_image_pil(url)
                    if img is not None:
                        return img
            except Exception as exc:
                logger.debug(f"steamgriddb grid fetch failed (grid_id={grid_id}): {exc!s}")

        return None

    async def fetch_image_pil(self, url: str):
        http = self._http()
        if not url or not http or Image is None:
            return None
        try:
            async with http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
            return await asyncio.to_thread(self._decode_image_sync, data)
        except Exception:
            return None

    @staticmethod
    def _decode_image_sync(data: bytes):
        if Image is None:
            return None
        try:
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None
