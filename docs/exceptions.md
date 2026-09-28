# Exceptions

`anymodbus` exposes a typed exception hierarchy where every class multi-inherits from the most natural standard-library or AnyIO base. Code that already catches `ValueError`, `TimeoutError`, `OSError`, `anyio.BrokenResourceError`, etc., picks up the right `anymodbus` exceptions without new `except` clauses.

## Hierarchy

```
ModbusError                          (Exception)
├── ConfigurationError               (ValueError)        ← bad argument; nothing sent
├── ProtocolError                    (ValueError)        ← bad bytes on the wire
│   ├── ChecksumError
│   │   ├── CRCError                                     ← RTU CRC-16 mismatch
│   │   └── LRCError                                     ← ASCII LRC mismatch
│   ├── FrameError                                       ← truncated / malformed frame
│   └── UnexpectedResponseError                          ← reply doesn't answer the request
├── FrameTimeoutError                (TimeoutError)
├── ConnectionLostError              (anyio.BrokenResourceError)
│   └── TransportError               (OSError)           ← other OS-level port failure
├── BusClosedError                   (anyio.ClosedResourceError)
├── ModbusUnsupportedFunctionError   (NotImplementedError)
└── ModbusExceptionResponse          (slave-returned exception codes)
    ├── IllegalFunctionError
    ├── IllegalDataAddressError
    ├── IllegalDataValueError
    ├── SlaveDeviceFailureError
    ├── AcknowledgeError
    ├── SlaveDeviceBusyError
    ├── MemoryParityError
    ├── GatewayPathUnavailableError
    ├── GatewayTargetFailedToRespondError
    └── ModbusUnknownExceptionError                      ← any other code, e.g. legacy 0x07
```

`ConfigurationError` and `ProtocolError` both inherit `ValueError`, but the split is meaningful: `ConfigurationError` is raised before anything is sent (a bad `BusConfig` value, a slave address out of range, a register count above the spec maximum, a value that doesn't fit in a register), while `ProtocolError` is reserved for byte-level problems with what came back. Catching one independently of the other is usually what you want.

## When to expect what

| Situation | Exception |
|---|---|
| `BusConfig(request_timeout=-1)` | `ConfigurationError` |
| `bus.slave(999)` / `bus.slave(0)` | `ConfigurationError` |
| `read_holding_registers(0, count=200)` | `ConfigurationError` (raised before sending) |
| `write_register(0, 70_000)` | `ConfigurationError` (raised before sending) |
| CRC mismatch on response (RTU) | `CRCError` (a `ChecksumError`) |
| LRC mismatch on response (ASCII) | `LRCError` (a `ChecksumError`) |
| Truncated or malformed response | `FrameError` |
| Response FC, register count or write echo doesn't match the request | `UnexpectedResponseError` |
| No response within `request_timeout` | `FrameTimeoutError` |
| Slave returned exception code | `ModbusExceptionResponse` subclass |
| USB cable yanked mid-transaction | `ConnectionLostError` |
| Other OS error from the port (e.g. an unmapped `anyserial.SerialError`) | `TransportError` (a `ConnectionLostError` and an `OSError`) |
| Bus closed and another task tries to use it | `BusClosedError` |

Under the default `RetryPolicy`, reads are retried on every `ProtocolError` and on `FrameTimeoutError`; writes are not retried.

An `UnexpectedResponseError` on a **write** means the slave did answer — the write may have been applied. A mismatched reply is most often a late reply to an earlier request; see [late replies](cancellation.md#late-replies).

`ModbusUnsupportedFunctionError` is for the client declining to send a request whose function code it recognises but doesn't implement; no public method sends one today. A *reply* with such a function code is not this error: it is line damage (`CRCError`) or a confused slave (`UnexpectedResponseError`).

## `code_to_exception`

For test fixtures and downstream library code that builds exception responses synthetically:

```python
from anymodbus.exceptions import code_to_exception

raise code_to_exception(function_code=0x03, exception_code=0x02)
# → IllegalDataAddressError(...)
```
