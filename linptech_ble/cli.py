"""Windows-friendly command line tools for local Linptech BLE testing."""

from __future__ import annotations

import argparse
import asyncio
import json
from getpass import getpass
from importlib import metadata
import sys

from .config import DeviceConfig, MIOT_MODELS, load_config, normalize_address, save_config, validate_auth_token, validate_bindkey
from .runtime import capture, replay


MODELS = ("ES5BB", "MS1BB", "KS2BB", "HS3BB")


def _seconds(value: str) -> float:
    seconds = float(value)
    if seconds < 0 or seconds == float("inf") or seconds != seconds:
        raise argparse.ArgumentTypeError("秒數必須是有限的非負數；0 表示持續執行。")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本機 Linptech BLE 廣播掃描、解密與功能測試")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="擷取原始廣播，預設只保留 Linptech 候選裝置")
    scan.add_argument("--seconds", type=_seconds, default=60)
    scan.add_argument("--output", default="captures/scan.jsonl")
    scan.add_argument("--all", action="store_true", help="保留所有 BLE 裝置的廣播")
    scan.add_argument("--address", help="只保留指定 MAC，包含尚未辨識的型號")
    scan.add_argument("--bridge-config", help="本機／ESPHome 連線設定 JSON；省略時使用本機藍牙")

    monitor = commands.add_parser("monitor", help="持續解碼廣播並記錄狀態")
    monitor.add_argument("--config", default="sensors.local.json")
    monitor.add_argument("--seconds", type=_seconds, default=0)
    monitor.add_argument("--output", default="captures/events.jsonl")
    monitor.add_argument("--raw-output", help="同步保存原始包，供離線重播")
    monitor.add_argument("--address", help="只處理指定 MAC")
    monitor.add_argument("--configured-only", action="store_true", help="只記錄設定檔內的裝置")
    monitor.add_argument("--bridge-config", help="本機／ESPHome 連線設定；MQTT 發布請使用 dashboard")

    offline = commands.add_parser("replay", help="離線重新解析 scan 或 raw-output JSONL")
    offline.add_argument("input", metavar="INPUT")
    offline.add_argument("--config", default="sensors.local.json")
    offline.add_argument("--output", help="寫入新檔；省略時逐筆輸出至終端")

    add = commands.add_parser("add-device", help="新增或更新裝置設定")
    add.add_argument("--config", default="sensors.local.json")
    add.add_argument("--address", required=True)
    add.add_argument("--model", choices=MODELS, required=True)
    add.add_argument("--name")

    remove = commands.add_parser("remove-device", help="Remove a sensor from local configuration only")
    remove.add_argument("--config", default="sensors.local.json")
    remove.add_argument("--address", required=True)

    key = commands.add_parser("set-key", help="用不回顯的提示輸入 bindkey")
    key.add_argument("--config", default="sensors.local.json")
    key.add_argument("--address", required=True)

    token = commands.add_parser("set-token", help="用不回顯的提示輸入 12-byte Standard Auth 直連 token")
    token.add_argument("--config", default="sensors.local.json")
    token.add_argument("--address", required=True)
    token.add_argument("--miot-model", choices=tuple(MIOT_MODELS.values()), help="已核對的米家協定型號；HS3BB 設定寫入需要此確認")

    diagnostics = commands.add_parser("doctor", help="檢查 Python、依賴與選用的連線設定，不連接硬體")
    diagnostics.add_argument("--bridge-config", help="另外檢查 ESPHome／MQTT 設定與選用套件")
    inventory = commands.add_parser("capabilities", help="列出四款廣播與直連能力、來源及驗證界線，不讀取密鑰或連線")
    inventory.add_argument("--model", choices=MODELS)
    importer = commands.add_parser("import-keys", help="匯入 Token Extractor 本機 JSON，只保留這四款感測器")
    importer.add_argument("input", metavar="INPUT")
    importer.add_argument("--config", default="sensors.local.json")
    importer.add_argument("--include-tokens", action="store_true", help="額外匯入這四款可用的 12-byte Standard Auth 直連 token")
    dashboard = commands.add_parser("dashboard", help="啟動四款感測器的本機即時監控網頁")
    dashboard.add_argument("--config", default="sensors.local.json")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--open", action="store_true", help="自動開啟預設瀏覽器")
    dashboard.add_argument("--replay", metavar="RAW_JSONL", help="離線重播原始廣播，不啟動藍牙")
    dashboard.add_argument("--bridge-config", help="ESPHome／MQTT 連線設定；與 sensors 設定分開保存")
    dashboard.add_argument("--stale-after", type=_seconds, default=180,
                           help="多久未收到廣播標為較久未更新；預設 180 秒，不表示離線")
    return parser


def doctor(bridge_path: str | None = None) -> int:
    print(f"Python：{sys.version.split()[0]} ({sys.executable})")
    if sys.platform == "win32":
        version = sys.getwindowsversion()
        system = f"Windows {version.major}.{version.minor} build {version.build}"
    else:
        system = sys.platform
    # Avoid WMI-backed platform queries: these can block on some Windows hosts.
    print(f"系統：{system}")
    missing = False
    remote_mode = False
    for package in ("bleak", "xiaomi-ble", "bluetooth-sensor-state-data"):
        try:
            version = metadata.version(package)
        except metadata.PackageNotFoundError:
            version = "未安裝"
            missing = True
        print(f"{package}：{version}")
    if bridge_path is not None:
        from .bridge_config import load_bridge_config, public_transport
        bridge = load_bridge_config(bridge_path)
        remote_mode = bridge.bluetooth.backend == "esphome"
        print("連線設定：" + json.dumps(public_transport(bridge.bluetooth), ensure_ascii=False))
        print(f"Home Assistant MQTT：{'啟用' if bridge.mqtt.enabled else '停用'}")
        optional = (["bleak-esphome", "aioesphomeapi"] if bridge.bluetooth.backend == "esphome" else [])
        if bridge.mqtt.enabled:
            optional.append("paho-mqtt")
        for package in optional:
            try:
                print(f"{package}：{metadata.version(package)}")
            except metadata.PackageNotFoundError:
                print(f"{package}：未安裝（請安裝 .[bridge] 或 requirements-bridge.txt）")
                missing = True
    if remote_mode:
        print("本指令僅檢查設定與套件，未連接 ESPHome、MQTT broker 或感測器；網路代理不需要主機藍牙。")
    else:
        print("本指令只檢查軟體環境，未驗證藍牙硬體、驅動、接收範圍或感測器。")
        print("Windows：請開啟藍牙；確認隱私權設定允許此電腦及桌面應用程式使用藍牙。")
        print("請從一般 PowerShell 執行本程式；不需在 Windows 設定先配對感測器。")
    if sys.version_info < (3, 11):
        print("需要 Python 3.11 或更新版本。", file=sys.stderr)
        return 1
    return int(missing)


def _add_device(args: argparse.Namespace) -> int:
    address = normalize_address(args.address)
    devices = load_config(args.config)
    previous = next((device for device in devices if device.address == address), None)
    item = DeviceConfig(
        address=address,
        model=args.model,
        name=args.name or (previous.name if previous else args.model),
        bindkey=previous.bindkey if previous else None,
        auth_token=previous.auth_token if previous else None,
        miot_model=previous.miot_model if previous and previous.model == args.model else None,
    )
    devices = [device for device in devices if device.address != address] + [item]
    save_config(args.config, devices)
    print(f"已儲存 {address} ({args.model}) 至 {args.config}")
    if args.model == "HS3BB":
        print("HS3BB 使用專案本機型號對應，可解析已驗證的移動與照度廣播。")
    return 0


def _set_key(args: argparse.Namespace) -> int:
    address = normalize_address(args.address)
    devices = load_config(args.config)
    previous = next((device for device in devices if device.address == address), None)
    if previous is None:
        raise ValueError("此地址尚未加入設定；請先執行 add-device。")
    if not sys.stdin.isatty():
        raise ValueError("請在互動式 PowerShell 執行 set-key；不接受管線輸入密鑰。")
    bindkey = validate_bindkey(getpass("輸入 32 位十六進位 bindkey（不會回顯）："))
    updated = DeviceConfig(address=address, model=previous.model, name=previous.name, bindkey=bindkey,
                           auth_token=previous.auth_token, miot_model=previous.miot_model)
    devices = [updated if device.address == address else device for device in devices]
    save_config(args.config, devices)
    print(f"已更新 {address} 的 bindkey。設定檔內含明文密鑰，請保留在本機。")
    return 0


def _remove_device(args: argparse.Namespace) -> int:
    address = normalize_address(args.address)
    devices = load_config(args.config)
    remaining = [device for device in devices if device.address != address]
    if len(remaining) == len(devices):
        raise ValueError(f"No configured device at {address}; configuration was not changed.")
    save_config(args.config, remaining)
    print(f"Removed {address} from {args.config}. Restart the dashboard to reload configuration.")
    return 0


def _set_token(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        raise ValueError("請在互動式 PowerShell 執行 set-token；不接受管線輸入密鑰。")
    address = normalize_address(args.address)
    devices = load_config(args.config)
    previous = next((device for device in devices if device.address == address), None)
    if previous is None:
        raise ValueError("找不到此裝置；請先新增感測器。")
    model = args.miot_model or previous.miot_model
    if model is not None and model != MIOT_MODELS[previous.model]:
        raise ValueError("協定型號與此裝置不符；未變更設定。")
    token = validate_auth_token(getpass("Standard Auth token（24 個 hex 字元，不回顯）："))
    updated = DeviceConfig(address, previous.model, previous.name, previous.bindkey, token, model)
    save_config(args.config, [updated if device.address == address else device for device in devices])
    print("已保存此裝置的直連憑證；尚未連線或寫入感測器設定。")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor(args.bridge_config)
        if args.command == "capabilities":
            from .capabilities import capabilities
            print(json.dumps(capabilities(args.model), ensure_ascii=False, indent=2))
            return 0
        if args.command == "dashboard":
            from .dashboard import serve_dashboard
            if not 1 <= args.port <= 65535 or args.stale_after <= 0:
                raise ValueError("port 必須介於 1～65535；stale-after 必須大於 0。")
            serve_dashboard(config=args.config, port=args.port, open_browser=args.open,
                            replay_path=args.replay, stale_after=args.stale_after,
                            bridge_path=args.bridge_config)
            return 0
        if args.command == "import-keys":
            from .xiaomi_import import import_keys
            print(json.dumps(import_keys(args.input, args.config, include_tokens=args.include_tokens), ensure_ascii=False))
            return 0
        if args.command == "add-device":
            return _add_device(args)
        if args.command == "remove-device":
            return _remove_device(args)
        if args.command == "set-key":
            return _set_key(args)
        if args.command == "set-token":
            return _set_token(args)
        if args.command == "replay":
            replay(args.input, load_config(args.config), args.output)
            return 0
        transport = None
        if args.bridge_config:
            from .bridge_config import load_bridge_config
            bridge = load_bridge_config(args.bridge_config)
            if bridge.mqtt.enabled or bridge.es5_auto_connect:
                raise ValueError("scan／monitor 僅擷取廣播；啟用 MQTT 或 ES5 自動直連時請使用 dashboard。")
            transport = bridge.bluetooth
        options = {"transport": transport} if transport is not None else {}
        asyncio.run(
            capture(
                mode=args.command,
                seconds=args.seconds,
                output=args.output,
                devices=load_config(args.config) if args.command == "monitor" else [],
                raw_output=getattr(args, "raw_output", None),
                all_devices=getattr(args, "all", False),
                address=args.address,
                configured_only=getattr(args, "configured_only", False),
                **options,
            )
        )
        return 0
    except KeyboardInterrupt:
        print("\n已停止，擷取檔已關閉。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"錯誤：{error}", file=sys.stderr)
        if args.command in {"scan", "monitor"}:
            if args.bridge_config:
                print("請先執行 doctor --bridge-config 檔名；確認代理網路、API 金鑰及廣播訂閱歸屬。", file=sys.stderr)
            else:
                print("請先執行 doctor；確認藍牙已開啟、驅動正常，並關閉其他正在掃描的程式。", file=sys.stderr)
        return 1
