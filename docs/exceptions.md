# Exceptions

`anymodbus` exposes a typed exception hierarchy where every class multi-inherits from the most natural standard-library or AnyIO base. Code that already catches `ValueError`, `TimeoutError`, `anyio.BrokenResourceError`, etc., picks up the right `anymodbus` exceptions without new `except` clauses.

## Hierarchy

```
ModbusError                          (Exception)
├── ConfigurationError               (ValueError)        ← raised at construction time
├── ProtocolError                    (ValueError)        ← raised on the wire
│   ├── CRCError
│   ├── FrameError
│   └── UnexpectedResponseError
├── FrameTimeoutError                (TimeoutError)
├── BusBusyError                     (anyio.BusyResourceError)
├── ConnectionLostError              (anyio.BrokenResourceError)
├── BusClosedError                   (anyio.ClosedResourceError)
└── ModbusExceptionResponse          (slave-returned exception codes 1-11)
    ├── IllegalFunctionError
    ├── IllegalDataAddressError
    ├── IllegalDataValueError
    ├── SlaveDeviceFailureError
    ├── AcknowledgeError
    ├── SlaveDeviceBusyError
    ├── NegativeAcknowledgeError
    ├── MemoryParityError
    ├── GatewayPathUnavailableError
    └── GatewayTargetFailedToRespondError
```

`ConfigurationError` and `ProtocolError` both inherit `ValueError`, but the split is meaningful: `ConfigurationError` fires at construction time (bad `BusConfig` argument, slave address out of range, broadcast call on a unicast-only method), while `ProtocolError` is reserved for byte-level violations seen on the wire. Catching one independently of the other is usually what you want.

## When to expect what

| Situation | Exception |
|---|---|
| `BusConfig(request_timeout=-1)` | `ConfigurationError` |
| `Slave(bus, address=999)` | `ConfigurationError` |
| `slave.read_holding_registers(...)` on a broadcast handle (address 0) | `ConfigurationError` |
| CRC mismatch on response | `CRCError` |
| No response within `request_timeout` | `FrameTimeoutError` |
| Slave returned exception code | `ModbusExceptionResponse` subclass |
| USB cable yanked mid-transaction | `ConnectionLostError` |
| Bus closed and another task tries to use it | `BusClosedError` |
| Two tasks bypass the lock and race | `BusBusyError` (should never normally happen) |
| Response slave-addr or FC echo wrong | `UnexpectedResponseError` |
| Asked for `count=200` registers | `ProtocolError` (raised before sending) |

## `code_to_exception`

For test fixtures and downstream library code that builds exception responses synthetically:

```python
from anymodbus.exceptions import code_to_exception

raise code_to_exception(function_code=0x03, exception_code=0x02)
# → IllegalDataAddressError(...)
```
