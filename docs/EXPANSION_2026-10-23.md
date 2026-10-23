# Model-scoped expansion

The project extends xiaomi-ble through its own model-scoped object catalog and authenticated decoder. It does not modify site-packages or a global device registry.

- Object metadata includes IDs, wire types, MIoT mappings and evidence levels.
- Known malformed values reject the packet; unknown objects/enums remain diagnostic rather than becoming zero or false.
- ES5BB adds radar/configuration profiles, MS1BB stays advertisement-only, and KS2BB/HS3BB expose selected authenticated settings.
- The public async client is independent of the Dashboard. Both share protocol primitives.

Early simulation-only status has been superseded by [hardware tests](HARDWARE_VALIDATION_2026-10-23.md). Use the [current inventory](DEVICE_CAPABILITIES.md), [API guide](LIBRARY.md) and [capability schema](CAPABILITY_API.md) instead of inferring support from this historical milestone.
