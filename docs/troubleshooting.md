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

The slave echoed a different address or function code than we requested. Usually means one of:

- **Hardware echo on a USB-RS485 adapter.** Some cheap adapters loop your transmitted bytes back into the receive line, so `anymodbus` reads its own request and tries to parse it as a response. Symptoms: the "wrong" address/FC is exactly what you just sent. Fix at the `anyserial` layer — see `anyserial`'s `RS485Config` (`rts_on_send` / kernel `TIOCSRS485`) or, for adapters that ignore RTS, the manual `set_control_lines` + `drain_exact` pattern. The protocol layer can't recover from this; it must be solved one layer down.
- Another master is on the bus.
- The slave is misbehaving (firmware bug).
- A previous transaction left junk in the rx buffer (`reset_input_buffer_before_request=True` in `BusConfig` is the default — don't disable it without reason).

## `ConfigurationError`

Raised at construction time, never on the wire. Common triggers:

- `BusConfig(request_timeout=...)` with a value <= 0 or > 60 seconds.
- `Slave(bus, address=...)` with an address outside [0, 255].
- Calling a unicast method (`read_holding_registers`, `write_register`, …) on a broadcast handle (`address=0`). Use `Bus.broadcast_*` for broadcasts.

## Floats look wrong

Word order. Try `word_order=WordOrder.LOW_HIGH` if the default `HIGH_LOW` gives garbage values, or vice versa. The Modbus spec doesn't standardize multi-register word order — check the device's manual. See [Decoders & word order](decoders.md).

*This page will be expanded with concrete repro recipes once v0.1 lands.*
