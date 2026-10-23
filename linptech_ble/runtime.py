"""BLE advertisement capture and offline replay; never pairs or writes GATT."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, TextIO

from .config import DeviceConfig, normalize_address
from .decoder import Decoder
from .models import AdvertisementRecord, is_candidate, mibeacon_header
from .transport import BluetoothConfig, scanner_context


def _write_json(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
    stream.flush()


def _prepare_paths(paths: list[str | Path]) -> None:
    resolved = [Path(path).resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("輸出路徑必須各不相同。")
    for path in resolved:
        if path.exists():
            raise FileExistsError(f"拒絕覆蓋既有擷取檔：{path}；請指定新的 --output。")
    for path in resolved:
        path.parent.mkdir(parents=True, exist_ok=True)


def _open_output(stack: ExitStack, path: str | Path) -> TextIO:
    return stack.enter_context(Path(path).open("x", encoding="utf-8", newline="\n"))


def _summary(stats: dict[str, Any], *, stream: TextIO | None = None) -> None:
    print("摘要：" + json.dumps(stats, ensure_ascii=False), file=stream or sys.stderr, flush=True)


async def _wait_duration(seconds: float) -> None:
    if seconds:
        await asyncio.sleep(seconds)
    else:
        await asyncio.Event().wait()


async def capture(
    *,
    mode: str,
    seconds: float,
    output: str | Path,
    devices: list[DeviceConfig] | None = None,
    raw_output: str | Path | None = None,
    all_devices: bool = False,
    address: str | None = None,
    configured_only: bool = False,
    on_record: Callable[[dict[str, Any]], None] | None = None,
    on_started: Callable[[], None] | None = None,
    quiet: bool = False,
    transport: BluetoothConfig | None = None,
) -> dict[str, Any]:
    """Capture all selected callbacks; only console display is throttled."""
    if mode not in {"scan", "monitor"}:
        raise ValueError("未知的擷取模式。")
    paths = [output] + ([raw_output] if raw_output else [])
    _prepare_paths(paths)
    wanted = normalize_address(address) if address else None
    configured = {normalize_address(device.address) for device in devices or []}
    decoder = Decoder(devices or []) if mode == "monitor" else None
    stats: dict[str, Any] = {
        "mode": mode,
        "received": 0,
        "selected": 0,
        "written": 0,
        "raw_written": 0,
        "errors": 0,
        "statuses": {},
    }
    statuses: Counter[str] = Counter()
    last_display: dict[str, float] = {}
    last_state: dict[str, str] = {}
    callback_failure: Exception | None = None
    finished = asyncio.Event()
    started = time.monotonic()

    with ExitStack() as stack:
        destination = _open_output(stack, output)
        raw_destination = _open_output(stack, raw_output) if raw_output else None

        def on_advertisement(device: Any, advertisement: Any) -> None:
            nonlocal callback_failure
            if callback_failure is not None:
                return
            stats["received"] += 1
            try:
                record = AdvertisementRecord.from_bleak(device, advertisement)
                record_address = normalize_address(record.address)
                if wanted and record_address != wanted:
                    return
                if mode == "scan":
                    if not (all_devices or wanted or is_candidate(record)):
                        return
                elif not (wanted or record_address in configured or (not configured_only and is_candidate(record))):
                    return
                stats["selected"] += 1
                if raw_destination:
                    _write_json(raw_destination, record.to_dict())
                    stats["raw_written"] += 1
                if decoder is None:
                    _write_json(destination, record.to_dict())
                    try:
                        header = mibeacon_header(record) or {}
                        status = "encrypted" if header.get("encrypted") else "advertisement"
                    except ValueError:
                        header = {}
                        status = "malformed"
                    model = header.get("model") or "未知型號"
                    display = (
                        f"{record.timestamp} {record.address} RSSI={record.rssi} "
                        f"{model} PID={header.get('product_id_hex', '?')} {status} "
                        f"name={json.dumps(record.name, ensure_ascii=False)}"
                    )
                    immediate = False
                else:
                    decoded = decoder.decode(record)
                    _write_json(destination, decoded)
                    status = str(decoded.get("status", "parse_error"))
                    display = json.dumps(decoded, ensure_ascii=False, default=str)
                    state = json.dumps(
                        {key: decoded.get(key) for key in ("status", "measurements", "binary_sensors")},
                        sort_keys=True,
                        default=str,
                    )
                    immediate = bool(decoded.get("events")) or (
                        status != "duplicate" and state != last_state.get(record_address)
                    )
                    if status != "duplicate":
                        last_state[record_address] = state
                stats["written"] += 1
                statuses[status] += 1
                if on_record is not None:
                    on_record(decoded if decoder is not None else record.to_dict())
                now = time.monotonic()
                if not quiet and (immediate or now - last_display.get(record_address, float("-inf")) >= 1):
                    print(display, flush=True)
                    last_display[record_address] = now
            except Exception as error:
                # Stop instead of silently discarding records after an I/O failure.
                stats["errors"] += 1
                callback_failure = error
                finished.set()

        if not quiet:
            print(
                f"{mode}：{seconds:g} 秒" if seconds else f"{mode}：持續執行，按 Ctrl+C 結束",
                flush=True,
            )
            print(f"輸出：{Path(output).resolve()}", flush=True)
        try:
            async with scanner_context(transport, on_advertisement) as scanner:
                if on_started is not None:
                    on_started()
                duration = asyncio.create_task(_wait_duration(seconds))
                failure = asyncio.create_task(finished.wait())
                async def watch_transport() -> None:
                    while True:
                        if hasattr(scanner, "check_health"):
                            scanner.check_health()
                        await asyncio.sleep(1)
                health = asyncio.create_task(watch_transport())
                try:
                    done, _ = await asyncio.wait({duration, failure, health}, return_when=asyncio.FIRST_COMPLETED)
                    if health in done:
                        await health
                finally:
                    for task in (duration, failure, health):
                        task.cancel()
                    await asyncio.gather(duration, failure, health, return_exceptions=True)
            if callback_failure is not None:
                raise RuntimeError(f"擷取回呼失敗：{callback_failure}") from callback_failure
        finally:
            stats["statuses"] = dict(statuses)
            stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
            if not quiet:
                _summary(stats)
    return stats


def replay(
    input_path: str | Path,
    devices: list[DeviceConfig],
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Decode a raw JSONL capture without scanning or using the network."""
    source_path = Path(input_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到原始廣播檔：{source_path}")
    if output:
        _prepare_paths([output])
    decoder = Decoder(devices)
    statuses: Counter[str] = Counter()
    stats: dict[str, Any] = {"mode": "replay", "records": 0, "malformed_lines": 0}
    with ExitStack() as stack:
        source = stack.enter_context(source_path.open("r", encoding="utf-8-sig"))
        destination = _open_output(stack, output) if output else sys.stdout
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("每行必須是原始廣播 JSON 物件。")
                record = AdvertisementRecord.from_dict(value)
                decoded = decoder.decode(record)
            except (ValueError, TypeError, KeyError) as error:
                stats["malformed_lines"] += 1
                decoded = {"status": "malformed", "line": line_number, "error": str(error)}
            _write_json(destination, decoded)
            statuses[str(decoded.get("status", "parse_error"))] += 1
            stats["records"] += 1
    stats["statuses"] = dict(statuses)
    _summary(stats)
    return stats
