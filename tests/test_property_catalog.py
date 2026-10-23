"""The shared schema can be inspected without loading dashboard workers."""
from __future__ import annotations

import subprocess
import sys


def test_catalog_import_does_not_load_controller_or_dashboard_modules():
    code = """
import sys
from linptech_ble.property_catalog import CATALOG, PROFILES
assert CATALOG['pir_energy'].notification_only
assert set(PROFILES) == {'MS1BB', 'KS2BB', 'HS3BB'}
assert not set(sys.modules) & {
    'linptech_ble.es5_direct', 'linptech_ble.sensor_direct',
    'linptech_ble.es5_worker', 'linptech_ble.dashboard',
}
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_legacy_controller_paths_reexport_the_same_catalog_objects():
    from linptech_ble import es5_direct, property_catalog, sensor_direct

    for name in ("Property", "PROPERTIES", "CATALOG", "PAIR_CATALOG"):
        assert getattr(es5_direct, name) is getattr(property_catalog, name)
    for name in ("SensorProperty", "PROFILES", "PROTOCOL_MODELS"):
        assert getattr(sensor_direct, name) is getattr(property_catalog, name)
