# Device credentials on Windows

ES5BB, MS1BB, KS2BB and HS3BB use per-device credentials. A Xiaomi gateway is not required for local BLE reception or authenticated GATT, but that does not guarantee account-free credential extraction.

| Credential | Purpose | Required form |
| --- | --- | --- |
| Bindkey / BLE KEY | Decrypt authenticated MiBeacon advertisements | 16 bytes, 32 hexadecimal characters |
| Standard Auth token | Authenticate supported GATT operations | 12 bytes, 24 hexadecimal characters |

These credentials are not interchangeable. A generic cloud `TOKEN`, Windows pairing key or Bluetooth address is not automatically the required credential. Resetting, rebinding or offline activation may change the active keys.

## Existing configured checkout

If your own devices are already configured, keep using the local `sensors.local.json`. Monitoring does not require another login. The public repository and distribution do not contain credentials for physical devices.

The dashboard's **Add sensor** and per-device **Name and credentials** form can save keys you already obtained. Blank fields preserve saved credentials; the browser never receives them back. Nearby scanning identifies devices and RSSI, not their keys. The optional QR workflow below retrieves credentials from your own Xiaomi Home account.

## Dashboard QR login

Install optional cloud dependencies and start the dashboard:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[cloud]"
.\start-dashboard.cmd
```

Select **Xiaomi Home keys**, choose the same region as the app, then start login. Scan the QR with Xiaomi Home and confirm on your phone. Choose the supported devices and select **Get and save selected**. Refresh an expired QR from the same panel.

Only selected devices are merged into the active local configuration. Existing names and unselected devices are preserved. A missing bindkey or compatible direct token does not clear a saved value. Generic 32-character cloud tokens are ignored for Standard Auth; GATT requires exactly 24 hexadecimal characters. Disconnect a selected device before replacing its credentials.

Account login is optional and temporary: the dashboard keeps its session in server memory and exposes only device metadata and credential-status flags to the page. Device keys are saved to the private configuration file. No Xiaomi gateway is involved, and subsequent local monitoring does not need a cloud session. This is a community protocol integration, not an official Xiaomi account SDK.

## Obtain your own credentials

Account-free scanning can identify product IDs and record encrypted advertisements. It cannot derive an arbitrary device's bindkey from those packets. No account-free extraction/activation method has been verified across all four models and firmware versions, and no universal unauthenticated GATT key-reading characteristic has been established.

[Mi Activation](https://atc1441.github.io/Temp_universal_mi_activate.html) is a separate experimental provisioning tool, not a passive extractor of an existing key. Activation can replace the current binding. This project does not recommend resetting a working sensor or flashing unrelated firmware to try it.

For account-based retrieval, first bind each physical sensor to your own Xiaomi Home account. Match the chosen region to the region where the devices were added. The dashboard can use the QR workflow above; alternatively, obtain an authorized export separately using the upstream [Xiaomi Cloud Tokens Extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor). The project does not bundle or launch its executable. Core decoding and direct connections do not log into an account.

Match each export entry to the actual device and address. Use its `BLE KEY` for advertisements. Import only a correctly sized Standard Auth token for GATT; an arbitrary token field is insufficient.

## Import an existing private export

Run from the project folder:

```powershell
$exportFile = Read-Host 'Path to your private extractor JSON export'
.\.venv\Scripts\python.exe -m linptech_ble import-keys $exportFile --include-tokens
```

The importer selects these four models, merges by address and reports a credential-free summary. Omitting `--include-tokens` imports bindkeys while preserving existing direct tokens. Keep the original export private: it can contain credentials for other devices in the same account.

## Enter credentials through hidden prompts

Use interactive PowerShell. The following example configures an ES5BB using your real observed address:

```powershell
$address = Read-Host 'Sensor Bluetooth MAC address'
.\.venv\Scripts\python.exe -m linptech_ble add-device --address $address --model ES5BB --name 'Living room'
.\.venv\Scripts\python.exe -m linptech_ble set-key --address $address
.\.venv\Scripts\python.exe -m linptech_ble set-token --address $address --miot-model linp.sensor_occupy.es5b
```

`set-key` and `set-token` do not echo input or accept secret values in command-line arguments. The resulting local JSON still stores plaintext credentials; protect that file. `--config` selects a different private credential file.

Use the correct model for each device:

| Product | Advertisement product ID | MIoT model |
| --- | --- | --- |
| ES5BB | `0x6A0F` | `linp.sensor_occupy.es5b` |
| MS1BB | `0x1889` | `linp.magnet.m1` |
| KS2BB | `0x520B` | `linp.sensor_ht.ks2bb` |
| HS3BB | `0x50D4` | `linp.motion.hs1bb2` |

HS3BB writes require explicit confirmation of its MIoT model, for example `set-token --address $address --miot-model linp.motion.hs1bb2` after verifying your device's model. The released MS1BB profile has no exposed GATT settings, so its normal integration needs only the bindkey.

## Verify locally

Start the [dashboard](DASHBOARD_WINDOWS.md) or capture a short monitor session. Success requires authenticated decryption and values/events that agree with physical actions, not merely a connected radio or a key-shaped string.

If decryption fails, check the device/address mapping, account region, export age and whether binding changed. If GATT fails, distinguish a sleeping/unconnectable sensor from an authentication failure. Do not reset or re-export credentials solely because no connectable advertisement is visible.

Do not commit local settings, complete exports, account sessions or real recordings. For shared bug reports, provide model, firmware when verified, error phase and sanitized diagnostics. See [developer tools](DEVTOOLS.md), [ES5 controls](ES5BB_ADVANCED.md) and [HS3 wake-up behavior](HS3BB_WAKE.md).
