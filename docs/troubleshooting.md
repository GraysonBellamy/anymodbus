# Troubleshooting

When `slave.read_holding_registers(...)` raises, walk this list before opening an issue.

## `FrameTimeoutError` — no response

1. Check baud, parity, and stop bits match the slave. The Modbus RTU spec specifies 8E1 (even parity), but real devices commonly ship 8N1 or 8O1. Mismatched parity will silently drop every frame.
2. Verify the slave address. Most devices ship with address 1, but always confirm.
3. RS-485 only: confirm direction control. Either:
   - The kernel handles RTS-toggle (Linux `TIOCSRS485`, see `anyserial`'s `RS485Config`), or
   - You're toggling RTS manually with `set_control_lines()` + `drain_exact()`.
4. RS-485 only: bus termination resistors at both ends? Bias resistors?
5. Try a longer `BusConfig.request_timeout`. Some slaves are slow to respond after a write.

## `CRCError` — bytes are arriving but the CRC doesn't verify

1. Wrong baud rate is the #1 cause — bytes are being misframed.
2. Electrical noise on long RS-485 runs. Check shielding and grounding.
3. A second master on the bus is interleaving its traffic with yours. Modbus RTU is single-master; verify nothing else is talking.

## `IllegalFunctionError` / `IllegalDataAddressError`

The slave received the frame fine but rejected the request semantically.

- `IllegalFunctionError` — the slave doesn't implement that function code. Check its protocol manual.
- `IllegalDataAddressError` — the address (or address+count) is outside the slave's register map.

## `UnexpectedResponseError`

The reply has a checksum that verifies but doesn't answer the request: a different function code, a different number of registers or coils than you asked for, or a write echo whose address, value or quantity differs from what you sent. On a write, the slave did answer, so the write may have been applied. Usually means one of:

- **A late reply to an earlier request.** A request that timed out or was cancelled can still be answered; the reply then lands during the next transaction. Set `TimingConfig.late_reply_window` (see [late replies](cancellation.md#late-replies)) — and consider a longer `request_timeout` if your device is simply slow.

- **Hardware echo on a USB-RS485 adapter.** Some cheap adapters loop your transmitted bytes back into the receive line, so `anymodbus` reads its own request and tries to parse it as a response. Symptoms: the "wrong" address/FC is exactly what you just sent. Fix at the `anyserial` layer — see `anyserial`'s `RS485Config` (`rts_on_send` / kernel `TIOCSRS485`) or, for adapters that ignore RTS, the manual `set_control_lines` + `drain_exact` pattern. The protocol layer can't recover from this; it must be solved one layer down.
- Another master is on the bus.
- The slave is misbehaving (firmware bug).
- A previous transaction left junk in the rx buffer (`reset_input_buffer_before_request=True` in `BusConfig` is the default — don't disable it without reason).

## `TransportError` / `ConnectionLostError`

The port itself failed: the device was unplugged, the driver returned an error, or `drain` / `reset_input_buffer` failed. `ConnectionLostError` is a disconnect; `TransportError` (a subclass, and also an `OSError`) is any other OS-level error, with the original exception as `__cause__`. Neither is retried. Treat the port as gone: close the bus and open a new one.

## `ConfigurationError`

Raised before anything is sent, never because of what came back. Common triggers:

- `BusConfig(request_timeout=...)` with a value <= 0 or > 60 seconds.
- `bus.slave(address)` with an address outside 1-247 — including 0, the broadcast address. Use `Bus.broadcast_*` for broadcasts.
- A register count above the spec maximum (125 for FC 03/04) or a value that doesn't fit in a 16-bit register.

## Floats look wrong

Word order. Try `word_order=WordOrder.LOW_HIGH` if the default `HIGH_LOW` gives garbage values, or vice versa. The Modbus spec doesn't standardize multi-register word order — check the device's manual. See [Decoders & word order](decoders.md).

*This page will be expanded with concrete repro recipes once v0.1 lands.*
