# Signal quality and receiver placement

The dashboard's signal-quality view tracks received advertisements for each configured sensor. RSSI is the receiver's measured signal level in dBm; a less-negative value is stronger. It is useful for comparing placements with the same sensor and receiver, but does not measure distance or prove authenticated decoding, GATT availability or event delivery.

## Read the display

The view shows recent RSSI samples, latest/median/minimum/maximum levels, sample count, last reception and the longest observed gap. Statistics cover the most recent five minutes, within the bounded in-memory history. Select one receiver when comparing placements; different adapters/proxies can report different RSSI values.

The following bands are this dashboard's practical guide, not Bluetooth acceptance limits:

| RSSI | Display band | Placement guidance |
| --- | --- | --- |
| At least -60 dBm | Strong | Good signal margin; still verify events |
| -75 to below -60 dBm | Fair | A useful installation target |
| -90 to below -75 dBm | Weak | Test carefully; improve placement if possible |
| Below -90 dBm | Poor | Little margin; avoid for important sensors when events are missed |

Aim for about **-75 dBm or stronger** during ordinary use. Test -76 to -85 dBm carefully; readings near or below -90 dBm warrant moving or adding a receiver. Repeated missed events during controlled tests make a placement unacceptable regardless of its displayed band. These are project heuristics, not guaranteed device limits.

Do not optimize a single number. Bluetooth Core 6.2 recommends RSSI accuracy of +/-6 dB, so a small difference between samples or adapters may not be meaningful. See [Bluetooth Core, Vol 6, Part A, section 4.8](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-62/out/en/low-energy-controller/radio-physical-layer-specification.html).

## Compare candidate locations

1. Keep each sensor in its intended position. Move the receiver or proxy to a candidate position and let the signal view collect samples for **two to five minutes**.
2. Test with doors closed, normal furniture and people present. Compare the median, weaker samples and history rather than the strongest sample.
3. Repeatedly open/close MS1BB, press KS2BB, and move in front of ES5BB/HS3BB. Confirm the corresponding decoded events. Respect the sensor's configured detection interval and clearing delay; environmental and battery reports may take longer.
4. Repeat at the next location, then validate the chosen placement over normal household use. If GATT settings are needed, separately test wake, authentication, reads and cleanup there.

Quiet battery sensors may not advertise at a fixed known interval. Sample counts and gaps are observations, **not packet-loss percentages**; the scanner cannot count transmissions it never received. Old readings remain historical, and no samples mean unknown quality. Replay is for interface testing and cannot validate a current placement.

## Using an ESPHome proxy

RSSI describes the **sensor-to-proxy BLE link**, not the proxy's Wi-Fi signal or network reliability. Check both links independently. Place the proxy close to the relevant sensors and away from network equipment. ESPHome recommends Ethernet to free the ESP32 radio from Wi-Fi traffic, external antennas where available, and default scan parameters. See [ESPHome Bluetooth Proxy reception guidance](https://esphome.io/components/bluetooth_proxy/#improving-reception-performance) and this project's [proxy setup](ESPHOME_PROXY.md).
