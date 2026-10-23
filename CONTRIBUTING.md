# Contributing

Use Python 3.11+ and work from a source checkout:

```powershell
python -m pip install -e ".[test,proxy,cloud]"
python -m pytest -q
python -m build
```

Tests use synthetic credentials/peers and do not require real sensors. Physical BLE tests are separate and must use your own devices. Label evidence as protocol-defined, simulated or hardware-observed; an ACK alone does not prove data or setting application.

[GitHub Actions](https://github.com/eyeduck-ai/linptech-ble/actions/workflows/tests.yml) runs the synthetic suite on Windows and Linux with Python 3.11, 3.12 and 3.13. Jobs install the test/proxy/cloud extras, use no account credentials or BLE hardware, and receive read-only repository permissions. A green matrix establishes software compatibility for those runners, not physical sensor or proxy operation.

Keep decoders model-scoped. Preserve unknown/missing values, MIC validation, replay protection, bounded queues and explicit connection ownership. Keep optional transports and applications out of import-time core behavior.

Never commit device keys, account exports, MAC-specific captures, vendor plugins or real household screenshots. Synthetic fixtures are identified in `tests/conftest.py` and `examples/README.md`. Use the [documentation index](docs/README.md) for API and device limits.
