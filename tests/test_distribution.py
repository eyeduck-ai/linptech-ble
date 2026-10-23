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
    for relative in ("sensors.local.json", "bridge.local.json", ".private/export.secrets.json", "captures/private.raw.jsonl",
                     "tests/fixtures/private.secret", ".tools/vendor.js", "specs/local-device.json"):
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
        assert any(name.startswith("linptech_ble/specs/") and name.endswith(".json") for name in files)
    assert "docs/LIBRARY.md" in source_data and "examples/library_direct.py" in source_data
    assert "CONTRIBUTING.md" in source_data and "docs/README.md" in source_data
    assert "docs/VALIDATION.md" in source_data
    for name in ("CAPABILITY_API.md", "PIR_INVESTIGATION.md", "HS3BB_WAKE.md"):
        assert f"docs/{name}" in source_data
    assert any(name.endswith("/licenses/LICENSE") for name in wheel_names)
    assert any(name.endswith("/licenses/THIRD_PARTY_NOTICES.md") for name in wheel_names)


def test_distribution_metadata_has_version_license_bounded_core_and_independent_extras(distributions):
    _, wheel, _ = distributions
    with zipfile.ZipFile(wheel) as archive:
        metadata = Parser().parsestr(archive.read(next(n for n in archive.namelist() if n.endswith("/METADATA"))).decode())
    assert metadata["Name"] == "linptech-ble" and metadata["Version"] == "0.4.0"
    assert metadata["License-Expression"] == "MIT"
    assert set(metadata.get_all("Provides-Extra")) >= {"proxy", "ha", "dashboard", "test"}
    from packaging.requirements import Requirement
    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist")]
    core = {item.name: str(item.specifier) for item in requirements if item.marker is None}
    assert set(core) == {"bleak", "xiaomi-ble", "cryptography"}
    assert all("<" in specifier and ">=" in specifier for specifier in core.values())
    assert all(item.marker is not None for item in requirements if item.name in {"paho-mqtt", "bleak-esphome", "aioesphomeapi"})


def test_wheel_imports_without_site_packages_or_application_side_effects(distributions, tmp_path):
    _, wheel, _ = distributions
    # -I -S removes editable source and dependencies: top-level lazy import
    # remains usable without accidentally importing the dashboard or manager.
    code = """
import json, pathlib, socket, sys, threading
sys.path.insert(0, sys.argv[1])
def forbidden(*a, **k):
    raise AssertionError('Unexpected application I/O on import')
socket.socket = forbidden
threading.Thread.start = forbidden
pathlib.Path.read_text = forbidden
import linptech_ble
assert linptech_ble.__version__ == '0.4.0'
assert 'LinptechClient' in linptech_ble.__all__
assert not any(name in sys.modules for name in ('bleak','aioesphomeapi','xiaomi_ble','paho','linptech_ble.dashboard'))
print('wheel-safe-import')
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(wheel)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "wheel-safe-import"
