"""Exercise real Windows spawn/IPC without importing or accessing BLE hardware."""
import time
import base64

from linptech_ble.es5_worker import ProcessDirectController
from linptech_ble.config import DeviceConfig
from linptech_ble.transport import BluetoothConfig, ProxyConfig


def wait_until(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError("Worker did not respond before deadline")


def test_process_worker_returns_errors_and_recovers_from_worker_death(tmp_path):
    c = ProcessDirectController(DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Synthetic"), recording_dir=tmp_path)
    c.start()
    try:
        # Refresh without a session fails locally; it must never touch a radio.
        op = c.submit({"action": "refresh"})
        wait_until(lambda: c.snapshot()["last_operation"]["status"] == "error")
        assert c.snapshot()["last_operation"]["id"] == op
        assert not c.snapshot()["busy"]
        first = c.worker.pid
        c.worker.terminate()
        wait_until(lambda: c.generation == 2)
        assert c.worker.pid != first and not c.snapshot()["authenticated"]
        assert c.snapshot()["properties"]["motion_energy"]["value"] is None
        assert c.snapshot()["error"]
    finally:
        c.stop()
    assert not c.worker.is_alive()


def test_worker_restart_cancels_scan_without_blocking_reconnect(tmp_path):
    c = ProcessDirectController(None, recording_dir=tmp_path)
    c._cal_state(phase="scanning")
    c._retire = lambda: None
    c._spawn = lambda: None
    c._restart()
    assert c.snapshot()["calibration"]["phase"] == "cancelled"
    assert not c.snapshot()["busy"]


def test_proxy_transport_uses_same_process_thread_without_exposing_credentials(tmp_path):
    psk = base64.b64encode(bytes(range(32))).decode()
    transport = BluetoothConfig("esphome", (ProxyConfig("bedroom", "192.0.2.1", noise_psk=psk),))
    c = ProcessDirectController(DeviceConfig("AA:BB:CC:DD:EE:01", "ES5BB", "Synthetic"),
        recording_dir=tmp_path, transport=transport)
    c.start()
    try:
        # No connect action: exercise controller routing without network I/O.
        op = c.submit({"action": "refresh"})
        wait_until(lambda: c.snapshot()["last_operation"]["status"] == "error")
        state = c.snapshot()
        assert state["last_operation"]["id"] == op
        assert state["transport"]["backend"] == "esphome"
        assert state["transport"]["proxies"] == ["bedroom"]
        assert psk not in str(state)
    finally:
        c.stop()
    assert c.worker is None and not c.thread.is_alive()


def test_stop_is_idempotent_before_start_and_after_partial_spawn(tmp_path):
    c = ProcessDirectController(None, recording_dir=tmp_path)
    c.stop()
    c.stop()
    c = ProcessDirectController(None, recording_dir=tmp_path)
    c.worker = c.context.Process(target=lambda: None)
    c.thread = __import__("threading").Thread(target=lambda: None)
    c.stop()
    c.stop()
    remote = ProcessDirectController(None, recording_dir=tmp_path,
        transport=BluetoothConfig("esphome", (ProxyConfig("room", "192.0.2.1"),)))
    remote.stop()
    remote.stop()
