# Changelog

All notable changes to `anymodbus` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Initial v0.1 release. See [DESIGN.md](DESIGN.md) for the full plan.

### Added

- Repository skeleton: `pyproject.toml`, `Makefile`, CI/docs/publish workflows,
  pre-commit config, src/tests/docs layout.
- Foundational pure-Python pieces:
  `anymodbus._types` (enums + small dataclasses),
  `anymodbus.exceptions` (full `ModbusError` tree + `code_to_exception` translator),
  `anymodbus.crc` (CRC-16/Modbus, 256-entry table),
  `anymodbus.config` (frozen dataclasses with validation).
- `WordOrder` and `ByteOrder` named enums for 32-bit value layout, with
  defaults matching the Modbus Application Protocol spec example
  (high-word-first, big-endian within word).
- `Bus` (half-duplex single-master client) and per-slave `Slave` handle, with
  `open_modbus_rtu(...)` convenience opener over `anyserial`.
- Length-aware RTU `Framer` with timing-gap fallback for unknown function codes
  and tx-side 3.5-character pre-tx idle gap.
- PDU encode/decode for the v0.1 function code set (FC 1–6, 15, 16) and
  `decoders` for 32-bit float / int values across two registers.
- `Capability` model (`SUPPORTED`/`UNSUPPORTED`/`UNKNOWN`) populated from probe
  results.
- Sync wrapper (`anymodbus.sync`) using the same shared-portal pattern as
  `anyserial.sync`.
- `anymodbus.testing` with `MockSlave`, `FaultPlan`, and `client_slave_pair()`
  for hardware-free integration tests.

[Unreleased]: https://github.com/GraysonBellamy/anymodbus/compare/HEAD...HEAD
