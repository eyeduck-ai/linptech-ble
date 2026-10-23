"""Application recording uses temporary directories and synthetic payloads only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest

from linptech_ble.recording import AsyncRecordingWriter, RecordingConfig, RecordingError


def records(directory):
    return [json.loads(line) for path in sorted(directory.glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


def managed(directory, **options):
    return AsyncRecordingWriter(config=RecordingConfig(directory=directory, retention_days=0, **options),
                                streams=("events",))


def test_utf8_rotation_respects_encoded_byte_limit_and_close_flushes(tmp_path):
    rows = [{"text": "溫度資料", "index": number} for number in range(5)]
    line_size = len((json.dumps(rows[0], ensure_ascii=False) + "\n").encode("utf-8"))
    writer = managed(tmp_path, max_file_bytes=line_size * 2)
    writer.start()
    for row in rows:
        assert writer.submit({"events": row})
    writer.close()
    paths = sorted(tmp_path.glob("*.jsonl"))
    assert len(paths) == 3 and records(tmp_path) == rows
    assert all(path.stat().st_size <= line_size * 2 for path in paths)
    assert writer.snapshot()["written"] == {"events": 5}
    assert writer.snapshot()["pending"] == writer.snapshot()["queued"] == 0
    assert writer.snapshot()["status"] == "closed"


def test_fixed_paths_are_never_overwritten(tmp_path):
    path = tmp_path / "explicit.jsonl"
    path.write_text("original\n", encoding="utf-8")
    writer = AsyncRecordingWriter(paths={"output": path}, policy="fail")
    with pytest.raises(FileExistsError):
        writer.start()
    assert path.read_text(encoding="utf-8") == "original\n" and writer.thread is None


def test_disabled_writer_creates_no_directory_or_files(tmp_path):
    directory = tmp_path / "not-created"
    writer = AsyncRecordingWriter(config=RecordingConfig(enabled=False, directory=directory))
    writer.start()
    assert writer.submit({"events": {"state": True}}) is False
    writer.close()
    assert not directory.exists() and writer.snapshot()["status"] == "disabled"
    assert writer.snapshot()["accepted"] == writer.snapshot()["dropped"] == 0


@pytest.mark.parametrize("policy", ["drop_newest", "fail"])
def test_bounded_queue_does_not_wait_for_disk_and_reports_loss(tmp_path, monkeypatch, policy):
    writer = AsyncRecordingWriter(config=RecordingConfig(directory=tmp_path, queue_size=1),
                                  streams=("events",), policy=policy)
    entered, release = threading.Event(), threading.Event()
    original = writer._write
    disk_threads = []
    def blocked(job):
        disk_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        original(job)
    monkeypatch.setattr(writer, "_write", blocked)
    writer.start()
    try:
        assert writer.submit({"events": {"number": 1}})
        assert entered.wait(2)
        assert writer.submit({"events": {"number": 2}})
        if policy == "fail":
            with pytest.raises(RecordingError, match="queue is full"):
                writer.submit({"events": {"number": 3}})
        else:
            assert writer.submit({"events": {"number": 3}}) is False
        assert writer.snapshot()["dropped"] == 1
    finally:
        release.set()
        writer.close()
    assert records(tmp_path) == [{"number": 1}, {"number": 2}]
    assert writer.snapshot()["pending"] == 0
    assert all(identity != threading.get_ident() for identity in disk_threads)


def test_submit_encoding_cannot_race_close_into_an_unconsumed_queue(tmp_path):
    writer = managed(tmp_path)
    entered, release = threading.Event(), threading.Event()
    outcomes = []
    class DelayedValue:
        def __str__(self):
            entered.set()
            assert release.wait(3)
            return "synthetic"
    writer.start()
    submitter = threading.Thread(target=lambda: outcomes.append(writer.submit({"events": {"value": DelayedValue()}})))
    submitter.start()
    try:
        assert entered.wait(2)
        writer.close()
    finally:
        release.set()
        submitter.join(3)
    snapshot = writer.snapshot()
    assert outcomes == [False] and not submitter.is_alive()
    assert snapshot["accepted"] == snapshot["pending"] == snapshot["queued"] == 0
    assert snapshot["dropped"] == 1 and snapshot["status"] == "closed"


def test_close_waits_for_slow_start_before_reporting_cleanup(tmp_path, monkeypatch):
    writer = managed(tmp_path)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = writer._open_part
    failures = []
    def slow_open(key):
        entered.set()
        assert release.wait(3)
        original(key)
    def start():
        try:
            writer.start()
        except RecordingError:
            failures.append("cancelled startup")
    monkeypatch.setattr(writer, "_open_part", slow_open)
    starter = threading.Thread(target=start)
    starter.start()
    assert entered.wait(2)
    closer = threading.Thread(target=lambda: (writer.close(), closed.set()))
    closer.start()
    try:
        assert writer.closing.wait(2)
        assert not closed.is_set()
    finally:
        release.set()
        starter.join(3)
        closer.join(3)
    assert closed.is_set() and failures == ["cancelled startup"]
    assert writer.thread is None and writer.snapshot()["pending"] == 0
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    writer.close()
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_close_before_start_prevents_all_files_and_workers(tmp_path):
    directory = tmp_path / "never-created"
    writer = managed(directory)
    writer.close()
    with pytest.raises(RecordingError, match="cannot start"):
        writer.start()
    assert writer.thread is None and not directory.exists()


def test_startup_cleanup_timeout_is_explicit(tmp_path, monkeypatch):
    writer = managed(tmp_path)
    entered, release = threading.Event(), threading.Event()
    failures = []
    original = writer._open_part
    def slow_open(key):
        entered.set()
        assert release.wait(3)
        original(key)
    def start():
        try:
            writer.start()
        except RecordingError:
            failures.append(True)
    monkeypatch.setattr(writer, "_open_part", slow_open)
    starter = threading.Thread(target=start)
    starter.start()
    assert entered.wait(2)
    try:
        with pytest.raises(RecordingError, match="startup cleanup timed out"):
            writer.close(timeout=.01)
    finally:
        release.set()
        starter.join(3)
    assert failures == [True] and writer.thread is None
    assert writer.snapshot()["status"] == "error"


def test_disk_failure_is_visible_and_pending_batches_are_not_claimed_saved(tmp_path, monkeypatch):
    writer = managed(tmp_path)
    def fail(_job):
        raise OSError("SYNTHETIC_PRIVATE_PATH_OR_KEY")
    monkeypatch.setattr(writer, "_write", fail)
    writer.start()
    writer.submit({"events": {"value": 1}})
    assert writer.failed.wait(2)
    assert writer.submit({"events": {"value": 2}}) is False
    writer.close()
    snapshot = writer.snapshot()
    assert snapshot["status"] == "error" and snapshot["error_type"] == "OSError"
    assert snapshot["pending"] == 0 and snapshot["dropped"] == 2
    assert snapshot["written"] == {"events": 0}
    assert "SYNTHETIC_PRIVATE" not in json.dumps(snapshot)


def test_fixed_output_disk_failure_fails_close_instead_of_silent_success(tmp_path, monkeypatch):
    writer = AsyncRecordingWriter(paths={"output": tmp_path / "cli.jsonl"}, policy="fail")
    monkeypatch.setattr(writer, "_write", lambda _job: (_ for _ in ()).throw(OSError("do-not-echo")))
    writer.start()
    writer.submit({"output": {"value": 1}})
    assert writer.failed.wait(2)
    with pytest.raises(RecordingError, match="incomplete") as caught:
        writer.close()
    assert "do-not-echo" not in str(caught.value)


def test_oversized_utf8_record_is_dropped_without_exceeding_part_size(tmp_path):
    writer = managed(tmp_path, max_file_bytes=20)
    writer.start()
    assert writer.submit({"events": {"text": "溫度" * 20}}) is False
    writer.close()
    assert all(path.stat().st_size <= 20 for path in tmp_path.glob("*.jsonl"))
    assert writer.snapshot()["dropped"] == 1 and writer.snapshot()["error_type"] == "RecordTooLarge"


def test_retention_only_prunes_valid_owned_closed_parts_and_ignores_legacy_files(tmp_path):
    original = managed(tmp_path, max_file_bytes=25, max_parts=100)
    original.start()
    for index in range(4):
        original.submit({"events": {"number": index}})
    original.close()
    old_parts = sorted(tmp_path.glob("*.jsonl"))
    assert len(old_parts) == 4
    legacy = tmp_path / "dashboard-20200101.events.jsonl"
    legacy.write_text("legacy private capture", encoding="utf-8")
    fake = tmp_path / ("linptech-recording-v1-" + "a" * 32 + ".events.000001.jsonl")
    fake.write_text("not owned without a marker", encoding="utf-8")
    for path in [*old_parts, legacy, fake]:
        os.utime(path, (1, 1))
    writer = AsyncRecordingWriter(config=RecordingConfig(directory=tmp_path, retention_days=1, max_parts=2),
                                  streams=("events",), clock=lambda: 200000)
    writer.start()
    writer.close()
    assert not any(path.exists() for path in old_parts)
    assert legacy.read_text(encoding="utf-8") == "legacy private capture"
    assert fake.read_text(encoding="utf-8") == "not owned without a marker"
    assert writer.snapshot()["pruned"] == 4


def test_count_retention_removes_proven_closed_parts_but_excludes_active_files(tmp_path):
    active = managed(tmp_path, max_file_bytes=25, max_parts=100)
    active.start()
    for index in range(3):
        active.submit({"events": {"number": index}})
    # Drain deterministically without closing the other writer's owner marker.
    while active.snapshot()["pending"]:
        assert not active.failed.wait(.01)
    protected = active.paths["events"]
    writer = managed(tmp_path, max_parts=1)
    writer.start()
    writer.close()
    assert protected.exists()
    assert len(list(tmp_path.glob("*.jsonl"))) <= 2  # one active plus one closed
    active.close()


def test_retention_never_follows_part_symlinks(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    directory = tmp_path / "recordings"
    directory.mkdir()
    run = "a" * 32
    marker = directory / f"linptech-recording-v1-{run}.owner.json"
    marker.write_text(json.dumps({"format": "linptech-ble-recording-v1", "run_id": run, "active": False}), encoding="ascii")
    link = directory / f"linptech-recording-v1-{run}.events.000001.jsonl"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symbolic links")
    writer = managed(directory, max_parts=1)
    writer.start()
    writer.close()
    assert link.is_symlink() and outside.read_text(encoding="utf-8") == "keep"


def test_retention_never_follows_owner_symlinks(tmp_path):
    run = "a" * 32
    outside = tmp_path / "outside.owner.json"
    contents = json.dumps({"format": "linptech-ble-recording-v1", "run_id": run, "active": False})
    outside.write_text(contents, encoding="ascii")
    directory = tmp_path / "recordings"
    directory.mkdir()
    owner = directory / f"linptech-recording-v1-{run}.owner.json"
    try:
        owner.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symbolic links")
    part = directory / f"linptech-recording-v1-{run}.events.000001.jsonl"
    part.write_text("keep", encoding="ascii")
    part.with_name(part.name + ".closed").write_text(json.dumps({
        "format": "linptech-ble-recording-v1", "part": part.name}), encoding="ascii")
    os.utime(part, (1, 1))
    writer = AsyncRecordingWriter(config=RecordingConfig(directory=directory, retention_days=1),
                                  streams=("events",), clock=lambda: 200000)
    writer.start()
    writer.close()
    assert owner.is_symlink() and part.read_text(encoding="ascii") == "keep"
    assert outside.read_text(encoding="ascii") == contents


def test_recording_directory_symlink_is_rejected_without_touching_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="ascii")
    directory = tmp_path / "recordings"
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symbolic links")
    writer = managed(directory)
    with pytest.raises(RecordingError, match="symbolic links"):
        writer.start()
    assert list(outside.iterdir()) == [sentinel] and sentinel.read_text(encoding="ascii") == "keep"


def test_failed_file_close_never_creates_proof_or_allows_retention(tmp_path):
    writer = managed(tmp_path)
    writer.start()
    real = writer.files["events"]
    class Unclosable:
        def __getattr__(self, name):
            return getattr(real, name)
        def close(self):
            raise OSError("synthetic private path")
    writer.files["events"] = Unclosable()
    try:
        writer.close()
        part = writer.paths["events"]
        assert not real.closed and not writer.files_closed
        assert not part.with_name(part.name + ".closed").exists()
        assert writer.snapshot()["status"] == "error"
        assert writer.snapshot()["error_type"] == "OSError"
        os.utime(part, (1, 1))
        other = AsyncRecordingWriter(config=RecordingConfig(directory=tmp_path, retention_days=1, max_parts=1),
                                     streams=("events",), clock=lambda: 200000)
        other.start()
        other.close()
        assert part.exists()
    finally:
        real.close()


def test_start_failure_closes_other_handles_when_one_close_fails(tmp_path, monkeypatch):
    writer = AsyncRecordingWriter(config=RecordingConfig(directory=tmp_path))
    original = writer._open_part
    real = {}
    class Unclosable:
        def close(self):
            raise OSError("synthetic close failure")
    def fail_after_open(key):
        original(key)
        real[key] = writer.files[key]
        if key == "events":
            writer.files[key] = Unclosable()
        else:
            raise RuntimeError("synthetic startup failure")
    monkeypatch.setattr(writer, "_open_part", fail_after_open)
    try:
        with pytest.raises(RuntimeError, match="startup failure"):
            writer.start()
        assert real["raw"].closed and not real["events"].closed
        assert writer.thread is None and not writer.files_closed
        assert not list(tmp_path.glob("*.closed"))
    finally:
        for stream in real.values():
            stream.close()


def test_managed_filename_collision_never_overwrites_an_existing_owner(tmp_path, monkeypatch):
    import linptech_ble.recording as module
    monkeypatch.setattr(module, "uuid4", lambda: type("Identifier", (), {"hex": "a" * 32})())
    owner = tmp_path / ("linptech-recording-v1-" + "a" * 32 + ".owner.json")
    owner.write_text("preserve this unrelated existing file", encoding="utf-8")
    writer = managed(tmp_path)
    with pytest.raises(FileExistsError):
        writer.start()
    assert owner.read_text(encoding="utf-8") == "preserve this unrelated existing file"


def test_unproven_part_of_unknown_active_run_is_never_pruned(tmp_path):
    run = "a" * 32
    marker = tmp_path / f"linptech-recording-v1-{run}.owner.json"
    marker.write_text(json.dumps({"format": "linptech-ble-recording-v1", "run_id": run, "active": True}), encoding="ascii")
    active = tmp_path / f"linptech-recording-v1-{run}.events.000001.jsonl"
    active.write_text("active/unconfirmed", encoding="utf-8")
    os.utime(active, (1, 1))
    writer = AsyncRecordingWriter(config=RecordingConfig(directory=tmp_path, retention_days=1, max_parts=1),
                                  streams=("events",), clock=lambda: 200000)
    writer.start()
    writer.close()
    assert active.read_text(encoding="utf-8") == "active/unconfirmed"
