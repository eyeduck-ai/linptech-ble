"""Linptech MiBeacon decoding and explicit authenticated BLE clients.

Importing this package does not scan, connect, load a config file, start a
thread, install a Bluetooth manager, or import the optional dashboard.
Public objects are imported lazily when explicitly requested.
"""
from importlib import import_module

__version__ = "0.4.0"
_EXPORTS = {
    "AdvertisementRecord": ("models", "AdvertisementRecord"),
    "DeviceConfig": ("config", "DeviceConfig"),
    "Decoder": ("decoder", "Decoder"),
    "object_catalog": ("broadcast_objects", "object_catalog"),
    "capabilities": ("capabilities", "capabilities"),
    "capabilities_schema": ("capabilities", "capabilities_schema"),
    "LinptechClient": ("client", "LinptechClient"),
    "GattClient": ("gatt", "GattClient"),
    "GattConnectionFactory": ("gatt", "GattConnectionFactory"),
    "AdvertisementScanner": ("scanner", "AdvertisementScanner"),
    "PropertyDefinition": ("client", "PropertyDefinition"),
    "PropertyValue": ("client", "PropertyValue"),
    "ClientError": ("client", "ClientError"),
    "BluetoothConfig": ("transport", "BluetoothConfig"),
    "ProxyConfig": ("transport", "ProxyConfig"),
    "record_from_service_info": ("adapters", "record_from_service_info"),
    "decode_service_info": ("adapters", "decode_service_info"),
    "hex_series": ("es5_radar", "hex_series"),
    "bitmap_data": ("es5_radar", "bitmap_data"),
    "radar_bins": ("es5_radar", "radar_bins"),
    "EmptyRoomScan": ("es5_radar", "EmptyRoomScan"),
}
__all__ = ["__version__", *_EXPORTS]


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = _EXPORTS[name]
    module = import_module(f".{module_name}", __name__)
    # Importing a submodule sets its name on the parent package. Resolve all
    # sibling exports together so requesting capabilities_schema first cannot
    # leave the public capabilities name pointing at that module.
    for export, (source, member) in _EXPORTS.items():
        if source == module_name:
            globals()[export] = getattr(module, member)
    return globals()[name]


def __dir__():
    return sorted(set(globals()) | set(__all__))
