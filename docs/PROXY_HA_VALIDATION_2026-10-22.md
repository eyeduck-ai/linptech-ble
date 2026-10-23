# ESPHome / MQTT simulation evidence

Tests used the real `bleak-esphome` backend with a simulated Native API peer. No physical ESP32, Mosquitto deployment or Home Assistant instance was tested.

- One API owner routes advertisements and GATT; simultaneous local/remote paths and partial disconnects were exercised.
- Standard Auth, encrypted properties, typed writes and readback used production protocol code with synthetic peers.
- MQTT tests used a real paho client against a local TCP protocol peer: CONNECT, LWT, discovery, commands, events and clean shutdown.
- Missing/stale values remained unavailable. Retained commands and old button-event replay were rejected.
- Synthetic UI checks covered proxy status, desktop/mobile layout and error recovery; they are not hardware evidence.

ESPHome YAML examples were reviewed against source, but were not compiled or flashed. Proxy latency, long-term stability, cross-proxy handover and HA frontend rendering remain unverified. See [setup](ESPHOME_HOME_ASSISTANT.md) and [current validation](VALIDATION.md).
