#!/usr/bin/env python3
"""Sweep Pluto RX settings for transport diagnostics."""

from __future__ import annotations

import argparse
import gc
import time


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pluto_rx_sweep",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--uris",
        nargs="+",
        default=["usb:1.5.5", "ip:192.168.2.1"],
        help="Pluto IIO URIs to test",
    )
    p.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[256, 4096, 16384, 65536, 131072, 262144, 524288],
        help="RX buffer sizes to test",
    )
    p.add_argument(
        "--sample-rates",
        nargs="+",
        type=float,
        default=[1e6, 4e6],
        help="Sample rates to test",
    )
    p.add_argument(
        "--channel-sets",
        nargs="+",
        default=["0", "01"],
        help="RX channel sets to test: '0', '1', '01', or comma form like '0,1'",
    )
    p.add_argument("--rx-lo", type=float, default=2.1e9)
    p.add_argument("--gain", type=float, default=60.0)
    p.add_argument("--timeout-ms", type=int, default=5000)
    p.add_argument("--settle-s", type=float, default=0.5)
    return p


def parse_channel_set(value: str) -> list[int]:
    if "," in value:
        chans = [int(part.strip()) for part in value.split(",") if part.strip()]
    else:
        chans = [int(ch) for ch in value.strip()]
    if not chans or any(ch not in (0, 1) for ch in chans):
        raise ValueError(f"Invalid channel set '{value}'. Use 0, 1, 01, or 0,1.")
    return chans


def main() -> int:
    import adi

    args = build_parser().parse_args()
    print(f"adi={adi.__version__}", flush=True)
    channel_sets = [(raw, parse_channel_set(raw)) for raw in args.channel_sets]

    for uri in args.uris:
        print(f"\n=== {uri} ===", flush=True)
        for sample_rate in args.sample_rates:
            for label, channels in channel_sets:
                for size in args.sizes:
                    sdr = None
                    try:
                        sdr = adi.ad9361(uri=uri)
                        sdr._ctx.set_timeout(args.timeout_ms)
                        sdr.sample_rate = int(sample_rate)
                        sdr.rx_lo = int(args.rx_lo)
                        sdr.rx_enabled_channels = channels
                        if 0 in channels:
                            sdr.gain_control_mode_chan0 = "manual"
                            sdr.rx_hardwaregain_chan0 = int(args.gain)
                        if 1 in channels:
                            sdr.gain_control_mode_chan1 = "manual"
                            sdr.rx_hardwaregain_chan1 = int(args.gain)
                        sdr.rx_buffer_size = int(size)

                        print(
                            f"before rx uri={uri} fs={int(sample_rate)} "
                            f"ch={label} n={size}",
                            flush=True,
                        )
                        data = sdr.rx()
                        if isinstance(data, list):
                            lengths = ",".join(str(len(ch_data)) for ch_data in data)
                        else:
                            lengths = str(len(data))
                        print(
                            f"PASS fs={int(sample_rate)} ch={label} "
                            f"n={size} lens={lengths}",
                            flush=True,
                        )
                    except Exception as exc:  # noqa: BLE001 - diagnostic script
                        print(
                            f"FAIL fs={int(sample_rate)} ch={label} n={size}: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                    finally:
                        if sdr is not None:
                            try:
                                sdr.rx_destroy_buffer()
                            except Exception:
                                pass
                            del sdr
                        gc.collect()
                        time.sleep(args.settle_s)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
