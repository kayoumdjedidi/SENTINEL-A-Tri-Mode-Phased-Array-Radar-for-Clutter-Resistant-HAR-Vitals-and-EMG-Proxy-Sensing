#!/usr/bin/env python3
"""Generate CN0566 per-element array calibration files.

This is the calibration step that produces:

    gain_cal_val.pkl
    phase_cal_val.pkl

It is separate from HB100 frequency calibration.  The HB100 frequency file
(`hb100_freq_val.pkl`) tells the Phaser where the external source is; this
script uses that source to measure element gain/phase mismatch and save the
array calibration files consumed by `load_gain_cal()` / `load_phase_cal()`.
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


ROOT = Path(__file__).resolve().parents[1]
PHASER_EXAMPLES = ROOT / "pyadi-iio" / "examples" / "phaser"
if str(PHASER_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(PHASER_EXAMPLES))

from phaser_functions import channel_calibration, gain_calibration, phase_calibration  # noqa: E402


@dataclass
class ArrayCalMetadata:
    topology: str
    rpi_uri: str
    sdr_uri: str
    source: str
    hb100_freq_hz: float
    sample_rate: float
    rx_lo: float
    rx_buffer_size: int
    averages: int
    channel_file: str
    gain_file: str
    phase_file: str
    channel_cal: list[float]
    gain_cal: list[float]
    phase_cal: list[float]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phaser_array_calibrate",
        description="Calibrate CN0566 array gain/phase and save gain_cal_val.pkl / phase_cal_val.pkl.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--topology", choices=["reference", "direct", "auto"], default="reference")
    p.add_argument("--rpi-uri", default="ip:phaser.local")
    p.add_argument("--reference-sdr-uri", default="ip:phaser.local:50901")
    p.add_argument("--direct-sdr-uri", default="ip:192.168.2.1")
    p.add_argument("--source", choices=["hb100", "tx"], default="hb100")
    p.add_argument("--hb100-cal-file", type=Path, default=Path("hb100_freq_val.pkl"))
    p.add_argument("--default-signal-freq", type=float, default=10.525e9)
    p.add_argument("--sample-rate", type=float, default=30e6)
    p.add_argument("--rx-lo", type=float, default=2.2e9)
    p.add_argument("--rx-buffer-size", type=int, default=1024)
    p.add_argument("--rx-gain", type=int, default=6)
    p.add_argument("--tx-gain", type=int, default=-6)
    p.add_argument("--averages", type=int, default=4)
    p.add_argument("--timeout-ms", type=int, default=30000)
    p.add_argument("--channel-file", type=Path, default=Path("channel_cal_val.pkl"))
    p.add_argument("--gain-file", type=Path, default=Path("gain_cal_val.pkl"))
    p.add_argument("--phase-file", type=Path, default=Path("phase_cal_val.pkl"))
    p.add_argument("--metadata-file", type=Path, default=Path("phaser_array_cal_val.json"))
    p.add_argument("--no-channel-cal", action="store_true")
    p.add_argument("--no-prompt", action="store_true")
    p.add_argument("--print-contexts", action="store_true")
    return p


def print_cable_instructions(args: argparse.Namespace) -> None:
    print()
    print("Phaser array calibration cable checklist")
    print("---------------------------------------")
    if args.topology in {"reference", "auto"}:
        print("Reference topology:")
        print("  - Plug Pluto USB DATA into the Raspberry Pi attached to the Phaser.")
        print("  - Unplug Pluto USB DATA from the Windows PC for this calibration.")
        print("  - Keep CN0566/Phaser attached to the Raspberry Pi.")
        print(f"  - Expected SDR URI: {args.reference_sdr_uri}")
    if args.topology in {"direct", "auto"}:
        print("Direct topology:")
        print("  - Keep Pluto connected through the normal Windows/direct path.")
        print(f"  - Expected SDR URI: {args.direct_sdr_uri}")
    if args.source == "hb100":
        print("HB100 source:")
        print("  - Power the HB100 module from its proper supply.")
        print("  - Aim it at mechanical boresight, about 0.5-2 m from the array.")
        print("  - Keep people and moving objects away during calibration.")
        print(f"  - Expected frequency cache: {args.hb100_cal_file}")
    else:
        print("TX source:")
        print("  - This uses the onboard TX path instead of HB100.")
        print("  - Use only if you have the reference TX antenna/path configured.")
    print("Stop all other SENTINEL/radar Python processes first.")
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


def load_hb100_freq(args: argparse.Namespace) -> float:
    if args.hb100_cal_file.exists():
        with args.hb100_cal_file.open("rb") as f:
            freq = float(pickle.load(f))
        print(f"[hb100] loaded frequency cache: {freq/1e9:.9f} GHz from {args.hb100_cal_file}")
        return freq
    print(
        f"[hb100] {args.hb100_cal_file} not found; "
        f"using default {args.default_signal_freq/1e9:.6f} GHz."
    )
    return float(args.default_signal_freq)


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


def configure_hardware(phaser, sdr, signal_freq_hz: float, args: argparse.Namespace) -> None:
    time.sleep(0.5)
    phaser.configure(device_mode="rx")
    try:
        phaser.element_spacing = 0.014
    except Exception:
        pass

    # Match the ADI reference bring-up where possible.
    try:
        phaser.SDR_init(
            int(args.sample_rate),
            int(args.rx_lo),
            int(args.rx_lo),
            int(args.rx_gain),
            int(args.tx_gain),
            int(args.rx_buffer_size),
        )
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
    sdr.rx_lo = int(args.rx_lo)
    sdr.gain_control_mode_chan0 = "manual"
    sdr.gain_control_mode_chan1 = "manual"
    sdr.rx_hardwaregain_chan0 = int(args.rx_gain)
    sdr.rx_hardwaregain_chan1 = int(args.rx_gain)
    try:
        sdr.filter = "LTE20_MHz.ftr"
    except Exception:
        pass

    sdr.tx_enabled_channels = [0, 1]
    if args.source == "hb100":
        sdr.tx_hardwaregain_chan0 = -88
        sdr.tx_hardwaregain_chan1 = -88
        try:
            sdr.tx_destroy_buffer()
        except Exception:
            pass
    else:
        sdr.tx_hardwaregain_chan0 = -88
        sdr.tx_hardwaregain_chan1 = int(args.tx_gain)
        sdr.tx_lo = int(args.rx_lo)
        try:
            sdr.dds_single_tone(int(2e6), 0.9, 1)
        except Exception:
            pass

    phaser.SignalFreq = float(signal_freq_hz)
    phaser.frequency = int((float(signal_freq_hz) + float(args.rx_lo)) // 4)
    phaser.freq_dev_step = 5690
    phaser.freq_dev_range = 0
    phaser.freq_dev_time = 0
    phaser.powerdown = 0
    phaser.ramp_mode = "disabled"
    phaser.Averages = int(args.averages)
    phaser.set_beam_phase_diff(0.0)
    phaser.set_all_gain(127, apply_cal=False)
    time.sleep(0.25)


def apply_channel_cal(phaser, sdr, rx_gain: int) -> None:
    try:
        sdr.rx_hardwaregain_chan0 = int(round(float(rx_gain) + float(phaser.ccal[0])))
        sdr.rx_hardwaregain_chan1 = int(round(float(rx_gain) + float(phaser.ccal[1])))
    except Exception:
        pass


def run_array_calibration(name: str, rpi_uri: str, sdr_uri: str, args: argparse.Namespace) -> ArrayCalMetadata:
    signal_freq = load_hb100_freq(args) if args.source == "hb100" else float(args.default_signal_freq)
    phaser = None
    sdr = None
    try:
        phaser, sdr = open_devices(name, rpi_uri, sdr_uri, args)
        configure_hardware(phaser, sdr, signal_freq, args)

        if not args.no_channel_cal:
            print("[cal] Channel calibration...")
            channel_calibration(phaser, verbose=True)
            phaser.save_channel_cal(str(args.channel_file))
            apply_channel_cal(phaser, sdr, int(args.rx_gain))
            print(f"[save] channel calibration: {args.channel_file}")

        print("[cal] Per-element gain calibration...")
        gain_calibration(phaser, verbose=True)
        phaser.save_gain_cal(str(args.gain_file))
        print(f"[save] gain calibration: {args.gain_file}")

        print("[cal] Per-element phase calibration...")
        phase_calibration(phaser, verbose=True)
        phaser.save_phase_cal(str(args.phase_file))
        print(f"[save] phase calibration: {args.phase_file}")

        meta = ArrayCalMetadata(
            topology=name,
            rpi_uri=rpi_uri,
            sdr_uri=sdr_uri,
            source=args.source,
            hb100_freq_hz=float(signal_freq),
            sample_rate=float(args.sample_rate),
            rx_lo=float(args.rx_lo),
            rx_buffer_size=int(args.rx_buffer_size),
            averages=int(args.averages),
            channel_file=str(args.channel_file),
            gain_file=str(args.gain_file),
            phase_file=str(args.phase_file),
            channel_cal=[float(v) for v in getattr(phaser, "ccal", [0.0, 0.0])],
            gain_cal=[float(v) for v in getattr(phaser, "gcal", [1.0] * 8)],
            phase_cal=[float(v) for v in getattr(phaser, "pcal", [0.0] * 8)],
        )
        args.metadata_file.write_text(json.dumps(asdict(meta), indent=2), encoding="utf-8")
        print(f"[save] metadata: {args.metadata_file}")
        return meta
    finally:
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
        input("Press ENTER when HB100 is stable at boresight and the area is clear...")

    last_exc: Exception | None = None
    for name, rpi_uri, sdr_uri in topology_candidates(args):
        try:
            meta = run_array_calibration(name, rpi_uri, sdr_uri, args)
            print()
            print("[result] Phaser array calibration complete.")
            print(f"[result] gain file:  {meta.gain_file}")
            print(f"[result] phase file: {meta.phase_file}")
            print("[result] SENTINEL will load these files automatically from the current directory.")
            return 0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            last_exc = exc
            print(f"[fail] {name}: {type(exc).__name__}: {exc}")

    print()
    print("[error] Phaser array calibration failed for all requested topologies.")
    if last_exc is not None:
        print(f"[error] last failure: {type(last_exc).__name__}: {last_exc}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
