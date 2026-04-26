#!/usr/bin/env python3
"""Standalone HB100 frequency calibration for CN0566 Phaser.

This intentionally stays separate from SENTINEL closed-loop acquisition.
The output file is compatible with ADI's phaser_functions.load_hb100_cal()
and with sentinel_closed_loop.py:

    hb100_freq_val.pkl

Recommended reference cable topology:
  1. CN0566/Phaser stays attached to the Raspberry Pi.
  2. Pluto USB DATA is plugged into the Raspberry Pi, not the Windows PC.
  3. Windows PC is on the same network and can reach phaser.local.
  4. HB100 is powered and aimed at the array boresight.

Fallback direct topology:
  1. Pluto stays plugged into the Windows PC / direct IP path.
  2. CN0566/Phaser stays reachable at ip:phaser.local.
  3. Use --topology direct.
"""
from __future__ import annotations

import argparse
import gc
import json
import pickle
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


@dataclass
class HBCalResult:
    topology: str
    rpi_uri: str
    sdr_uri: str
    sample_rate: float
    rx_lo: float
    sweep_start: float
    sweep_stop: float
    sweep_step: float
    peak_freq_hz: float
    peak_power_db: float
    save_file: str


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hb100_calibrate",
        description="Standalone HB100 frequency calibration for CN0566/Pluto.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--topology",
        choices=["reference", "direct", "auto"],
        default="reference",
        help="reference = Pluto USB plugged into RPi, direct = Pluto via normal PC/direct URI.",
    )
    p.add_argument("--rpi-uri", default="ip:phaser.local")
    p.add_argument("--reference-sdr-uri", default="ip:phaser.local:50901")
    p.add_argument("--direct-sdr-uri", default="ip:192.168.2.1")
    p.add_argument("--sample-rate", type=float, default=30e6)
    p.add_argument("--rx-buffer-size", type=int, default=4 * 256)
    p.add_argument("--rx-rf-bandwidth", type=float, default=10e6)
    p.add_argument("--rx-lo", type=float, default=2.0e9)
    p.add_argument("--rx-gain", type=int, default=0)
    p.add_argument("--tx-atten", type=int, default=-80)
    p.add_argument("--array-gain", type=int, default=64)
    p.add_argument("--sweep-start", type=float, default=10.0e9)
    p.add_argument("--sweep-stop", type=float, default=10.7e9)
    p.add_argument("--sweep-step", type=float, default=10e6)
    p.add_argument("--settle-s", type=float, default=0.10)
    p.add_argument("--timeout-ms", type=int, default=30000)
    p.add_argument("--save-file", type=Path, default=Path("hb100_freq_val.pkl"))
    p.add_argument("--save-json", type=Path, default=None)
    p.add_argument("--plot", action="store_true")
    p.add_argument("--no-prompt", action="store_true")
    p.add_argument("--print-contexts", action="store_true")
    return p


def print_cable_instructions(args: argparse.Namespace) -> None:
    print()
    print("HB100 calibration cable checklist")
    print("--------------------------------")
    if args.topology in {"reference", "auto"}:
        print("Reference topology:")
        print("  - Plug Pluto USB DATA into the Raspberry Pi attached to the Phaser.")
        print("  - Unplug Pluto USB DATA from the Windows PC for this calibration.")
        print("  - Keep CN0566/Phaser attached to the Raspberry Pi.")
        print("  - Keep Windows PC on the same network as phaser.local.")
        print(f"  - Expected SDR URI: {args.reference_sdr_uri}")
    if args.topology in {"direct", "auto"}:
        print("Direct fallback topology:")
        print("  - Keep Pluto connected to Windows/direct IP.")
        print("  - Keep CN0566/Phaser reachable at ip:phaser.local.")
        print(f"  - Expected SDR URI: {args.direct_sdr_uri}")
    print("HB100 source:")
    print("  - Power the HB100 module from its proper supply.")
    print("  - Aim it straight at the Phaser array boresight, about 0.5-2 m away.")
    print("  - Keep people/moving objects away during the sweep.")
    print("  - Stop all other SENTINEL/radar Python processes before running this.")
    print()


def print_contexts() -> None:
    try:
        import iio

        contexts = iio.scan_contexts()
        print("[iio] available contexts:")
        if not contexts:
            print("  none discovered")
        for uri, desc in contexts.items():
            print(f"  {uri}: {desc}")
    except Exception as exc:
        print(f"[iio] scan_contexts failed: {type(exc).__name__}: {exc}")


def topology_candidates(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []

    def add(name: str, rpi_uri: str, sdr_uri: str) -> None:
        item = (name, rpi_uri, sdr_uri)
        if item not in candidates:
            candidates.append(item)

    if args.topology in {"reference", "auto"}:
        add("reference-local", "ip:localhost", args.reference_sdr_uri)
        add("reference-rpi", args.rpi_uri, args.reference_sdr_uri)
    if args.topology in {"direct", "auto"}:
        add("direct", args.rpi_uri, args.direct_sdr_uri)
    return candidates


def open_devices(name: str, rpi_uri: str, sdr_uri: str, args: argparse.Namespace):
    from adi import ad9361
    from adi.cn0566 import CN0566

    print(f"[open] {name}: CN0566={rpi_uri}  Pluto={sdr_uri}")
    if name.startswith("direct"):
        sdr = ad9361(uri=sdr_uri)
        sdr._ctx.set_timeout(int(args.timeout_ms))
        phaser = CN0566(uri=rpi_uri, sdr=sdr)
    else:
        phaser = CN0566(uri=rpi_uri)
        sdr = ad9361(uri=sdr_uri)
        sdr._ctx.set_timeout(int(args.timeout_ms))
        phaser.sdr = sdr
    return phaser, sdr


def configure_hardware(phaser, sdr, args: argparse.Namespace) -> None:
    time.sleep(0.5)
    phaser.configure(device_mode="rx")
    try:
        phaser.element_spacing = 0.014
    except Exception:
        pass

    for name, value in (
        ("adi,frequency-division-duplex-mode-enable", "1"),
        ("adi,ensm-enable-txnrx-control-enable", "0"),
        ("initialize", "1"),
    ):
        try:
            sdr._ctrl.debug_attrs[name].value = value
        except Exception:
            pass

    sdr.rx_enabled_channels = [0, 1]
    try:
        sdr._rxadc.set_kernel_buffers_count(1)
    except Exception:
        pass
    try:
        sdr._ctrl.find_channel("voltage0").attrs["quadrature_tracking_en"].value = "1"
    except Exception:
        pass

    sdr.sample_rate = int(args.sample_rate)
    sdr.rx_buffer_size = int(args.rx_buffer_size)
    try:
        sdr.rx_rf_bandwidth = int(args.rx_rf_bandwidth)
    except Exception:
        pass
    sdr.gain_control_mode_chan0 = "manual"
    sdr.gain_control_mode_chan1 = "manual"
    sdr.rx_hardwaregain_chan0 = int(args.rx_gain)
    sdr.rx_hardwaregain_chan1 = int(args.rx_gain)
    sdr.rx_lo = int(args.rx_lo)

    try:
        sdr.tx_enabled_channels = [0, 1]
        sdr.tx_cyclic_buffer = False
        sdr.tx_hardwaregain_chan0 = int(args.tx_atten)
        sdr.tx_hardwaregain_chan1 = int(args.tx_atten)
    except Exception:
        pass
    try:
        sdr.filter = "LTE20_MHz.ftr"
    except Exception:
        pass

    phaser.SignalFreq = 10.525e9
    phaser.lo = int(phaser.SignalFreq) + int(sdr.rx_lo)
    for i in range(8):
        phaser.set_chan_gain(i, int(args.array_gain), apply_cal=False)
    phaser.set_beam_phase_diff(0.0)
    try:
        phaser.Averages = 8
    except Exception:
        pass


def spectrum_db(x: np.ndarray, fs: float, ref: float = 2**12) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=np.complex64).reshape(-1)
    n = x.size
    win = np.blackman(n).astype(np.float32)
    coherent = float(np.sum(win)) or 1.0
    sp = np.fft.fftshift(np.fft.fft(x * win))
    amp = np.abs(sp) / coherent
    amp_db = 20.0 * np.log10(np.maximum(amp / ref, 1e-20))
    freq = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / float(fs)))
    return amp_db.astype(np.float32), freq.astype(np.float64)


def run_sweep(phaser, sdr, args: argparse.Namespace) -> tuple[float, float, np.ndarray, np.ndarray]:
    full_freqs: list[np.ndarray] = []
    full_power: list[np.ndarray] = []

    sweep = np.arange(float(args.sweep_start), float(args.sweep_stop), float(args.sweep_step))
    if sweep.size == 0:
        raise ValueError("empty sweep; check --sweep-start/--sweep-stop/--sweep-step")

    for idx, rf in enumerate(sweep, start=1):
        print(f"[sweep] {idx:03d}/{sweep.size:03d}  RF={rf/1e9:.6f} GHz")
        phaser.SignalFreq = float(rf)
        phaser.frequency = int((float(phaser.SignalFreq) + float(sdr.rx_lo)) // 4)
        time.sleep(float(args.settle_s))

        data = sdr.rx()
        if isinstance(data, (list, tuple)):
            iq = np.asarray(data[0], dtype=np.complex64) + np.asarray(data[1], dtype=np.complex64)
        elif hasattr(data, "ndim") and data.ndim > 1:
            iq = data[0].astype(np.complex64) + data[1].astype(np.complex64)
        else:
            iq = np.asarray(data, dtype=np.complex64).reshape(-1)

        power_db, bb_freq = spectrum_db(iq, float(args.sample_rate))
        full_freqs.append(bb_freq + float(rf))
        full_power.append(power_db)

    freqs = np.concatenate(full_freqs)
    power = np.concatenate(full_power)
    peak_idx = int(np.argmax(power))
    return float(freqs[peak_idx]), float(power[peak_idx]), freqs, power


def save_result(result: HBCalResult, freqs: np.ndarray, power: np.ndarray, args: argparse.Namespace) -> None:
    args.save_file.parent.mkdir(parents=True, exist_ok=True)
    with args.save_file.open("wb") as f:
        pickle.dump(float(result.peak_freq_hz), f)

    json_path = args.save_json
    if json_path is None:
        json_path = args.save_file.with_suffix(".json")
    json_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")

    npz_path = args.save_file.with_suffix(".npz")
    np.savez_compressed(npz_path, freqs_hz=freqs.astype(np.float64), power_db=power.astype(np.float32))
    print(f"[save] pickle frequency: {args.save_file}")
    print(f"[save] metadata: {json_path}")
    print(f"[save] spectrum: {npz_path}")

    if args.plot:
        import matplotlib.pyplot as plt

        plt.figure("HB100 calibration")
        plt.plot(freqs / 1e9, power, ".", markersize=2)
        plt.axvline(result.peak_freq_hz / 1e9, color="r", linestyle="--")
        plt.title(f"HB100 peak {result.peak_freq_hz/1e9:.6f} GHz")
        plt.xlabel("Frequency [GHz]")
        plt.ylabel("Amplitude [dBFS-ish]")
        plt.grid(True)
        plt.show()


def cleanup(phaser, sdr) -> None:
    if sdr is not None:
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass
        try:
            sdr.rx_destroy_buffer()
        except Exception:
            pass
    del phaser
    del sdr
    gc.collect()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    print_cable_instructions(args)
    if args.print_contexts:
        print_contexts()
    if not args.no_prompt:
        input("Press ENTER when the cable checklist is correct and HB100 is aimed at boresight...")

    last_exc: Exception | None = None
    for name, rpi_uri, sdr_uri in topology_candidates(args):
        phaser = None
        sdr = None
        try:
            phaser, sdr = open_devices(name, rpi_uri, sdr_uri, args)
            configure_hardware(phaser, sdr, args)
            peak_hz, peak_db, freqs, power = run_sweep(phaser, sdr, args)
            result = HBCalResult(
                topology=name,
                rpi_uri=rpi_uri,
                sdr_uri=sdr_uri,
                sample_rate=float(args.sample_rate),
                rx_lo=float(args.rx_lo),
                sweep_start=float(args.sweep_start),
                sweep_stop=float(args.sweep_stop),
                sweep_step=float(args.sweep_step),
                peak_freq_hz=peak_hz,
                peak_power_db=peak_db,
                save_file=str(args.save_file),
            )
            print()
            print(f"[result] HB100 peak frequency: {peak_hz/1e9:.9f} GHz")
            print(f"[result] peak amplitude: {peak_db:.2f} dB")
            save_result(result, freqs, power, args)
            return 0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            last_exc = exc
            print(f"[fail] {name}: {type(exc).__name__}: {exc}")
        finally:
            if phaser is not None or sdr is not None:
                cleanup(phaser, sdr)

    print()
    print("[error] HB100 calibration failed for all requested topologies.")
    if last_exc is not None:
        print(f"[error] last failure: {type(last_exc).__name__}: {last_exc}")
    print("Try --topology direct if Pluto is plugged into the Windows PC.")
    print("Try --topology reference only after moving Pluto USB DATA to the Raspberry Pi.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
