# Dashboard validation: 2026-10-22

Historical live Windows BLE session: all four models produced MIC-verified data. No decryption/parsing failures were recorded in the snapshot.

| Model | Observed |
| --- | --- |
| ES5BB | Occupancy true, near-range false and changing illuminance |
| MS1BB | Door open and closed |
| KS2BB | Press event and temperature/humidity updates |
| HS3BB | Motion and changing illuminance |

Initial UI tests covered desktop/mobile layout, filters, pause/resume, keyboard-accessible help and SSE reconnect. Replay used historical packets and was labeled separately from live data. No new battery value was fabricated from earlier sessions.

Current setup: [Windows Dashboard](DASHBOARD_WINDOWS.md). Current support: [device capabilities](DEVICE_CAPABILITIES.md). Direct-connection evidence: [hardware validation](HARDWARE_VALIDATION_2026-10-23.md).
