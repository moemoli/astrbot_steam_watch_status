from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = None
    ImageDraw = None
    ImageFont = None


class SteamRenderer:
    def __init__(self, cards_dir: Path):
        self._cards_dir = cards_dir

    async def render_playing_card(
        self,
        *,
        steam_name: str,
        group_name: str,
        game_name: str,
        playtime_text: str,
        cover,
        avatar,
    ) -> str | None:
        if Image is None or ImageDraw is None:
            return None
        return await asyncio.to_thread(
            self._render_playing_card_sync,
            steam_name,
            group_name,
            game_name,
            playtime_text,
            cover,
            avatar,
        )

    async def render_batch_status_card(self, entries: list[dict]) -> str | None:
        if not entries or Image is None or ImageDraw is None:
            return None
        return await asyncio.to_thread(self._render_batch_status_card_sync, entries)

    async def render_news_card(
        self,
        *,
        appid: int,
        game_name: str,
        title: str,
        author: str,
        date_ts: int,
        contents: str,
        cover,
    ) -> str | None:
        if Image is None or ImageDraw is None:
            return None
        return await asyncio.to_thread(
            self._render_news_card_sync,
            appid,
            game_name,
            title,
            author,
            date_ts,
            contents,
            cover,
        )

    def _render_batch_status_card_sync(self, entries: list[dict]) -> str | None:
        if not entries or Image is None or ImageDraw is None:
            return None

        count = len(entries)
        width = 1240
        header_h = 118
        row_h = 188
        padding = 26
        height = header_h + row_h * count + padding

        canvas = Image.new("RGB", (width, height), color=(15, 23, 34))
        draw = ImageDraw.Draw(canvas)

        for y in range(height):
            t = y / max(1, height - 1)
            r = int(15 + 16 * t)
            g = int(23 + 24 * t)
            b = int(34 + 32 * t)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

        font_title = self._load_font(40)
        font_sub = self._load_font(24)
        font_name = self._load_font(30)
        font_text = self._load_font(24)
        font_small = self._load_font(20)

        draw.rounded_rectangle([(22, 18), (width - 22, 96)], radius=16, fill=(26, 38, 53))
        draw.text((42, 34), f"Steam 状态变化汇总 · {count} 人", fill=(236, 240, 245), font=font_title)
        draw.text((44, 72), "本轮轮询检测到状态变化（开始游戏 / 结束游戏 / 在线状态变更）", fill=(170, 196, 216), font=font_sub)
        self._draw_steam_badge(draw, width - 220, 30)

        state_color = {
            "in_game": (122, 235, 160),
            "online": (100, 190, 255),
            "offline": (170, 170, 170),
        }

        for idx, it in enumerate(entries):
            top = header_h + idx * row_h
            draw.rounded_rectangle(
                [(20, top + 8), (width - 20, top + row_h - 10)],
                radius=14,
                fill=(30, 43, 60),
            )

            ns = str(it.get("new_state") or "")
            bar_color = state_color.get(ns, (178, 210, 230))
            draw.rounded_rectangle(
                [(24, top + 14), (34, top + row_h - 16)],
                radius=4,
                fill=bar_color,
            )

            cover = it.get("cover")
            if cover is None:
                cover = Image.new("RGB", (120, 158), color=(55, 60, 68))
            cover = cover.resize((120, 158))
            canvas.paste(cover, (48, top + 14))

            avatar = it.get("avatar")
            if avatar is None:
                avatar = Image.new("RGB", (76, 76), color=(85, 90, 100))
            avatar = avatar.resize((76, 76))
            canvas.paste(avatar, (184, top + 20))

            name = str(it.get("display_name") or "未知")
            status_desc = str(it.get("status_desc") or "")
            game_name = str(it.get("game_name") or "")
            playtime = str(it.get("playtime_text") or "")

            status_symbol = self._status_symbol(ns)
            draw.text((286, top + 22), f"{status_symbol} {name}", fill=(245, 245, 245), font=font_name)
            draw.text((286, top + 70), status_desc, fill=(169, 223, 255), font=font_text)

            tag_w = 150
            draw.rounded_rectangle(
                [(width - tag_w - 40, top + 24), (width - 34, top + 64)],
                radius=10,
                fill=(45, 62, 84),
            )
            draw.text(
                (width - tag_w - 24, top + 33),
                self._state_text(ns),
                fill=bar_color,
                font=font_small,
            )

            if game_name:
                draw.text((286, top + 108), f"游戏：{game_name}", fill=(220, 220, 220), font=font_small)
            if playtime:
                draw.text((286, top + 136), playtime, fill=(200, 200, 200), font=font_small)

        out = self._cards_dir / f"steam_batch_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out, format="PNG")
        return str(out)

    def _render_news_card_sync(
        self,
        appid: int,
        game_name: str,
        title: str,
        author: str,
        date_ts: int,
        contents: str,
        cover,
    ) -> str | None:
        if Image is None or ImageDraw is None:
            return None

        width = 1240
        height = 560
        canvas = Image.new("RGB", (width, height), color=(17, 24, 33))
        draw = ImageDraw.Draw(canvas)

        for y in range(height):
            t = y / max(1, height - 1)
            draw.line(
                [(0, y), (width, y)],
                fill=(int(17 + 10 * t), int(24 + 14 * t), int(33 + 18 * t)),
            )

        if cover is None:
            cover = Image.new("RGB", (360, 520), color=(43, 53, 66))
        cover = cover.resize((360, 520))
        canvas.paste(cover, (20, 20))

        draw.rounded_rectangle([(400, 20), (width - 20, height - 20)], radius=16, fill=(30, 43, 60))

        font_brand = self._load_font(26)
        font_game = self._load_font(38)
        font_title = self._load_font(32)
        font_meta = self._load_font(22)
        font_body = self._load_font(22)

        draw.text((428, 40), "STEAM NEWS", fill=(122, 204, 255), font=font_brand)
        draw.text((428, 80), game_name or f"App {appid}", fill=(238, 242, 248), font=font_game)

        title_lines = self._wrap_text(draw, title or "新公告", font_title, width - 468)
        y = 142
        for ln in title_lines[:2]:
            draw.text((428, y), ln, fill=(240, 240, 240), font=font_title)
            y += 40

        meta_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(date_ts)) if date_ts > 0 else "未知时间"
        draw.text((428, 232), f"作者：{author or 'Steam'}", fill=(170, 196, 216), font=font_meta)
        draw.text((428, 262), f"时间：{meta_time}", fill=(170, 196, 216), font=font_meta)

        body = (contents or "").replace("\r", "").replace("\n\n", "\n").replace("\n", " ").strip()
        if len(body) > 260:
            body = body[:260].rstrip() + "..."
        body_lines = self._wrap_text(draw, body or "请点击链接查看完整公告内容。", font_body, width - 468)

        by = 320
        for ln in body_lines[:7]:
            draw.text((428, by), ln, fill=(220, 225, 232), font=font_body)
            by += 30

        out = self._cards_dir / f"steam_news_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out, format="PNG")
        return str(out)

    def _render_playing_card_sync(
        self,
        steam_name: str,
        group_name: str,
        game_name: str,
        playtime_text: str,
        cover,
        avatar,
    ) -> str | None:
        if Image is None or ImageDraw is None:
            return None

        if cover is None:
            cover = Image.new("RGB", (420, 520), color=(40, 40, 40))
        if avatar is None:
            avatar = Image.new("RGB", (128, 128), color=(70, 70, 70))

        canvas = Image.new("RGB", (980, 420), color=(23, 26, 33))
        draw = ImageDraw.Draw(canvas)

        left = cover.resize((300, 420))
        canvas.paste(left, (0, 0))

        right_x = 330
        display = f"{steam_name}({group_name})"

        font_title = self._load_font(42)
        font_text = self._load_font(30)
        font_small = self._load_font(24)

        draw.text((right_x, 36), display, fill=(240, 240, 240), font=font_title)
        draw.text((right_x, 106), game_name, fill=(173, 216, 230), font=font_text)
        draw.text((right_x, 158), playtime_text, fill=(200, 200, 200), font=font_small)

        avatar = avatar.resize((120, 120))
        canvas.paste(avatar, (right_x, 220))
        draw.text((right_x + 140, 252), "状态：游戏中", fill=(122, 255, 160), font=font_text)

        out = self._cards_dir / f"steam_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out, format="PNG")
        return str(out)

    def _load_font(self, size: int):
        if ImageFont is None:
            return None
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/msyhbd.ttc",
        ]
        for p in candidates:
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _state_text(state: str) -> str:
        if state == "in_game":
            return "游戏中"
        if state == "online":
            return "在线"
        if state == "offline":
            return "离线"
        return state or "未知"

    @staticmethod
    def _status_symbol(state: str) -> str:
        if state == "in_game":
            return "🎮"
        if state == "online":
            return "🟢"
        if state == "offline":
            return "⚫"
        return "•"

    def _draw_steam_badge(self, draw, x: int, y: int) -> None:
        draw.rounded_rectangle([(x, y), (x + 180, y + 54)], radius=12, fill=(34, 51, 74))
        cx, cy, r = x + 28, y + 27, 12
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=(180, 205, 225))
        draw.ellipse([(cx - 4, cy - 4), (cx + 4, cy + 4)], fill=(34, 51, 74))
        draw.line([(cx + 7, cy - 7), (cx + 24, cy - 16)], fill=(180, 205, 225), width=3)
        draw.ellipse([(cx + 20, cy - 20), (cx + 30, cy - 10)], outline=(180, 205, 225), width=2)
        draw.text((x + 50, y + 15), "STEAM", fill=(215, 230, 245), font=self._load_font(24))

    @staticmethod
    def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
        if not text:
            return []
        words = text.split(" ")
        lines: list[str] = []
        current = ""
        for w in words:
            nxt = w if not current else f"{current} {w}"
            box = draw.textbbox((0, 0), nxt, font=font)
            width = box[2] - box[0]
            if width <= max_width:
                current = nxt
                continue
            if current:
                lines.append(current)
                current = w
            else:
                lines.append(w)
                current = ""
        if current:
            lines.append(current)
        return lines
