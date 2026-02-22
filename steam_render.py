from __future__ import annotations

import asyncio
import io
import re
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    import cairosvg
except Exception:  # pragma: no cover
    cairosvg = None

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except Exception:  # pragma: no cover
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None


class SteamRenderer:
    def __init__(self, cards_dir: Path):
        self._cards_dir = cards_dir
        self._plugin_src_dir = Path(__file__).resolve().parent
        self._steam_logo_cache: dict[tuple[int, int], Any] = {}
        self._font_cache: dict[tuple[str, int], Any] = {}
        self._selected_font_path: str | None = None
        self._logo_warned = False
        self._font_warned = False

    def _resolve_plugin_fonts_dir(self) -> Path | None:
        candidates = [
            self._plugin_src_dir / "fonts",
            self._cards_dir.parent / "fonts",
        ]
        for p in candidates:
            if p.exists() and p.is_dir():
                return p
        return None

    def _resolve_logo_svg_path(self) -> Path | None:
        candidates = [
            self._plugin_src_dir / "assets" / "logo_steam.svg",
            self._cards_dir.parent / "assets" / "logo_steam.svg",
        ]
        for p in candidates:
            if p.exists() and p.is_file():
                return p
        return None

    def _label(self, zh: str, en: str) -> str:
        return zh if self._selected_font_path else en

    def runtime_diagnostics(self) -> dict[str, str]:
        plugin_dir = self._cards_dir.parent
        fonts_dir = plugin_dir / "fonts"
        logo_svg = plugin_dir / "assets" / "logo_steam.svg"

        font_obj = self._load_font(20)
        logo_obj = self._load_steam_logo_icon(134, 30)

        return {
            "pillow": "ok" if Image is not None and ImageFont is not None else "missing",
            "cairosvg": "ok" if cairosvg is not None else "missing",
            "fonts_dir": "exists" if fonts_dir.exists() else "missing",
            "logo_svg": "exists" if logo_svg.exists() else "missing",
            "selected_font": self._selected_font_path or "none",
            "font_runtime": "ok" if font_obj is not None else "failed",
            "svg_runtime": "ok" if logo_obj is not None else "failed",
        }

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
        width = 900
        header_h = 104
        row_h = 178
        padding = 22
        height = header_h + row_h * count + padding

        canvas = Image.new("RGB", (width, height), color=(15, 23, 34))
        draw = ImageDraw.Draw(canvas)

        for y in range(height):
            t = y / max(1, height - 1)
            r = int(15 + 16 * t)
            g = int(23 + 24 * t)
            b = int(34 + 32 * t)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

        font_title = self._load_font(32)
        font_sub = self._load_font(20)
        font_name = self._load_font(24)
        font_text = self._load_font(20)
        font_small = self._load_font(18)

        draw.rounded_rectangle([(22, 18), (width - 22, 96)], radius=16, fill=(26, 38, 53))
        draw.rounded_rectangle([(22, 18), (width - 22, 57)], radius=16, fill=(40, 57, 78))
        draw.text(
            (38, 32),
            self._label(f"Steam 状态变化汇总 · {count} 人", f"Steam Status Update · {count}"),
            fill=(236, 240, 245),
            font=font_title,
        )
        draw.text(
            (40, 66),
            self._label("本轮检测到状态变化（开始/结束游戏/在线变化）", "Detected state changes in this poll"),
            fill=(170, 196, 216),
            font=font_sub,
        )
        self._draw_steam_badge(canvas, draw, width - 194, 30)

        state_color = {
            "in_game": (122, 235, 160),
            "online": (100, 190, 255),
            "offline": (170, 170, 170),
        }

        for idx, it in enumerate(entries):
            top = header_h + idx * row_h
            self._draw_shadow(canvas, (20, top + 8, width - 20, top + row_h - 10), radius=14)
            draw.rounded_rectangle(
                [(20, top + 8), (width - 20, top + row_h - 10)],
                radius=14,
                fill=(30, 43, 60),
            )
            draw.rounded_rectangle(
                [(20, top + 8), (width - 20, top + row_h - 10)],
                radius=14,
                outline=(58, 76, 98),
                width=2,
            )

            ns = str(it.get("new_state") or "")
            bar_color = state_color.get(ns, (178, 210, 230))
            draw.rounded_rectangle(
                [(24, top + 14), (32, top + row_h - 16)],
                radius=4,
                fill=bar_color,
            )

            cover = it.get("cover")
            if cover is None:
                cover = Image.new("RGB", (102, 136), color=(55, 60, 68))
            cover = self._rounded_image(cover, (102, 136), 10)
            canvas.paste(cover, (42, top + 16), cover)

            avatar = it.get("avatar")
            if avatar is None:
                avatar = Image.new("RGB", (64, 64), color=(85, 90, 100))
            avatar = self._circle_image(avatar, 64)
            canvas.paste(avatar, (156, top + 20), avatar)

            name = str(it.get("display_name") or "未知")
            status_desc = str(it.get("status_desc") or "")
            game_name = str(it.get("game_name") or "")
            playtime = str(it.get("playtime_text") or "")
            comment_text = str(it.get("comment_text") or "")

            status_symbol = self._status_symbol(ns)
            draw.text((236, top + 22), f"{status_symbol} {name}", fill=(245, 245, 245), font=font_name)
            draw.text((236, top + 62), status_desc, fill=(169, 223, 255), font=font_text)

            tag_w = 120
            draw.rounded_rectangle(
                [(width - tag_w - 34, top + 22), (width - 28, top + 56)],
                radius=10,
                fill=(45, 62, 84),
            )
            draw.text(
                (width - tag_w - 18, top + 28),
                self._label(self._state_text(ns), ns or "unknown"),
                fill=bar_color,
                font=font_small,
            )

            if game_name:
                draw.text(
                    (236, top + 98),
                    self._label(f"游戏：{game_name}", f"Game: {game_name}"),
                    fill=(220, 220, 220),
                    font=font_small,
                )
            if playtime:
                draw.text((236, top + 126), playtime, fill=(200, 200, 200), font=font_small)
            if comment_text:
                draw.text(
                    (236, top + 150),
                    self._label(
                        f"评价：{self._truncate_text(draw, comment_text, font_small, width - 290)}",
                        f"Comment: {self._truncate_text(draw, comment_text, font_small, width - 290)}",
                    ),
                    fill=(158, 204, 236),
                    font=font_small,
                )

        draw.text(
            (width - 250, height - 22),
            self._label(
                time.strftime("生成于 %Y-%m-%d %H:%M:%S", time.localtime()),
                time.strftime("Generated at %Y-%m-%d %H:%M:%S", time.localtime()),
            ),
            fill=(130, 150, 168),
            font=self._load_font(14),
        )

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

        width = 900
        height = 500
        canvas = Image.new("RGB", (width, height), color=(17, 24, 33))
        draw = ImageDraw.Draw(canvas)

        for y in range(height):
            t = y / max(1, height - 1)
            draw.line(
                [(0, y), (width, y)],
                fill=(int(17 + 10 * t), int(24 + 14 * t), int(33 + 18 * t)),
            )

        if cover is None:
            cover = Image.new("RGB", (270, 460), color=(43, 53, 66))
        cover = self._rounded_image(cover, (270, 460), 14)
        canvas.paste(cover, (20, 20), cover)

        draw.rounded_rectangle([(310, 20), (width - 20, height - 20)], radius=16, fill=(30, 43, 60))

        font_brand = self._load_font(22)
        font_game = self._load_font(32)
        font_title = self._load_font(28)
        font_meta = self._load_font(20)
        font_body = self._load_font(20)

        draw.text((334, 38), "STEAM NEWS", fill=(122, 204, 255), font=font_brand)
        draw.text((334, 72), game_name or f"App {appid}", fill=(238, 242, 248), font=font_game)
        self._draw_steam_badge(canvas, draw, width - 194, 30)

        title_lines = self._wrap_text(draw, title or "新公告", font_title, width - 354)
        y = 124
        for ln in title_lines[:2]:
            draw.text((334, y), ln, fill=(240, 240, 240), font=font_title)
            y += 36

        meta_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(date_ts)) if date_ts > 0 else self._label("未知时间", "unknown")
        draw.text(
            (334, 206),
            self._label(f"作者：{author or 'Steam'}", f"Author: {author or 'Steam'}"),
            fill=(170, 196, 216),
            font=font_meta,
        )
        draw.text(
            (334, 232),
            self._label(f"时间：{meta_time}", f"Time: {meta_time}"),
            fill=(170, 196, 216),
            font=font_meta,
        )

        body = (contents or "").replace("\r", "").replace("\n\n", "\n").replace("\n", " ").strip()
        if len(body) > 210:
            body = body[:210].rstrip() + "..."
        body_lines = self._wrap_text(
            draw,
            body or self._label("请点击链接查看完整公告内容。", "Please open the link for full details."),
            font_body,
            width - 354,
        )

        by = 280
        for ln in body_lines[:6]:
            draw.text((334, by), ln, fill=(220, 225, 232), font=font_body)
            by += 28

        draw.text(
            (width - 210, height - 34),
            self._label("来自 Steam News", "From Steam News"),
            fill=(130, 150, 168),
            font=self._load_font(16),
        )

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

        canvas = Image.new("RGB", (760, 360), color=(23, 26, 33))
        draw = ImageDraw.Draw(canvas)

        left = cover.resize((250, 360))
        canvas.paste(left, (0, 0))

        right_x = 272
        display = f"{steam_name}({group_name})"

        font_title = self._load_font(34)
        font_text = self._load_font(24)
        font_small = self._load_font(20)

        draw.text((right_x, 36), display, fill=(240, 240, 240), font=font_title)
        draw.text((right_x, 92), game_name, fill=(173, 216, 230), font=font_text)
        draw.text((right_x, 136), playtime_text, fill=(200, 200, 200), font=font_small)

        avatar = self._circle_image(avatar, 96)
        canvas.paste(avatar, (right_x, 206), avatar)
        self._draw_steam_badge(canvas, draw, 560, 24)
        draw.text(
            (right_x + 114, 236),
            self._label("状态：游戏中", "Status: in game"),
            fill=(122, 255, 160),
            font=font_text,
        )

        out = self._cards_dir / f"steam_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out, format="PNG")
        return str(out)

    def _load_font(self, size: int):
        if ImageFont is None:
            return None
        plugin_fonts = self._resolve_plugin_fonts_dir()
        if not self._selected_font_path:
            candidates: list[str] = []
            preferred = [
                (plugin_fonts / "NotoSansHans-Medium.otf") if plugin_fonts else None,
                (plugin_fonts / "NotoSansHans-Regular.otf") if plugin_fonts else None,
            ]
            for fp in preferred:
                if fp and fp.exists():
                    candidates.append(str(fp))

            if plugin_fonts and plugin_fonts.exists():
                for ext in ("*.otf", "*.ttf", "*.ttc"):
                    for fp in sorted(plugin_fonts.glob(ext)):
                        p = str(fp)
                        if p not in candidates:
                            candidates.append(p)

            candidates.extend(
                [
                    "C:/Windows/Fonts/msyh.ttc",
                    "C:/Windows/Fonts/simhei.ttf",
                    "C:/Windows/Fonts/msyhbd.ttc",
                ]
            )
            for p in candidates:
                try:
                    ImageFont.truetype(p, size=16, encoding="unic")
                    self._selected_font_path = p
                    logger.info(f"[steam-watch] selected font: {p}")
                    break
                except Exception:
                    try:
                        data = Path(p).read_bytes()
                        ImageFont.truetype(io.BytesIO(data), size=16, encoding="unic")
                        self._selected_font_path = p
                        logger.info(f"[steam-watch] selected font(by bytes): {p}")
                        break
                    except Exception:
                        continue

        candidates = [self._selected_font_path] if self._selected_font_path else []

        for p in candidates:
            if not p:
                continue
            key = (p, size)
            if key in self._font_cache:
                return self._font_cache[key]
            try:
                font = ImageFont.truetype(p, size=size, encoding="unic")
                self._font_cache[key] = font
                return font
            except Exception:
                try:
                    data = Path(p).read_bytes()
                    font = ImageFont.truetype(io.BytesIO(data), size=size, encoding="unic")
                    self._font_cache[key] = font
                    return font
                except Exception:
                    continue

        if not self._font_warned:
            logger.warning("[steam-watch] no usable truetype font found, fallback to Pillow default font (may cause garbled text)")
            self._font_warned = True
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
            return ">"
        if state == "online":
            return "*"
        if state == "offline":
            return "-"
        return "."

    def _draw_steam_badge(self, canvas, draw, x: int, y: int) -> None:
        draw.rounded_rectangle([(x, y), (x + 160, y + 48)], radius=10, fill=(34, 51, 74))
        logo = self._load_steam_logo_icon(134, 30)
        if logo is not None:
            lx = x + (160 - logo.width) // 2
            ly = y + (48 - logo.height) // 2
            canvas.paste(logo, (lx, ly), logo)
        else:
            draw.text((x + 40, y + 13), "STEAM", fill=(215, 230, 245), font=self._load_font(20))
            if not self._logo_warned:
                logger.warning("[steam-watch] steam logo svg render failed; use text fallback")
                self._logo_warned = True

    def _load_steam_logo_icon(self, target_w: int, target_h: int):
        if Image is None:
            return None
        cache_key = (target_w, target_h)
        if cache_key in self._steam_logo_cache:
            return self._steam_logo_cache[cache_key]

        logo_svg = self._cards_dir.parent / "assets" / "logo_steam.svg"
        resolved_logo = self._resolve_logo_svg_path()
        if not resolved_logo or cairosvg is None:
            if not self._logo_warned:
                if not resolved_logo:
                    logger.warning("[steam-watch] assets/logo_steam.svg not found")
                if cairosvg is None:
                    logger.warning("[steam-watch] CairoSVG unavailable, cannot render svg logo")
                self._logo_warned = True
            return None

        try:
            svg_data = resolved_logo.read_bytes()
            ratio = 355.666 / 89.333
            vb = self._parse_svg_viewbox(svg_data)
            if vb is not None and vb[2] > 0 and vb[3] > 0:
                ratio = vb[2] / vb[3]

            output_w = max(1, int(target_w))
            output_h = max(1, int(round(output_w / ratio)))
            if output_h > target_h:
                output_h = int(target_h)
                output_w = max(1, int(round(output_h * ratio)))

            png_bytes = cairosvg.svg2png(
                bytestring=svg_data,
                output_width=output_w,
                output_height=output_h,
            )
            logo = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            self._steam_logo_cache[cache_key] = logo
            return logo
        except Exception:
            return None

    @staticmethod
    def _parse_svg_viewbox(svg_data: bytes) -> tuple[float, float, float, float] | None:
        try:
            text = svg_data.decode("utf-8", errors="ignore")
            m = re.search(r'viewBox\s*=\s*"([^\"]+)"', text)
            if not m:
                return None
            parts = [p for p in re.split(r"[\s,]+", m.group(1).strip()) if p]
            if len(parts) != 4:
                return None
            return tuple(float(x) for x in parts)  # type: ignore[return-value]
        except Exception:
            return None

    @staticmethod
    def _rounded_image(img, size: tuple[int, int], radius: int):
        if Image is None or ImageDraw is None:
            return img
        src = img.convert("RGBA").resize(size)
        mask = Image.new("L", size, 0)
        d = ImageDraw.Draw(mask)
        d.rounded_rectangle([(0, 0), (size[0] - 1, size[1] - 1)], radius=radius, fill=255)
        src.putalpha(mask)
        return src

    @staticmethod
    def _circle_image(img, size: int):
        if Image is None or ImageDraw is None:
            return img
        src = img.convert("RGBA").resize((size, size))
        mask = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(mask)
        d.ellipse([(0, 0), (size - 1, size - 1)], fill=255)
        src.putalpha(mask)
        return src

    @staticmethod
    def _draw_shadow(canvas, box: tuple[int, int, int, int], radius: int = 14):
        if Image is None or ImageDraw is None or ImageFilter is None:
            return
        x1, y1, x2, y2 = box
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        shadow = Image.new("RGBA", (w + 16, h + 16), (0, 0, 0, 0))
        d = ImageDraw.Draw(shadow)
        d.rounded_rectangle([(8, 8), (w + 2, h + 2)], radius=radius, fill=(0, 0, 0, 110))
        shadow = shadow.filter(ImageFilter.GaussianBlur(5))
        canvas.paste(shadow, (x1 - 8, y1 - 8), shadow)

    @staticmethod
    def _truncate_text(draw, text: str, font, max_width: int) -> str:
        if not text:
            return ""
        if max_width <= 0:
            return text
        curr = text.strip()
        while curr:
            box = draw.textbbox((0, 0), curr, font=font)
            if box[2] - box[0] <= max_width:
                return curr
            curr = curr[:-1]
        return ""

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
