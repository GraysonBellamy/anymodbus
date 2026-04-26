# Performance

`anymodbus` is not primarily a performance lever. Modbus RTU is hardware-limited at the wire: a 6-byte FC 3 request + ~10-byte response at 19200 baud takes on the order of 9 ms to traverse the cable regardless of what the host does. The library's job is to stay close to that ceiling while remaining correct.

## What we measure

Three numbers, drawn from `benchmarks/`:

1. **Round-trip latency on a single read.** How close to the wire ceiling does one `slave.read_holding_registers(0, count=1)` come? Compared against `pymodbus` and `minimalmodbus` on the same loopback PTY.
2. **Concurrent fan-out across multiple buses.** N independent buses (different ports), each polling at full bore from its own task — should scale near-linearly until the host runs out of cores.
3. **Single-bus serialization throughput.** N tasks all calling `slave.read_holding_registers` on **one** bus. The bus lock serializes them, so the right number is "FC 3 reads per second at this baud, with overhead vs. the on-wire ceiling characterised."

## How we measure

```
make bench
```

runs `pytest-benchmark` over the suite in `benchmarks/`. CI does **not** run benchmarks — they're noisy on shared runners and the absolute numbers are hardware-dependent. The repo includes:

- `benchmarks/bench_roundtrip.py` — single-call latency vs. peers, against a loopback PTY pair.
- `benchmarks/bench_concurrent.py` — multi-bus concurrent fan-out scaling.

## Numbers

Will be published here once the v0.2 hardening pass lands the benchmarks alongside CI artefacts. The honest comparison against `pymodbus` and `minimalmodbus` is the headline output — `anymodbus`'s pitch is correctness and AnyIO-native concurrency, not a 2× perf claim, and the docs should reflect that.

## Things that **are** performance levers

- **Don't open one bus per request.** Open the bus once, call repeatedly. Opener cost is tens of milliseconds of kernel + termios; per-call cost is microseconds plus the wire.
- **Keep the bus lock fair.** A single bus is intrinsically serialised — at 19200 baud you can do roughly 100 round-trips per second per bus. If you need more, split slaves across more physical ports and run buses concurrently.
- **Tune `request_timeout` to your slowest device.** Too generous and a hung slave wastes seconds before retry; too tight and a slow-but-healthy slave times out spuriously. The default 3.0 s suits most "responds within 200 ms" devices comfortably.
- **Set `inter_frame_idle` explicitly only if you've measured.** The auto-computed default is spec-correct; manually setting it shorter is a foot-gun (you risk frame fusion on the slave side); manually setting it longer wastes bus capacity.

## Things that are **not** performance levers

- **The framer.** The length-aware path is dominated by `await stream.receive`, not by Python overhead. Microoptimising the state machine hasn't shown a measurable effect.
- **Backend choice.** asyncio, asyncio+uvloop, and trio all benchmark within the noise floor for Modbus's transaction shape (one tx, one rx, lock held throughout).
