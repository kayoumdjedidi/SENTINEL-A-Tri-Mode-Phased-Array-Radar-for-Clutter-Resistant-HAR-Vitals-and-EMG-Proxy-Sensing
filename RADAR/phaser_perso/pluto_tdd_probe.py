r"""Minimal Pluto TDD capture probe.

This isolates the Pluto TDD engine from the full Phaser radar pipeline.

Use it from native Windows PowerShell in the radar .venv-win:

    python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers soft internal
    python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers external --rpi-uri ip:phaser.local

Interpretation:
    soft PASS      -> Pluto TDD + DMA can work; use --tdd-trigger soft in radar_mode_a.
    soft FAIL      -> Pluto TDD/DMA path itself is not filling buffers.
    external FAIL  -> Phaser gpio_burst / external sync line is not working.
"""
from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from typing import Any

import numpy as np


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pluto_tdd_probe",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sdr-uri", default="ip:192.168.2.1")
    p.add_argument("--tdd-uri", default=None, help="Defaults to --sdr-uri")
    p.add_argument("--rpi-uri", default="ip:phaser.local")
    p.add_argument(
        "--triggers",
        nargs="+",
        choices=["soft", "internal", "external"],
        default=["soft", "internal"],
        help="TDD trigger modes to test",
    )
    p.add_argument("--sample-rate", type=float, default=4e6)
    p.add_argument("--rx-lo", type=float, default=2.1e9)
    p.add_argument("--tx-lo", type=float, default=2.1e9)
    p.add_argument("--tone-hz", type=float, default=100e3)
    p.add_argument("--rx-gain", type=int, default=60)
    p.add_argument("--tx-gain", type=int, default=0)
    p.add_argument("--frame-ms", type=float, default=0.5)
    p.add_argument("--burst-count", type=int, default=8)
    p.add_argument("--arm-delay-s", type=float, default=1.0)
    p.add_argument("--rx-timeout-ms", type=int, default=30000)
    p.add_argument("--internal-period-ms", type=float, default=1000.0)
    p.add_argument("--external-pulse-high-s", type=float, default=0.05)
    p.add_argument("--external-pulse-gap-s", type=float, default=0.05)
    p.add_argument("--external-pulse-count", type=int, default=1)
    p.add_argument(
        "--external-pulse-active",
        choices=["high", "low"],
        default="high",
        help="Active polarity for Phaser gpio_burst pulse",
    )
    p.add_argument(
        "--buffer-size",
        type=int,
        default=0,
        help="0 means auto: next power of two covering frame_ms * burst_count",
    )
    p.add_argument("--dump", action="store_true", help="Print TDD attrs/channels")
    p.add_argument("--dump-gpio", action="store_true", help="Print one-bit GPIO labels/readback")
    p.add_argument("--no-tx-tone", action="store_true")
    return p


def next_power_of_two(n: int) -> int:
    if n <= 1:
        return 1
    return 1 << math.ceil(math.log2(n))


def safe_get(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except Exception as exc:  # noqa: BLE001
        return f"<error: {exc}>"


def safe_set(obj: Any, name: str, value: Any) -> bool:
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def dump_gpio_context(adi: Any, uri: str, label: str) -> None:
    print(f"[gpio] {label} uri={uri}")
    try:
        pins = adi.one_bit_adc_dac(uri)
    except Exception as exc:  # noqa: BLE001
        print(f"  open failed: {type(exc).__name__}: {exc}")
        return
    ctrl = getattr(pins, "_ctrl", None)
    if ctrl is None:
        print("  no _ctrl")
        return
    for chan in ctrl.channels:
        chan_label = chan.attrs["label"].value
        prop = f"gpio_{chan_label.lower()}"
        print(
            f"  id={chan.id} output={chan.output} label={chan_label} "
            f"{prop}={safe_get(pins, prop)}"
        )


def one_bit_labels(pins: Any) -> set[str]:
    ctrl = getattr(pins, "_ctrl", None)
    if ctrl is None:
        return set()
    labels: set[str] = set()
    for ch in ctrl.channels:
        try:
            labels.add(ch.attrs["label"].value.lower())
        except Exception:
            pass
    return labels


def abandon_rx_buffer(sdr: Any) -> None:
    rxbuf = getattr(sdr, "_rxbuf", None)
    if rxbuf is not None and hasattr(rxbuf, "_buffer"):
        try:
            rxbuf._buffer = None
        except Exception:
            pass
    try:
        sdr._rxbuf = None
    except Exception:
        pass


def configure_sdr(adi: Any, args: argparse.Namespace, buffer_size: int) -> Any:
    sdr = adi.ad9361(uri=args.sdr_uri)
    sdr._ctx.set_timeout(int(args.rx_timeout_ms))
    sdr.sample_rate = int(args.sample_rate)
    sdr.rx_lo = int(args.rx_lo)
    sdr.rx_enabled_channels = [0, 1]
    sdr.gain_control_mode_chan0 = "manual"
    sdr.gain_control_mode_chan1 = "manual"
    sdr.rx_hardwaregain_chan0 = int(args.rx_gain)
    sdr.rx_hardwaregain_chan1 = int(args.rx_gain)
    sdr.rx_buffer_size = int(buffer_size)

    sdr.tx_lo = int(args.tx_lo)
    sdr.tx_enabled_channels = [0, 1]
    sdr.tx_cyclic_buffer = True
    sdr.tx_hardwaregain_chan0 = -88
    sdr.tx_hardwaregain_chan1 = int(args.tx_gain)
    if not args.no_tx_tone:
        n = 2**16
        t = np.arange(n, dtype=np.float32) / float(args.sample_rate)
        iq = 0.9 * (np.cos(2 * np.pi * args.tone_hz * t) + 1j * np.sin(2 * np.pi * args.tone_hz * t))
        iq = (iq * 2**14).astype(np.complex64)
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass
        sdr.tx([iq, iq])
    return sdr


def configure_tdd(adi: Any, args: argparse.Namespace, trigger: str) -> tuple[Any, Any]:
    tdd_uri = args.tdd_uri or args.sdr_uri
    pins = adi.one_bit_adc_dac(tdd_uri)
    labels = one_bit_labels(pins)
    if trigger == "external" and "tdd_ext_sync" not in labels:
        raise RuntimeError(
            "External TDD requested, but Pluto one-bit-adc-dac does not expose "
            f"'tdd_ext_sync'. Exposed labels: {', '.join(sorted(labels))}. "
            "Use soft trigger unless the Pluto/CN0566 HDL/device tree is changed."
        )
    if "tdd_ext_sync" in labels:
        pins.gpio_tdd_ext_sync = trigger == "external"
    if hasattr(pins, "gpio_phaser_enable"):
        pins.gpio_phaser_enable = True

    tdd = adi.tddn(tdd_uri)
    tdd.enable = False
    tdd.sync_external = trigger == "external"
    safe_set(tdd, "sync_internal", trigger == "internal")
    safe_set(tdd, "internal_sync_period_ms", float(args.internal_period_ms))
    safe_set(tdd, "sync_reset", True)
    tdd.startup_delay_ms = 0
    tdd.frame_length_ms = float(args.frame_ms)
    tdd.burst_count = int(args.burst_count)
    for idx in range(min(3, len(tdd.channel))):
        tdd.channel[idx].enable = True
        tdd.channel[idx].polarity = False
        tdd.channel[idx].on_raw = 0
        tdd.channel[idx].off_raw = 10
    return pins, tdd


def dump_tdd(tdd: Any) -> None:
    attrs = [
        "enable",
        "state",
        "frame_length_ms",
        "frame_length_raw",
        "startup_delay_ms",
        "burst_count",
        "sync_external",
        "sync_internal",
        "sync_soft",
        "sync_reset",
        "internal_sync_period_ms",
    ]
    print("[dump] TDD attrs:")
    for name in attrs:
        print(f"  {name}={safe_get(tdd, name)}")
    for idx, ch in enumerate(tdd.channel):
        print(
            f"  ch{idx}: enable={safe_get(ch, 'enable')} "
            f"polarity={safe_get(ch, 'polarity')} "
            f"on_raw={safe_get(ch, 'on_raw')} off_raw={safe_get(ch, 'off_raw')} "
            f"on_ms={safe_get(ch, 'on_ms')} off_ms={safe_get(ch, 'off_ms')}"
        )


def pulse_external(adi: Any, args: argparse.Namespace) -> None:
    if args.external_pulse_active == "high":
        idle, active = 0, 1
    else:
        idle, active = 1, 0

    phaser_pins = adi.one_bit_adc_dac(args.rpi_uri)
    for idx in range(max(1, int(args.external_pulse_count))):
        phaser_pins.gpio_burst = idle
        time.sleep(max(0.0, args.external_pulse_gap_s))
        print(
            f"external pulse {idx + 1}: idle={idle} active={active} "
            f"high_s={args.external_pulse_high_s} readback_before={safe_get(phaser_pins, 'gpio_burst')}",
            flush=True,
        )
        phaser_pins.gpio_burst = active
        time.sleep(max(0.0, args.external_pulse_high_s))
        print(f"external pulse {idx + 1}: active_readback={safe_get(phaser_pins, 'gpio_burst')}", flush=True)
        phaser_pins.gpio_burst = idle
        print(f"external pulse {idx + 1}: final_readback={safe_get(phaser_pins, 'gpio_burst')}", flush=True)
        if idx < max(1, int(args.external_pulse_count)) - 1:
            time.sleep(max(0.0, args.external_pulse_gap_s))


def run_one(adi: Any, args: argparse.Namespace, trigger: str, buffer_size: int) -> bool:
    print(f"\n=== trigger={trigger} buffer={buffer_size} ===", flush=True)
    sdr = None
    tdd = None
    try:
        sdr = configure_sdr(adi, args, buffer_size)
        _, tdd = configure_tdd(adi, args, trigger)
        if args.dump:
            dump_tdd(tdd)

        result: dict[str, Any] = {}
        errors: list[BaseException] = []

        def _rx() -> None:
            try:
                result["data"] = sdr.rx()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=_rx, daemon=True)
        t.start()
        time.sleep(max(0.0, args.arm_delay_s))

        print(f"state before trigger: {safe_get(tdd, 'state')}", flush=True)
        tdd.enable = True
        if trigger == "soft":
            time.sleep(0.01)
            tdd.sync_soft = True
        elif trigger == "external":
            pulse_external(adi, args)
        elif trigger != "internal":
            raise ValueError(trigger)

        join_s = max(10.0, args.rx_timeout_ms / 1000.0 + 5.0)
        t.join(timeout=join_s)
        print(f"state after trigger: {safe_get(tdd, 'state')}", flush=True)

        if errors:
            raise errors[0]
        if "data" not in result:
            raise TimeoutError(f"rx thread did not return after {join_s:.1f} s")

        data = result["data"]
        arrays = data if isinstance(data, (list, tuple)) else [data]
        lens = ",".join(str(len(np.asarray(x))) for x in arrays)
        levels = ",".join(f"{float(np.mean(np.abs(np.asarray(x)))):.3g}" for x in arrays)
        print(f"PASS trigger={trigger} lens={lens} mean_abs={levels}", flush=True)
        return True
    except BaseException as exc:  # noqa: BLE001
        if sdr is not None:
            abandon_rx_buffer(sdr)
        print(f"FAIL trigger={trigger}: {type(exc).__name__}: {exc}", flush=True)
        return False
    finally:
        if tdd is not None:
            try:
                tdd.enable = False
            except Exception:
                pass
        if sdr is not None:
            try:
                sdr.tx_destroy_buffer()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    import adi

    print(f"adi={getattr(adi, '__version__', '<unknown>')}")
    if args.dump_gpio:
        dump_gpio_context(adi, args.tdd_uri or args.sdr_uri, "pluto/tdd")
        dump_gpio_context(adi, args.rpi_uri, "phaser/rpi")
    auto_samples = int(math.ceil(args.sample_rate * (args.frame_ms / 1000.0) * args.burst_count))
    buffer_size = args.buffer_size or next_power_of_two(auto_samples)
    print(
        f"sample_rate={args.sample_rate:.0f} frame_ms={args.frame_ms} "
        f"burst_count={args.burst_count} auto_samples={auto_samples} "
        f"buffer_size={buffer_size}"
    )

    passed = 0
    for trigger in args.triggers:
        if run_one(adi, args, trigger, buffer_size):
            passed += 1
    return 0 if passed == len(args.triggers) else 1


if __name__ == "__main__":
    sys.exit(main())
