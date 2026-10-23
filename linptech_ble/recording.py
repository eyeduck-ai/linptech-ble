"""Bounded application recording; the decoder and BLE client never use this module."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time
from typing import Any, Mapping
from uuid import uuid4


_FORMAT = "linptech-ble-recording-v1"
_PREFIX = "linptech-recording-v1-"
_OWNER = re.compile(r"linptech-recording-v1-([0-9a-f]{32})\.owner\.json\Z")
_PART = re.compile(r"linptech-recording-v1-([0-9a-f]{32})\.(events|raw)\.([0-9]{6})\.jsonl\Z")


class RecordingError(RuntimeError):
    """Application recording stopped or could not accept a record."""


@dataclass(frozen=True)
class RecordingConfig:
    enabled: bool = True
    directory: str | Path = "captures"
    max_file_bytes: int = 10 * 1024 * 1024
    retention_days: float = 7
    max_parts: int = 100
    queue_size: int = 1024
    flush_interval: float = .5

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or not isinstance(self.directory, (str, Path)) or not str(self.directory):
            raise ValueError("Invalid recording directory or enabled flag.")
        for value, maximum in ((self.max_file_bytes, 1024**3), (self.max_parts, 100000), (self.queue_size, 65536)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Recording byte, part and queue limits must be positive bounded integers.")
        if (type(self.retention_days) not in (int, float) or not math.isfinite(self.retention_days)
                or not 0 <= self.retention_days <= 36500 or type(self.flush_interval) not in (int, float)
                or not math.isfinite(self.flush_interval) or not .01 <= self.flush_interval <= 60):
            raise ValueError("Invalid recording retention or flush interval.")


def _regular(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not (getattr(info, "st_file_attributes", 0)
                                                  & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
    except OSError:
        return False


def _safe_directory(path: Path) -> None:
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise RecordingError("Recording directories cannot contain symbolic links.")
        if entry.exists():
            info = entry.lstat()
            if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
                raise RecordingError("Recording directories cannot contain reparse points.")


class AsyncRecordingWriter:
    """Nonblocking enqueue with an explicit loss policy and a single disk thread.

    ``paths`` keeps CLI fixed-file behavior. ``config`` creates managed parts;
    retention considers only regular parts with a valid application owner marker.
    Active files and parts without completed-close proof are never pruned.
    """

    def __init__(self, *, config: RecordingConfig | None = None,
                 paths: Mapping[str, str | Path] | None = None,
                 streams: tuple[str, ...] = ("events", "raw"), policy: str = "drop_newest",
                 clock=time.time) -> None:
        if policy not in {"drop_newest", "fail"} or (config is None) == (paths is None):
            raise ValueError("Choose managed recording or fixed paths and a valid loss policy.")
        if config is not None and (not streams or not set(streams) <= {"events", "raw"}):
            raise ValueError("Invalid managed recording streams.")
        self.config = config
        self.enabled = config.enabled if config else True
        self.policy, self.clock = policy, clock
        self.paths = {key: Path(value) for key, value in (paths or {}).items()}
        if paths is not None and (not self.paths or len({p.absolute() for p in self.paths.values()}) != len(self.paths)):
            raise ValueError("Recording output paths must be distinct.")
        self.streams = tuple(self.paths) if paths is not None else streams
        self.queue: queue.Queue = queue.Queue(config.queue_size if config else 1024)
        self.lock = threading.Lock()
        self.closing, self.failed = threading.Event(), threading.Event()
        self.start_done = threading.Event()
        self.start_done.set()
        self.thread: threading.Thread | None = None
        self.files: dict[str, Any] = {}
        self.sizes: dict[str, int] = {}
        self.parts: dict[str, int] = {key: 0 for key in self.streams}
        self.written = {key: 0 for key in self.streams}
        self.accepted = self.dropped = self.errors = self.pending = self.pruned = 0
        self.error: str | None = None
        self.status = "idle" if self.enabled else "disabled"
        self.run_id = uuid4().hex
        self.directory = Path(config.directory).absolute() if config else None
        self.owner: Path | None = None
        self.owner_created = False
        self.files_closed = False

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"enabled": self.enabled, "status": self.status, "policy": self.policy,
                "accepted": self.accepted, "written": dict(self.written), "dropped": self.dropped,
                "pending": self.pending, "queued": self.queue.qsize(), "errors": self.errors,
                "error_type": self.error, "pruned": self.pruned,
                "paths": {key: str(path) for key, path in self.paths.items()}}

    def _error(self, error: BaseException) -> None:
        with self.lock:
            self.errors += 1
            self.error = type(error).__name__
            self.status = "error"
            self.failed.set()

    def _owner_data(self, active: bool) -> bytes:
        return json.dumps({"format": _FORMAT, "run_id": self.run_id, "active": active}).encode("ascii")

    def start(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            if self.closing.is_set() or self.status != "idle":
                raise RecordingError("Recording writer cannot start after closing or a previous start.")
            self.status = "starting"
            self.start_done.clear()
        try:
            if self.config:
                assert self.directory is not None
                _safe_directory(self.directory)
                self.directory.mkdir(parents=True, exist_ok=True)
                _safe_directory(self.directory)
                self.owner = self.directory / f"{_PREFIX}{self.run_id}.owner.json"
                with self.owner.open("xb") as marker:
                    marker.write(self._owner_data(True))
                self.owner_created = True
                if self.closing.is_set():
                    raise RecordingError("Recording startup was cancelled.")
                self._prune()
                for key in self.streams:
                    self._open_part(key)
            else:
                for path in self.paths.values():
                    if os.path.lexists(path):
                        raise FileExistsError("Refusing to overwrite a recording file.")
                for key, path in self.paths.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self.files[key] = path.open("xb")
                    self.sizes[key] = 0
            with self.lock:
                if self.closing.is_set():
                    raise RecordingError("Recording startup was cancelled.")
                self.status = "running"
                worker = threading.Thread(target=self._run, name="linptech-recording", daemon=True)
                worker.start()
                self.thread = worker
        except BaseException as error:
            self._error(error)
            self._close_files(mark_closed=False)
            if self.owner_created and self.owner and _regular(self.owner):
                try:
                    self.owner.write_bytes(self._owner_data(not self.files_closed))
                except Exception as cleanup_error:
                    self._error(cleanup_error)
            raise
        finally:
            self.start_done.set()

    def _open_part(self, key: str) -> None:
        assert self.directory is not None
        _safe_directory(self.directory)
        self.parts[key] += 1
        if self.parts[key] > 999999:
            raise RecordingError("Recording part limit exhausted.")
        path = self.directory / f"{_PREFIX}{self.run_id}.{key}.{self.parts[key]:06d}.jsonl"
        stream = path.open("xb")
        with self.lock:
            self.paths[key] = path
        self.files[key], self.sizes[key] = stream, 0

    def _closed_marker(self, path: Path) -> Path:
        return path.with_name(path.name + ".closed")

    def _mark_closed(self, path: Path) -> None:
        marker = self._closed_marker(path)
        with marker.open("xb") as destination:
            destination.write(json.dumps({"format": _FORMAT, "part": path.name}).encode("ascii"))

    def _closed_proof(self, path: Path) -> bool:
        marker = self._closed_marker(path)
        try:
            if not _regular(marker) or marker.stat().st_size > 1024:
                return False
            return json.loads(marker.read_text(encoding="ascii")) == {"format": _FORMAT, "part": path.name}
        except (ValueError, OSError, UnicodeError):
            return False

    def submit(self, records: Mapping[str, dict[str, Any]]) -> bool:
        if not self.enabled:
            return False
        if self.failed.is_set() or self.closing.is_set() or self.thread is None:
            with self.lock:
                self.dropped += 1
            if self.policy == "fail":
                raise RecordingError("Recording is unavailable; no lossless capture is claimed.")
            return False
        encoded = {key: (json.dumps(value, ensure_ascii=False, default=str) + "\n").encode("utf-8")
                   for key, value in records.items()}
        if not encoded or not set(encoded) <= set(self.streams):
            raise ValueError("Unknown recording stream.")
        if any(len(value) > (self.config.max_file_bytes if self.config else 1024 * 1024) for value in encoded.values()):
            with self.lock:
                self.dropped += 1
                self.errors += 1
                self.error = "RecordTooLarge"
            if self.policy == "fail":
                raise RecordingError("Recording record exceeds its byte limit.")
            return False
        with self.lock:
            # Encoding intentionally occurs without the disk/lifecycle lock.
            # Close or failure may have completed while that snapshot was made.
            if self.failed.is_set() or self.closing.is_set() or self.thread is None:
                self.dropped += 1
                if self.policy == "fail":
                    raise RecordingError("Recording closed before this record could be queued.")
                return False
            try:
                self.queue.put_nowait(encoded)
            except queue.Full:
                self.dropped += 1
                if self.policy == "fail":
                    raise RecordingError("Recording queue is full; capture stopped before further loss.") from None
                return False
            self.accepted += 1
            self.pending += 1
        return True

    def _flush(self) -> None:
        for stream in self.files.values():
            stream.flush()

    def _close_files(self, *, mark_closed: bool) -> None:
        closed = 0
        for key, stream in self.files.items():
            try:
                stream.close()
            except Exception as error:
                self._error(error)
                continue
            closed += 1
            if mark_closed and self.config:
                path = self.paths[key]
                if _regular(path) and not self._closed_marker(path).exists():
                    try:
                        self._mark_closed(path)
                    except Exception as error:
                        self._error(error)
        self.files_closed = closed == len(self.files)

    def _write(self, job: Mapping[str, bytes]) -> None:
        for key, data in job.items():
            if self.config and self.sizes[key] and self.sizes[key] + len(data) > self.config.max_file_bytes:
                self.files[key].close()
                del self.files[key]
                self._mark_closed(self.paths[key])
                self._open_part(key)
                self._prune()
            self.files[key].write(data)
            self.sizes[key] += len(data)
            with self.lock:
                self.written[key] += 1

    def _prune(self) -> None:
        if not self.config:
            return
        assert self.directory is not None
        _safe_directory(self.directory)
        owners = {}
        for marker in self.directory.iterdir():
            match = _OWNER.fullmatch(marker.name)
            if not match or not _regular(marker):
                continue
            try:
                if marker.stat().st_size > 1024:
                    continue
                info = json.loads(marker.read_text(encoding="ascii"))
            except (OSError, ValueError, UnicodeError):
                continue
            if (isinstance(info, dict) and set(info) == {"format", "run_id", "active"}
                    and info["format"] == _FORMAT and info["run_id"] == match[1]
                    and type(info["active"]) is bool
                    ):
                owners[match[1]] = (marker, info["active"])
        active = set() if self.files_closed else set(self.paths.values())
        candidates = []
        for path in self.directory.iterdir():
            match = _PART.fullmatch(path.name)
            if (match and int(match[3]) >= 1 and match[1] in owners
                    and path not in active and _regular(path) and self._closed_proof(path)):
                try:
                    candidates.append((path.stat().st_mtime, path))
                except FileNotFoundError:
                    continue
        candidates.sort(key=lambda pair: (pair[0], pair[1].name))
        cutoff = self.clock() - self.config.retention_days * 86400
        remove = {path for modified, path in candidates if self.config.retention_days and modified < cutoff}
        remaining = [path for _, path in candidates if path not in remove]
        remove.update(remaining[:max(0, len(remaining) - self.config.max_parts)])
        for path in remove:
            _safe_directory(self.directory)
            if _regular(path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue  # Another recording process already pruned it.
                if self._closed_proof(path):
                    try:
                        self._closed_marker(path).unlink()
                    except FileNotFoundError:
                        pass
                with self.lock:
                    self.pruned += 1
        for run_id, (marker, is_active) in owners.items():
            if (run_id != self.run_id and not is_active
                    and not any(self.directory.glob(f"{_PREFIX}{run_id}.*.jsonl")) and _regular(marker)):
                try:
                    marker.unlink()
                except FileNotFoundError:
                    pass

    def _run(self) -> None:
        last_flush = time.monotonic()
        try:
            while not self.closing.is_set() or not self.queue.empty():
                try:
                    job = self.queue.get(timeout=.05)
                except queue.Empty:
                    job = None
                if job is not None:
                    try:
                        self._write(job)
                    except BaseException:
                        with self.lock:
                            self.dropped += 1
                        raise
                    finally:
                        with self.lock:
                            self.pending -= 1
                if time.monotonic() - last_flush >= (self.config.flush_interval if self.config else .5):
                    self._flush()
                    last_flush = time.monotonic()
            self._flush()
        except BaseException as error:
            self._error(error)
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
                with self.lock:
                    self.pending -= 1
                    self.dropped += 1
        finally:
            self._close_files(mark_closed=True)
            if self.owner_created and self.owner and _regular(self.owner):
                try:
                    assert self.directory is not None
                    _safe_directory(self.directory)
                    self.owner.write_bytes(self._owner_data(not self.files_closed))
                    self._prune()
                except Exception as error:
                    self._error(error)
            with self.lock:
                if not self.failed.is_set():
                    self.status = "closed"

    def close(self, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        with self.lock:
            self.closing.set()
        if not self.start_done.wait(max(0, deadline - time.monotonic())):
            self._error(TimeoutError())
            raise RecordingError("Recording startup cleanup timed out; cleanup is unconfirmed.")
        if self.thread is not None:
            self.thread.join(max(0, deadline - time.monotonic()))
            if self.thread.is_alive():
                self._error(TimeoutError())
                raise RecordingError("Recording cleanup timed out; pending data is not confirmed.")
        elif self.enabled and not self.failed.is_set():
            with self.lock:
                self.status = "closed"
        if self.failed.is_set() and self.policy == "fail":
            raise RecordingError("Recording failed (" + (self.error or "unknown") + "); capture is incomplete.")
