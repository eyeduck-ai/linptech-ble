"""Build public artifacts with hostile private-file canaries beside the source."""
from email.parser import Parser
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest


CANARY = "SENSITIVE_PACKAGE_CANARY_never_include"
ROOT = Path(__file__).resolve().parents[1]
PUBLIC_EXAMPLE_CONFIGS = ("transport.example.json", "esphome-proxy-wifi.yaml", "esphome-proxy-ethernet.yaml")
PUBLIC_DOCS = ("README.md", "VALIDATION.md", "LIBRARY.md", "DEVICE_CAPABILITIES.md", "DIRECT_PROTOCOL.md",
    "DEVTOOLS.md", "DASHBOARD_WINDOWS.md", "SIGNAL_QUALITY.md", "ESPHOME_PROXY.md", "BINDKEY_WINDOWS.md",
    "ES5BB_ADVANCED.md", "HARDWARE_VALIDATION.md", "CAPABILITY_API.md", "PIR_INVESTIGATION.md", "HS3BB_WAKE.md")


@pytest.fixture(scope="module")
def distributions(tmp_path_factory):
    pytest.importorskip("build")
    source = tmp_path_factory.mktemp("public-package-source")
    for name in ("pyproject.toml", "README.md", "CONTRIBUTING.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "MANIFEST.in"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "linptech_ble", source / "linptech_ble", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # These are the explicit public docs/examples; no private workspace files
    # are copied into this fixture at any point.
    for directory, pattern in (("docs", "*.md"), ("examples", "library_*.py")):
        (source / directory).mkdir()
        for path in (ROOT / directory).glob(pattern):
            shutil.copy2(path, source / directory / path.name)
    for name in ("README.md", *PUBLIC_EXAMPLE_CONFIGS):
        shutil.copy2(ROOT / "examples" / name, source / "examples" / name)
    for relative in ("sensors.local.json", "transport.local.json", "bridge.local.json", ".private/export.secrets.json", "captures/private.raw.jsonl",
                     "tests/fixtures/private.secret", ".tools/vendor.js", "specs/local-device.json", "examples/bridge.example.json"):
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(CANARY, encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "build", "--no-isolation"], cwd=source,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-6000:]
    return source, next((source / "dist").glob("*.whl")), next((source / "dist").glob("*.tar.gz"))


def test_wheel_and_sdist_exclude_all_private_canaries_and_keep_required_resources(distributions):
    _, wheel, sdist = distributions
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        wheel_data = {name: archive.read(name) for name in wheel_names}
    with tarfile.open(sdist, "r:gz") as archive:
        source_data = {member.name.partition("/")[2]: archive.extractfile(member).read()
                       for member in archive.getmembers() if member.isfile()}
    for files in (wheel_data, source_data):
        assert all(CANARY.encode() not in data for data in files.values())
        assert not any(any(part in {".private", ".tools", "captures", "tests", ".venv"} for part in Path(name).parts)
                       or name.endswith((".local.json", ".secrets.json", ".pyc")) for name in files)
        assert "linptech_ble/decoder.py" in files and "linptech_ble/client.py" in files
        assert "linptech_ble/adapters.py" in files and "linptech_ble/web/index.html" in files
        assert "linptech_ble/transport_config.py" in files
        assert all(f"linptech_ble/{name}" in files for name in ("scanner.py", "gatt.py", "decoded.py", "__init__.pyi", "py.typed"))
        assert "linptech_ble/ha_mqtt.py" not in files and "linptech_ble/bridge_config.py" not in files
        assert any(name.startswith("linptech_ble/specs/") and name.endswith(".json") for name in files)
    assert "examples/library_direct.py" in source_data and "CONTRIBUTING.md" in source_data
    assert "examples/library_scan.py" in source_data and "examples/library_validate_proxy.py" in source_data
    for name in PUBLIC_DOCS:
        assert source_data[f"docs/{name}"] == (ROOT / "docs" / name).read_bytes()
    assert "examples/README.md" in source_data
    for name in PUBLIC_EXAMPLE_CONFIGS:
        assert source_data[f"examples/{name}"] == (ROOT / "examples" / name).read_bytes()
    assert "examples/bridge.example.json" not in source_data
    assert any(name.endswith("/licenses/LICENSE") for name in wheel_names)
    assert any(name.endswith("/licenses/THIRD_PARTY_NOTICES.md") for name in wheel_names)


def test_distribution_metadata_has_version_license_bounded_core_and_independent_extras(distributions):
    _, wheel, _ = distributions
    with zipfile.ZipFile(wheel) as archive:
        metadata = Parser().parsestr(archive.read(next(n for n in archive.namelist() if n.endswith("/METADATA"))).decode())
    assert metadata["Name"] == "linptech-ble" and metadata["Version"] == "0.4.0"
    assert metadata["License-Expression"] == "MIT"
    extras = set(metadata.get_all("Provides-Extra"))
    assert extras >= {"proxy", "cloud", "dashboard", "test"}
    assert not extras & {"ha", "bridge", "mqtt"}
    from packaging.requirements import Requirement
    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist")]
    core = {item.name: str(item.specifier) for item in requirements if item.marker is None}
    assert set(core) == {"bleak", "xiaomi-ble", "cryptography"}
    assert all("<" in specifier and ">=" in specifier for specifier in core.values())
    assert not any(item.name in {"paho-mqtt", "homeassistant"} for item in requirements)
    proxy = {item.name for item in requirements if item.marker and item.marker.evaluate({"extra": "proxy"})}
    assert proxy == {"bleak-esphome", "aioesphomeapi"}
    # The suite builds with --no-isolation, so fresh environments must receive
    # the declared backend in the test extra, not only in pip's build sandbox.
    import tomllib
    backend = {Requirement(value).name: str(Requirement(value).specifier)
               for value in tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]["requires"]}
    test_dependencies = {item.name: str(item.specifier) for item in requirements
                         if item.marker and item.marker.evaluate({"extra": "test"})}
    assert all(test_dependencies.get(name) == specifier for name, specifier in backend.items())


def test_wheel_imports_without_site_packages_or_application_side_effects(distributions, tmp_path):
    _, wheel, _ = distributions
    # -I -S removes editable source and dependencies: top-level lazy import
    # remains usable without accidentally importing the dashboard or manager.
    code = """
import json, pathlib, socket, sys, threading
sys.path.insert(0, sys.argv[1])
def forbidden(*a, **k):
    raise AssertionError('Unexpected application I/O on import')
class ForbiddenSocket(socket.socket):
    def __init__(self, *a, **k):
        forbidden()
socket.socket = ForbiddenSocket
threading.Thread.start = forbidden
pathlib.Path.read_text = forbidden
import linptech_ble
assert linptech_ble.__version__ == '0.4.0'
assert 'LinptechClient' in linptech_ble.__all__
from linptech_ble import AdvertisementScanner, GattClient, GattConnectionFactory, DecodedAdvertisement, DecodeStatus
scanner = AdvertisementScanner()
assert scanner.health()['listening'] is False
assert not any(name in sys.modules for name in ('bleak','aioesphomeapi','xiaomi_ble','paho','linptech_ble.dashboard'))
print('wheel-safe-import')
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(wheel)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "wheel-safe-import"
