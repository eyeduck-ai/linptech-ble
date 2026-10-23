# HS3BB direct-connection wake

A **short physical-button press followed by immediate connection** worked on the tested HS3BB. Keep the phone disconnected while the computer takes over. Do not long-press, reset or rebind the sensor.

## Evidence: 2026-10-23

- Before pressing: fresh Windows primary type 3 advertisements, `connectable=false`.
- After the user confirmed a short press: fresh primary type 0, `connectable=true`; Standard Auth and two reads succeeded immediately.
- Read values were `indicator_light=true` and `detection_interval=30` seconds. No settings were written and GATT was closed.

The observations came from separate scan windows, not a continuous recording of the button transition. They support wake state as a cause of earlier failures, without proving a universal requirement, a fixed wake duration or the sole cause of every timeout.

## Procedure

1. Place the sensor near the computer or selected proxy, with a working battery.
2. Exit its Mi Home device page; temporarily disable phone Bluetooth if it occupies GATT.
3. Prepare the client, short-press once and connect immediately.
4. Supply that device's **12-byte Standard Auth token**, not its advertisement bindkey.
5. For writes, independently confirm `miot_model="linp.motion.hs1bb2"`. Read before writing, verify readback and close normally.

The library does not wake the hardware remotely or retry indefinitely. ESPHome extends range; it does not keep a sleeping sensor connectable. See [device capabilities](DEVICE_CAPABILITIES.md) and [hardware validation](HARDWARE_VALIDATION.md).
