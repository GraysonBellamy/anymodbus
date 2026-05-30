# Changelog

All notable changes to `anymodbus` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-30

Adds Modbus-ASCII framing, FC08 diagnostic loopback, FC04 input-register typed
reads, and RS485 cold-start ergonomics — the capabilities requested by
`servomexlib` for its `[modbus-ascii]` / `AUTO` milestone. The public API is
additive and backwards-compatible; the internal framing layer was restructured
behind a narrow `Framer` strategy seam so RTU / ASCII / (future) TCP share one
response interpreter.

### Added

- **Modbus-ASCII framing.** `Bus(stream, framing=Framing.ASCII)` selects the
  `:`…hex…LRC…CRLF wire format, sharing the existing PDU/register codec
  unchanged. New `Framing` enum (`RTU` / `ASCII`) and a thin
  `open_modbus_ascii(path, *, baudrate, parity, data_bits=8)` convenience opener
  (`data_bits=7` for the classic 7E1 wire). Sync mirrors included.
- **Pure LRC primitives** at `anymodbus.lrc`: `lrc8`, `lrc8_bytes`, `verify_lrc`
  — mirroring `anymodbus.crc` (submodule-level, not top-level).
- **FC08 diagnostic loopback.** `Slave.diagnostic_loopback(data=b"\x00\x00")`
  (FC 0x08 sub-function 0x0000, Return Query Data) — a side-effect-free
  liveness / round-trip probe. PDU codec in `anymodbus.pdu`; sync mirror.
- **FC04 input registers in the typed reads.** `Slave.read_float` /
  `read_int32` / `read_string` (and sync mirrors) accept
  `source=RegisterSource.INPUT` (FC04) — defaults to `HOLDING` (FC03).
- **`TimingConfig.startup_settle`** — a one-shot idle wait applied once before
  the first transaction, to absorb USB-RS485 adapter warm-up.
- **`ChecksumError`** base exception (parent of `CRCError` and the new
  `LRCError`).
- **`MockSlave` parity:** answers FC08 sub-0 (echo) and can speak ASCII
  (`MockSlave(framing=...)`, `client_slave_pair(framing=...)`), so one register
  bank backs both an RTU and an ASCII bus in hardware-free CI.

### Changed

- Default `RetryPolicy.retry_on` widened from `{CRCError, FrameTimeoutError}` to
  `{ChecksumError, FrameTimeoutError}`, so ASCII `LRCError` retries by default.
  RTU behaviour is unchanged (`CRCError ⊂ ChecksumError`; retry uses
  `isinstance`). Note: a direct membership check `CRCError in
  cfg.retries.retry_on` is now `False` by default even though `CRCError` is
  still retried — only the set membership changed, not the behaviour.
- FC 0x08 reclassified from "known-unsupported" (raised
  `ModbusUnsupportedFunctionError`) to supported (sub-0 only). `CRCError`
  reparented from `ProtocolError` to `ChecksumError` (still a `ProtocolError`
  transitively — `except CRCError` / `except ProtocolError` unaffected).
- Internal: the RTU framer was split into `RtuFramer.read_adu` (reads one raw
  frame) plus a shared `interpret_response_pdu`; the module-level
  `read_response_adu` is preserved as a compatibility wrapper.

No breaking changes to documented public API.

## [0.1.1] - 2026-04-26

First post-release polish, driven by the REVIEW.md notes against v0.1.0
ahead of the first downstream consumers (watlowlib + planned alicatlib /
sartoriuslib). The package is still alpha; backwards-compatibility shims
were not kept.

### Added

- `RegisterType` enum (`int16` / `uint16` / `int32` / `uint32` / `int64` /
  `uint64` / `float32` / `float64` / `string`) and a type-dispatched
  `decode(words, *, type=RegisterType.X, ...)` / `encode(value, *,
  type=RegisterType.X, ...)` pair in `anymodbus.decoders`. Recommended
  entry point for downstream device libraries that load a register schema
  from configuration. `register_count_for(type)` helper for schema callers.
- New per-type helpers: `decode_int16` / `encode_int16`, `decode_int64` /
  `encode_int64`, `decode_float64` / `encode_float64`.
- `decode_string` / `encode_string` gained a `byte_order` keyword for
  devices that store strings byte-swapped within each register.
  `encode_string` now accepts either `register_count` *or* `byte_count` —
  the latter is convenient for spec'd-in-bytes fields whose length isn't
  a multiple of two.
- `Slave.read_string` / `Slave.write_string` (and their sync mirrors)
  accept either `register_count` or `byte_count`, plus `byte_order`.
- `FC` short alias for `FunctionCode` at the top-level package surface,
  fulfilling the long-standing docstring promise.

### Changed

- `TimingConfig.inter_char_timeout` renamed to `TimingConfig.inter_char_idle`
  to match the spec term used throughout DESIGN.md. The framer's keyword
  argument is renamed to match.
- `ModbusUnknownExceptionError` is now a subclass of
  `ModbusExceptionResponse`, so callers wanting "any slave-returned
  exception" can `except ModbusExceptionResponse` and be done. The raw
  byte is exposed on `exception_code` (was: `code`). `code_to_exception`
  now returns `ModbusExceptionResponse` instead of `ModbusError`.

### Removed

- `BusBusyError` from DESIGN.md — it was documented but never
  implemented. The bus lock serializes transactions; the right
  resource-busy signal already comes from `anyio.BusyResourceError` on
  the underlying stream.

## [0.1.0] - 2026-04-25

Initial release. See [DESIGN.md](DESIGN.md) for the full plan.

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

[Unreleased]: https://github.com/GraysonBellamy/anymodbus/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/GraysonBellamy/anymodbus/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/GraysonBellamy/anymodbus/releases/tag/v0.1.0
