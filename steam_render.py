from __future__ import annotations

import asyncio
import base64
import html
import io
import mimetypes
import os
import platform
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from markupsafe import Markup

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

if TYPE_CHECKING:
    from jinja2 import Environment as JinjaEnvironment
else:
    JinjaEnvironment = Any

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except Exception:  # pragma: no cover
    Environment = None
    FileSystemLoader = None
    select_autoescape = None


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


_HTML_RENDER_LAUNCH_TIMEOUT_SEC = max(
    2, _env_int("STEAM_HTML_RENDER_LAUNCH_TIMEOUT", 8)
)
_HTML_RENDER_PAGE_TIMEOUT_SEC = max(2, _env_int("STEAM_HTML_RENDER_PAGE_TIMEOUT", 8))
_PLAYWRIGHT_INSTALL_TIMEOUT_SEC = max(
    60, _env_int("STEAM_PLAYWRIGHT_INSTALL_TIMEOUT", 600)
)
_PLAYWRIGHT_INSTALL_DEPS_TIMEOUT_SEC = max(
    60, _env_int("STEAM_PLAYWRIGHT_INSTALL_DEPS_TIMEOUT", 600)
)

_PLAYWRIGHT_INSTALL_LOCK = asyncio.Lock()
_PLAYWRIGHT_INSTALL_DONE = False
_PLAYWRIGHT_PREPARE_TASK: asyncio.Task[Any] | None = None
_PLAYWRIGHT_PREPARING_SKIP_LOGGED = False
_JINJA_ENV: JinjaEnvironment | None = None


class _PlaywrightRuntime:
    def __init__(self) -> None:
        self._playwright = None
        self._lock = asyncio.Lock()

    async def get(self):
        async with self._lock:
            if self._playwright is not None:
                return self._playwright
            try:
                from playwright.async_api import async_playwright
            except Exception as exc:
                logger.warning(f"playwright import failed: {exc!s}")
                return None

            try:
                self._playwright = await async_playwright().start()
                return self._playwright
            except Exception as exc:
                logger.warning(f"playwright startup failed: {exc!s}")
                return None


_PLAYWRIGHT_RUNTIME = _PlaywrightRuntime()


async def _run_playwright_cli(
    args: list[str], *, timeout_sec: int, env: dict[str, str] | None = None
) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "playwright", *args]
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=proc_env,
        )
    except Exception as exc:
        return 1, f"spawn failed: {exc!s}"

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))
    except TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, "timeout"
    except Exception as exc:
        return 1, f"run failed: {exc!s}"

    text = (out or b"").decode("utf-8", errors="ignore").strip()
    return int(proc.returncode or 0), text


async def ensure_playwright_runtime_ready(*, browser: str = "chromium") -> None:
    global _PLAYWRIGHT_INSTALL_DONE

    if _PLAYWRIGHT_INSTALL_DONE:
        return

    if _find_browser_executable():
        _PLAYWRIGHT_INSTALL_DONE = True
        return

    async with _PLAYWRIGHT_INSTALL_LOCK:
        if _PLAYWRIGHT_INSTALL_DONE:
            return

        target_browser = str(browser or "chromium").strip().lower()
        if target_browser not in {"chromium", "firefox", "webkit"}:
            target_browser = "chromium"

        rc_deps, output_deps = await _run_playwright_cli(
            ["install-deps", target_browser],
            timeout_sec=_PLAYWRIGHT_INSTALL_DEPS_TIMEOUT_SEC,
        )
        if rc_deps != 0:
            logger.warning(
                "playwright install-deps failed/skipped "
                f"(code={rc_deps}, browser={target_browser}): {output_deps[-300:]}"
            )
        else:
            logger.info(f"playwright install-deps success: {target_browser}")

        install_attempts: list[dict[str, str] | None] = [None]
        if platform.system().lower() == "linux" and not os.environ.get(
            "PLAYWRIGHT_DOWNLOAD_HOST"
        ):
            install_attempts.insert(
                0,
                {
                    "PLAYWRIGHT_DOWNLOAD_HOST": "https://cdn.npmmirror.com/binaries/playwright",
                    "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST": "https://cdn.npmmirror.com/binaries/chrome-for-testing",
                },
            )
            install_attempts.insert(
                1,
                {
                    "PLAYWRIGHT_DOWNLOAD_HOST": "https://npmmirror.com/mirrors/playwright",
                    "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST": "https://cdn.npmmirror.com/binaries/chrome-for-testing",
                },
            )

        rc = 1
        output = ""
        for idx, install_env in enumerate(install_attempts, start=1):
            if install_env:
                logger.info(
                    f"playwright install attempt#{idx} with mirror host: {install_env.get('PLAYWRIGHT_DOWNLOAD_HOST', '')}"
                )
            else:
                logger.info(f"playwright install attempt#{idx} with default host")

            rc, output = await _run_playwright_cli(
                ["install", target_browser],
                timeout_sec=_PLAYWRIGHT_INSTALL_TIMEOUT_SEC,
                env=install_env,
            )
            if rc == 0:
                break

        if rc != 0:
            logger.warning(
                "playwright install failed "
                f"(code={rc}, browser={target_browser}): {output[-300:]}"
            )
            return

        logger.info(f"playwright install success: {target_browser}")
        _PLAYWRIGHT_INSTALL_DONE = True


def start_playwright_runtime_prepare(*, browser: str = "chromium") -> None:
    global \
        _PLAYWRIGHT_INSTALL_DONE, \
        _PLAYWRIGHT_PREPARE_TASK, \
        _PLAYWRIGHT_PREPARING_SKIP_LOGGED

    if _PLAYWRIGHT_PREPARE_TASK is not None and not _PLAYWRIGHT_PREPARE_TASK.done():
        return

    local_browser = _find_browser_executable()
    if local_browser:
        _PLAYWRIGHT_INSTALL_DONE = True
        logger.info(
            f"local browser found on startup, skip playwright install: {local_browser}"
        )
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    _PLAYWRIGHT_PREPARING_SKIP_LOGGED = False
    logger.info("local browser missing on startup, preparing playwright in background")

    async def _runner() -> None:
        try:
            await ensure_playwright_runtime_ready(browser=browser)
        except Exception as exc:
            logger.warning(f"playwright background prepare failed: {exc!s}")

    _PLAYWRIGHT_PREPARE_TASK = loop.create_task(_runner())


def is_playwright_runtime_preparing() -> bool:
    return _PLAYWRIGHT_PREPARE_TASK is not None and not _PLAYWRIGHT_PREPARE_TASK.done()


def _find_browser_executable() -> str | None:
    custom = str(os.environ.get("STEAM_HTML_RENDER_BROWSER") or "").strip()
    if custom and Path(custom).exists():
        return custom

    candidates = [
        os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "Microsoft",
            "Edge",
            "Application",
            "msedge.exe",
        ),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "Microsoft",
            "Edge",
            "Application",
            "msedge.exe",
        ),
        os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("LocalAppData", ""),
            "Microsoft",
            "Edge",
            "Application",
            "msedge.exe",
        ),
        os.path.join(
            os.environ.get("LocalAppData", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
    ]

    for name in ("msedge", "msedge.exe", "chrome", "chrome.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    seen: set[str] = set()
    for item in candidates:
        p = str(item or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        if Path(p).exists():
            return p
    return None


def _image_to_data_uri(image_obj, *, filename: str = "image.png") -> str | None:
    if image_obj is None:
        return None
    if Image is None:
        return None
    if not hasattr(image_obj, "save"):
        return None

    try:
        buf = io.BytesIO()
        image_obj.save(buf, format="PNG")
        payload = base64.b64encode(buf.getvalue()).decode("ascii")
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "image/png"
        return f"data:{mime};base64,{payload}"
    except Exception:
        return None


def _escape_text(value: object) -> str:
    return html.escape(str(value or "").strip())


def _multiline_to_html(value: object) -> str:
    txt = _escape_text(value)
    return txt.replace("\n", "<br>")


def _state_label(state: str) -> str:
    mapping = {
        "in_game": "游戏中",
        "online": "在线",
        "offline": "离线",
        "ended": "结束",
    }
    return mapping.get(state, state or "未知")


def _state_color(state: str) -> str:
    mapping = {
        "in_game": "#34d399",
        "online": "#60a5fa",
        "offline": "#94a3b8",
        "ended": "#f59e0b",
    }
    return mapping.get(state, "#a5b4fc")


def _template_dir() -> Path:
    return Path(__file__).resolve().parent / "assets" / "template"


def _get_jinja_env() -> JinjaEnvironment:
    global _JINJA_ENV

    if _JINJA_ENV is not None:
        return _JINJA_ENV

    if Environment is None or FileSystemLoader is None or select_autoescape is None:
        raise RuntimeError("jinja2 is not installed")

    _JINJA_ENV = Environment(
        loader=FileSystemLoader(str(_template_dir())),
        autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
    )
    return _JINJA_ENV


def _render_template(template_name: str, values: dict[str, object]) -> str:
    env = _get_jinja_env()
    return env.get_template(template_name).render(**values)


def _build_batch_status_html(entries: list[dict]) -> str:
    row_entries: list[dict[str, str]] = []
    for entry in entries:
        state = str(entry.get("new_state") or "")
        cover_uri = _image_to_data_uri(entry.get("cover"), filename="cover.png")
        avatar_uri = _image_to_data_uri(entry.get("avatar"), filename="avatar.png")

        steam_name = str(entry.get("steam_name") or "").strip()
        group_nickname = str(entry.get("group_nickname") or "").strip()
        if not steam_name and not group_nickname:
            combined_name = str(entry.get("display_name") or "").strip()
            if combined_name:
                if combined_name.endswith(")") and "(" in combined_name:
                    split_index = combined_name.rfind("(")
                    steam_name = combined_name[:split_index].strip()
                    group_nickname = combined_name[split_index + 1 : -1].strip()
                else:
                    steam_name = combined_name

        if not steam_name:
            steam_name = group_nickname or "未知成员"
        if not group_nickname:
            group_nickname = "-"

        row_entries.append(
            {
                "status": state,
                "state_label": _state_label(state),
                "cover_uri": str(cover_uri or ""),
                "avatar_uri": str(avatar_uri or ""),
                "steam_name": steam_name,
                "group_nickname": group_nickname,
                "status_desc": str(entry.get("status_desc") or "状态未知"),
                "game_name": str(entry.get("game_name") or ""),
                "playtime_text": str(entry.get("playtime_text") or ""),
                "comment_text": str(entry.get("comment_text") or ""),
            }
        )

    now_text = _escape_text(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
    return _render_template(
        "batch_status.html",
        {
            "total": len(entries),
            "entries": row_entries,
            "generated_at": now_text,
        },
    )


def _build_news_html(
    *,
    appid: int,
    game_name: str,
    title: str,
    author: str,
    date_ts: int,
    contents: str,
    price_text: str | None,
    cover,
) -> str:
    cover_uri = _image_to_data_uri(cover, filename=f"{appid}.png")
    date_text = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(int(date_ts)))
        if int(date_ts or 0) > 0
        else "未知时间"
    )

    cover_html = (
        f'<img src="{cover_uri}" alt="cover">'
        if cover_uri
        else '<div class="empty">No Cover</div>'
    )
    values: dict[str, object] = {
        "cover_html": Markup(cover_html),
        "appid": _escape_text(appid),
        "game_name": _escape_text(game_name or f"App {appid}"),
        "title": _escape_text(title or "新公告"),
        "author": _escape_text(author or "Steam"),
        "date_text": _escape_text(date_text),
        "contents_html": Markup(
            _multiline_to_html(contents or "请点击链接查看完整公告内容。")
        ),
        "generated_at": _escape_text(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        ),
    }
    if price_text is not None:
        values["price_text"] = _escape_text(price_text)
    return _render_template("news.html", values)


def _parse_iso_to_ts(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


def _build_itad_price_history_html(
    *,
    game_name: str,
    appid: int,
    game_id: str,
    currency: str,
    points: list[dict],
) -> str:
    norm_points: list[dict[str, object]] = []
    for row in points:
        if not isinstance(row, dict):
            continue
        amount_raw = row.get("amount")
        try:
            amount = float(amount_raw)
        except Exception:
            continue
        timestamp = _parse_iso_to_ts(row.get("timestamp"))
        if timestamp <= 0:
            continue
        norm_points.append(
            {
                "ts": timestamp,
                "amount": amount,
                "shop": str(row.get("shop") or "").strip(),
                "cut": int(row.get("cut") or 0),
            }
        )

    if not norm_points:
        return _render_template(
            "itad_price_history.html",
            {
                "game_name": _escape_text(game_name or "Unknown Game"),
                "appid": _escape_text(appid or "-"),
                "game_id": _escape_text(game_id or "-"),
                "currency": _escape_text(currency or ""),
                "point_count": 0,
                "latest_price": "-",
                "lowest_price": "-",
                "lowest_time": "-",
                "highest_price": "-",
                "start_date": "-",
                "end_date": "-",
                "chart_points": "",
                "chart_polyline": "",
                "chart_w": 760,
                "chart_h": 240,
                "view_w": 900,
                "view_h": 300,
                "y_ticks": [],
                "lowest_marker": None,
                "rows": [],
                "generated_at": _escape_text(
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                ),
            },
        )

    norm_points.sort(key=lambda x: int(x["ts"]))
    values = [float(x["amount"]) for x in norm_points]
    min_v = min(values)
    max_v = max(values)
    raw_rng = max_v - min_v
    if raw_rng < 1e-6:
        pad = max(abs(max_v) * 0.05, 1.0)
    else:
        pad = max(raw_rng * 0.1, 0.01)
    y_min = min_v - pad
    y_max = max_v + pad
    rng = max(y_max - y_min, 1e-6)

    chart_w = 760.0
    chart_h = 240.0
    view_w = 900
    view_h = 300
    n = len(norm_points)
    latest_ts = max(int(norm_points[-1]["ts"]), int(time.time()))
    window_start_ts = latest_ts - 365 * 24 * 3600
    ts_range = max(latest_ts - window_start_ts, 1)

    chart_points: list[dict[str, object]] = []
    for row in norm_points:
        row_ts = int(row["ts"])
        x_ratio = (row_ts - window_start_ts) / ts_range
        x_ratio = min(1.0, max(0.0, x_ratio))
        x = chart_w * x_ratio
        y = chart_h * (1.0 - ((float(row["amount"]) - y_min) / rng))
        chart_points.append(
            {
                "x": round(x, 2),
                "y": round(y, 2),
                "amount": row["amount"],
                "ts": row["ts"],
            }
        )

    polyline = " ".join(f"{p['x']},{p['y']}" for p in chart_points)
    y_ticks = [
        {
            "value": round(y_max - (rng * i / 4), 2),
            "y": round(chart_h * i / 4, 2),
        }
        for i in range(5)
    ]

    latest = norm_points[-1]
    latest_price = float(latest["amount"])
    lowest = min(norm_points, key=lambda r: (float(r["amount"]), int(r["ts"])))
    lowest_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(lowest["ts"])))
    lowest_marker = None
    for p in chart_points:
        if (
            int(p["ts"]) == int(lowest["ts"])
            and abs(float(p["amount"]) - float(lowest["amount"])) < 1e-9
        ):
            lowest_marker = p
            break

    rows = list(reversed(norm_points[-8:]))
    row_items = [
        {
            "date": time.strftime("%Y-%m-%d", time.localtime(int(r["ts"]))),
            "price": f"{float(r['amount']):.2f}",
            "shop": str(r.get("shop") or "-") or "-",
            "cut": int(r.get("cut") or 0),
        }
        for r in rows
    ]

    return _render_template(
        "itad_price_history.html",
        {
            "game_name": _escape_text(game_name or "Unknown Game"),
            "appid": _escape_text(appid or "-"),
            "game_id": _escape_text(game_id or "-"),
            "currency": _escape_text(currency or ""),
            "point_count": n,
            "latest_price": f"{latest_price:.2f}",
            "lowest_price": f"{min_v:.2f}",
            "lowest_time": _escape_text(lowest_time),
            "highest_price": f"{max_v:.2f}",
            "start_date": _escape_text(
                time.strftime("%Y-%m-%d", time.localtime(int(norm_points[0]["ts"])))
            ),
            "end_date": _escape_text(
                time.strftime("%Y-%m-%d", time.localtime(int(norm_points[-1]["ts"])))
            ),
            "chart_points": chart_points,
            "chart_polyline": polyline,
            "chart_w": int(chart_w),
            "chart_h": int(chart_h),
            "view_w": int(view_w),
            "view_h": int(view_h),
            "y_ticks": y_ticks,
            "lowest_marker": lowest_marker,
            "rows": row_items,
            "generated_at": _escape_text(
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            ),
        },
    )


async def _render_page_to_png(
    *,
    browser,
    html_text: str,
    width: int,
    min_height: int,
    out_path: Path,
) -> str | None:
    page = None
    try:
        timeout_ms = int(float(_HTML_RENDER_PAGE_TIMEOUT_SEC) * 1000)
        page = await browser.new_page(
            viewport={
                "width": max(420, int(width)),
                "height": max(320, int(min_height)),
            },
            device_scale_factor=1.5,
        )
        await page.set_content(html_text, wait_until="load", timeout=timeout_ms)
        content_height = await page.evaluate(
            """
            () => {
              const body = document.body;
              const doc = document.documentElement;
              return Math.ceil(Math.max(
                body ? body.scrollHeight : 0,
                body ? body.offsetHeight : 0,
                doc ? doc.clientHeight : 0,
                doc ? doc.scrollHeight : 0,
                doc ? doc.offsetHeight : 0,
              ));
            }
            """
        )
        await page.set_viewport_size(
            {
                "width": max(420, int(width)),
                "height": max(320, int(content_height or min_height)),
            }
        )
        await page.screenshot(
            path=str(out_path),
            full_page=True,
            type="png",
            timeout=timeout_ms,
        )
        return str(out_path)
    except Exception as exc:
        logger.warning(f"Browser render failed: {exc!s}")
        return None
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def _render_html_to_png_file(
    *,
    html_text: str,
    width: int,
    prefix: str,
    min_height: int,
) -> str | None:
    global _PLAYWRIGHT_PREPARING_SKIP_LOGGED

    temp_dir = Path(get_astrbot_temp_path())
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = temp_dir / f"{prefix}_{uuid.uuid4().hex}.png"

    if is_playwright_runtime_preparing():
        if not _PLAYWRIGHT_PREPARING_SKIP_LOGGED:
            logger.info(
                "playwright preparing in progress; skip browser render this round"
            )
            _PLAYWRIGHT_PREPARING_SKIP_LOGGED = True
        return None

    runtime = await _PLAYWRIGHT_RUNTIME.get()
    if runtime is None:
        return None

    executable_path = _find_browser_executable()
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--no-zygote",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    browser = None
    try:
        launch_timeout_ms = int(float(_HTML_RENDER_LAUNCH_TIMEOUT_SEC) * 1000)
        kwargs: dict[str, Any] = {
            "headless": True,
            "args": launch_args,
            "timeout": launch_timeout_ms,
        }
        if executable_path:
            kwargs["executable_path"] = executable_path

        browser = await runtime.chromium.launch(**kwargs)
        return await _render_page_to_png(
            browser=browser,
            html_text=html_text,
            width=width,
            min_height=min_height,
            out_path=out_path,
        )
    except Exception as exc:
        logger.warning(f"Failed to render html snapshot by playwright: {exc!s}")
        return None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass


class SteamRenderer:
    def __init__(self, cards_dir: Path):
        self._cards_dir = cards_dir
        self._plugin_src_dir = Path(__file__).resolve().parent

    def start_runtime_prepare(self) -> None:
        start_playwright_runtime_prepare(browser="chromium")

    def runtime_diagnostics(self) -> dict[str, str]:
        plugin_dir = self._cards_dir.parent
        fonts_dir = plugin_dir / "fonts"
        logo_svg = plugin_dir / "assets" / "logo_steam.svg"
        browser_path = _find_browser_executable()

        return {
            "pillow": "ok" if Image is not None else "missing",
            "cairosvg": "ok",
            "fonts_dir": "exists" if fonts_dir.exists() else "missing",
            "logo_svg": "exists" if logo_svg.exists() else "missing",
            "selected_font": "browser",
            "font_runtime": "ok",
            "svg_runtime": "ok",
            "playwright": "ok" if browser_path else "need_install",
            "browser_path": browser_path or "none",
        }

    async def render_batch_status_card(self, entries: list[dict]) -> str | None:
        if not entries:
            return None
        html_text = _build_batch_status_html(entries)
        return await _render_html_to_png_file(
            html_text=html_text,
            width=980,
            prefix="steam_batch",
            min_height=320,
        )

    async def render_news_card(
        self,
        *,
        appid: int,
        game_name: str,
        title: str,
        author: str,
        date_ts: int,
        contents: str,
        price_text: str | None = None,
        cover,
    ) -> str | None:
        html_text = _build_news_html(
            appid=appid,
            game_name=game_name,
            title=title,
            author=author,
            date_ts=date_ts,
            contents=contents,
            price_text=price_text,
            cover=cover,
        )
        return await _render_html_to_png_file(
            html_text=html_text,
            width=980,
            prefix="steam_news",
            min_height=560,
        )

    async def render_itad_price_history_card(
        self,
        *,
        game_name: str,
        appid: int,
        game_id: str,
        currency: str,
        points: list[dict],
    ) -> str | None:
        html_text = _build_itad_price_history_html(
            game_name=game_name,
            appid=appid,
            game_id=game_id,
            currency=currency,
            points=points,
        )
        return await _render_html_to_png_file(
            html_text=html_text,
            width=1080,
            prefix="steam_itad_price",
            min_height=720,
        )
