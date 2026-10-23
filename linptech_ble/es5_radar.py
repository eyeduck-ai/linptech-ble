"""ES5BB radar data, derived from protocol facts in the official v113 plugin.

No vendor JavaScript is loaded or executed. See docs/DIRECT_PROTOCOL.md.
"""
from __future__ import annotations

import re
from typing import Any

from .standard_auth import DirectError

BIN_COUNT, BIN_METERS = 32, .25
REPORT_MINUTES = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120)
REPORT_MASK = (1 << len(REPORT_MINUTES)) - 1


def duration_reporting(value: Any) -> dict[str, Any] | None:
    """Official v113 report settings are a uint16 mask, not an interval."""
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        return None
    return {"enabled_minutes": [minute for bit, minute in enumerate(REPORT_MINUTES) if value & (1 << bit)],
            "unknown_bits": value & ~REPORT_MASK,
            "options": [{"minute": minute, "bit": bit, "enabled": bool(value & (1 << bit))}
                        for bit, minute in enumerate(REPORT_MINUTES)]}


def hex_series(value: Any) -> list[int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != BIN_COUNT or any(not re.fullmatch(r"[0-9a-fA-F]{1,2}", p.strip()) for p in parts):
        return None
    return [int(p, 16) for p in parts]


def encode_series(values: list[int]) -> str:
    if len(values) != BIN_COUNT or any(type(v) is not int or not 0 <= v <= 255 for v in values):
        raise DirectError("雷達閾值必須是 32 個 0–255 的整數。")
    return ",".join(f"{v:X}" for v in values)


def bitmap_data(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if not 4 <= len(parts) <= 16 or any(not re.fullmatch(r"[0-9a-fA-F]{1,8}", p.strip()) for p in parts):
        return None
    numbers = [int(p, 16) for p in parts]
    noise = numbers[2]
    return {"motion": numbers[0], "presence": numbers[1], "occupied": numbers[-1],
            "floor_noise": noise, "noise_health": "正常" if noise > 95 else ("輕微異常" if noise >= 92 else "嚴重異常"),
            "pir": numbers[7] if len(numbers) >= 8 else None}


def radar_bins(mask: int | None) -> list[bool] | None:
    if type(mask) is not int or not 0 <= mask <= 0xFFFFFFFF:
        return None
    return [bool(mask & (1 << i)) for i in range(BIN_COUNT)]


class EmptyRoomScan:
    """Preview-only noise learning; writes require a separate apply operation.

    Thirty consecutive seconds of explicitly vacant readings are required.
    Candidates only raise the original thresholds, capped at a byte. This is a
    conservative desktop workflow, not a claim to clone every Mi Home preset.
    """
    duration = 30
    margin = 6

    def __init__(self, original: dict[str, Any], started: float) -> None:
        self.original, self.started = original, started
        self.since: float | None = None
        self.occupancy: int | None = None
        self.occupancy_at = -1e9
        self.peaks = {name: [0] * BIN_COUNT for name in ("motion_energy", "presence_energy")}
        self.samples = {name: 0 for name in self.peaks}
        self.last_energy = {name: -1e9 for name in self.peaks}

    def reset(self) -> None:
        self.since = None
        for name in self.peaks:
            self.peaks[name] = [0] * BIN_COUNT
            self.samples[name] = 0
            self.last_energy[name] = -1e9

    def accept(self, name: str, value: Any, now: float) -> None:
        if name == "occupancy_status":
            self.occupancy, self.occupancy_at = value, now
            if value != 0:
                self.reset()
        if name not in self.peaks or self.occupancy != 0 or now - self.occupancy_at > 6:
            return
        series = hex_series(value)
        if series is None:
            return
        self.peaks[name] = [max(old, new) for old, new in zip(self.peaks[name], series)]
        self.samples[name] += 1
        self.last_energy[name] = now
        if self.since is None and all(self.samples.values()):
            self.since = now

    def progress(self, now: float) -> dict[str, Any]:
        if now - self.occupancy_at > 6 or any(now - self.last_energy[n] > 6 for n in self.peaks if self.samples[n]):
            self.reset()
        elapsed = max(0, int(now - self.since)) if self.since is not None else 0
        ready = elapsed >= self.duration and all(n >= 5 for n in self.samples.values()) and all(now - t <= 6 for t in self.last_energy.values())
        return {"elapsed": min(elapsed, self.duration), "duration": self.duration,
                "samples": dict(self.samples), "ready": ready}

    def proposals(self) -> dict[str, str]:
        return {target: encode_series([max(base, min(255, peak + self.margin))
                for base, peak in zip(hex_series(self.original[target]) or [], self.peaks[source])])
                for target, source in (("motion_threshold", "motion_energy"), ("presence_threshold", "presence_energy"))}
