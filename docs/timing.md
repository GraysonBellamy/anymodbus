# Timing

`anymodbus` enforces the inter-frame and inter-character timings that the *Modbus over Serial Line v1.02* spec requires of an RTU master. Defaults are spec-derived; everything is configurable via [`TimingConfig`](https://github.com/GraysonBellamy/anymodbus/blob/main/src/anymodbus/config.py).

## The two gaps

| Gap | Symbol | Spec value | Where it's used | Config field |
|-----|--------|-----------|-----------------|--------------|
| Inter-frame idle | t3.5 | ≥ 3.5 char-times of silence between frames | Held **before every tx** to satisfy *serial §2.5.1.1* | `inter_frame_idle` |
| Inter-character idle | t1.5 | ≤ 1.5 char-times within a frame | Used by the unknown-FC gap-based reader and the unexpected-slave drain branch in [framing.md](framing.md) | `inter_char_timeout` |

Both default to the sentinel `"auto"`, which computes from the stream's current baud:

```
inter_frame_idle  = max(3.5 * 11 / baudrate, 0.00175)   # 1.75 ms floor
inter_char_timeout = max(1.5 * 11 / baudrate, 0.00075)  # 0.75 ms floor
```

Per-baud floors of 1.75 ms / 0.75 ms come from *serial §2.5.1.1*: at baud > 19200 the per-character interrupt load otherwise becomes prohibitive, so the spec recommends fixed values.

## Why 11 bits per character?

The `11` in both formulas is the on-wire bit count: 1 start + 8 data + (1 parity OR 1 extra stop) + 1 stop = 11 bits. This is correct for the spec-compliant RTU character formats: 8E1, 8O1, 8N2.

8N1 is non-compliant per spec but exists in the wild. Using 11 bits unconditionally makes the gap ~10% longer than the wire actually requires under 8N1 — harmless, and it errs on the safe (longer) side.

## Per-baud table

Reference values for the spec-recommended baudrates:

| Baud | t3.5 (3.5 × 11/baud) | Effective t3.5 (with floor) | t1.5 |
|------|---------------------|----------------------------|------|
| 1200 | 32.08 ms | 32.08 ms | 13.75 ms |
| 2400 | 16.04 ms | 16.04 ms | 6.88 ms |
| 4800 | 8.02 ms | 8.02 ms | 3.44 ms |
| 9600 | 4.01 ms | 4.01 ms | 1.72 ms |
| 19200 | 2.01 ms | 2.01 ms | 0.86 ms |
| 38400 | 1.00 ms | 1.75 ms (floor) | 0.75 ms (floor) |
| 57600 | 0.67 ms | 1.75 ms (floor) | 0.75 ms (floor) |
| 115200 | 0.33 ms | 1.75 ms (floor) | 0.75 ms (floor) |

## Tx-side enforcement

The bus records `_last_io_monotonic` after every send and every receive. Before the next tx:

```
elapsed = anyio.current_time() - last_io_monotonic
if elapsed < inter_frame_idle:
    await anyio.sleep(inter_frame_idle - elapsed)
```

This is why `anymodbus` is "tx-side correct" — the master never violates t3.5, even if the slave's reply was late or the host scheduler was busy. `pymodbus` relies on the bus lock alone for this and does not enforce a pre-tx sleep.

## Broadcast turnaround

Per *serial §2.4.1*, after a broadcast (slave address 0) the master must hold the bus idle long enough for every slave to finish processing the write. This is **separate** from t3.5:

- t3.5 is wire-level framing (microseconds at 19200).
- The turnaround delay is "let slaves catch up" (typically 100–200 ms).

Configured via `TimingConfig.broadcast_turnaround` (default 0.1 s = 100 ms). The `Bus.broadcast_*` methods hold the bus lock for this long after sending; the next unicast can't preempt slave processing.

## Post-tx settling

`TimingConfig.post_tx_settle` (default 0) inserts a fixed wait between `stream.send` returning and the start of the rx loop. Most setups don't need it; some RS-485 transceivers benefit from a small (~ 0.5 ms) settling delay between de-asserting RTS and starting to listen.

## When `"auto"` falls back

If the stream isn't a serial port (e.g. `client_slave_pair` for tests, or the future Modbus TCP transport), the AnyIO typed-attribute lookup for the baudrate returns the fallback constant — the equivalent of 19200 baud. Override explicitly when you know better:

```python
from anymodbus import BusConfig, TimingConfig

cfg = BusConfig(timing=TimingConfig(inter_frame_idle=0.001, inter_char_timeout=0.0005))
```

## RS-485 considerations

When the kernel handles RTS-toggle (Linux RS-485 ioctl, modern USB-RS485 chips), the timing is hardware-tight and `anymodbus`'s defaults are correct. When userspace toggles RTS manually, the t3.5 gap also has to cover the bus turnaround. See [troubleshooting.md](troubleshooting.md) for the diagnosis flow if your reads time out on bus-idle hardware.
