"""Статус enrich-пайплайна (порт enrichStatusClient.ts — URL внутреннего статуса движка)."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field


@dataclass
class EnrichStatus:
    data: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def online(self) -> bool:
        return bool(self.data) and self.error is None

    @property
    def state(self) -> str:
        return str(self.data.get("state") or "")

    @property
    def stage(self) -> str:
        return str(self.data.get("stage") or self.data.get("current_stage") or "")

    @property
    def profile(self) -> str:
        return str(self.data.get("profile") or "")

    @property
    def scanned(self) -> int:
        try:
            return int(self.data.get("scannedNotes") or self.data.get("scanned") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def total(self) -> int:
        try:
            return int(self.data.get("totalNotes") or self.data.get("total") or 0)
        except (TypeError, ValueError):
            return 0

    def summary(self) -> str:
        if not self.online:
            return "enrich: —"
        base = f"enrich: {self.stage or self.state or '?'}"
        if self.total:
            base += f" {self.scanned}/{self.total}"
        if self.profile:
            base += f" ({self.profile})"
        return base


def fetch_enrich_status(url: str, timeout: float = 8.0) -> EnrichStatus:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return EnrichStatus(error=f"HTTP {resp.status}")
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
        if not isinstance(data, dict):
            return EnrichStatus(error="not an object")
        return EnrichStatus(data=data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return EnrichStatus(error=str(exc))
