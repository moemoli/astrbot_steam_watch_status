from __future__ import annotations

import asyncio
import json
from pathlib import Path


class SteamStateStore:
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir

    def ensure_data_dir(self) -> None:
        self.base_dir().mkdir(parents=True, exist_ok=True)
        self.cards_dir().mkdir(parents=True, exist_ok=True)

    def base_dir(self) -> Path:
        return self._base_dir

    def cards_dir(self) -> Path:
        return self.base_dir() / "cards"

    def state_file(self) -> Path:
        return self.base_dir() / "state.json"

    async def load_state(self) -> tuple[list[dict], list[dict]]:
        data = await asyncio.to_thread(self._read_state_sync)
        if not isinstance(data, dict):
            return [], []
        bindings = data.get("bindings") if isinstance(data, dict) else []
        subs = data.get("game_subscriptions") if isinstance(data, dict) else []
        return (
            bindings if isinstance(bindings, list) else [],
            subs if isinstance(subs, list) else [],
        )

    async def save_state(self, bindings: list[dict], game_subscriptions: list[dict]) -> None:
        await asyncio.to_thread(self._write_state_sync, bindings, game_subscriptions)

    def _read_state_sync(self) -> dict:
        fp = self.state_file()
        if not fp.exists():
            return {"bindings": [], "game_subscriptions": []}
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            return {"bindings": [], "game_subscriptions": []}
        except Exception:
            return {"bindings": [], "game_subscriptions": []}

    def _write_state_sync(self, bindings: list[dict], game_subscriptions: list[dict]) -> None:
        fp = self.state_file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bindings": bindings,
            "game_subscriptions": game_subscriptions,
        }
        fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
