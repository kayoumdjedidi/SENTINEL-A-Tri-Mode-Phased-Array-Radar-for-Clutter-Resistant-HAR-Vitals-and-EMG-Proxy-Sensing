# HB100 Calibration Procedure

Use `hb100_calibrate.py` to measure the external HB100 source once and save
`hb100_freq_val.pkl`. `sentinel_closed_loop.py --hb100-cal auto` can then reuse
that file without re-running the sweep.

## Current Verified Calibration State

As of the latest successful run, this directory contains the full calibration set:

- `hb100_freq_val.pkl`
- `hb100_freq_val.json`
- `hb100_freq_val.npz`
- `channel_cal_val.pkl`
- `gain_cal_val.pkl`
- `phase_cal_val.pkl`
- `phaser_array_cal_val.json`

The recorded HB100 peak is:

```text
10.423193359 GHz
```

The recorded array-calibration metadata says:

```text
topology: reference-rpi
CN0566:   ip:phaser.local
Pluto:    ip:phaser.local:50901
source:   hb100
sample:   30 MSPS
rx_lo:    2.2 GHz
averages: 4
```

If SENTINEL still prints missing `gain_cal_val.pkl` / `phase_cal_val.pkl`, check
that you launched it from `RADAR\phaser_perso` or that these files remain in the
same directory as `radar_backends.py`. The loader now checks both the current
working directory and the script directory.

## Reference Cable Topology

This matches ADI `phaser_find_hb100.py`.

- Plug Pluto USB DATA into the Raspberry Pi attached to the Phaser.
- Unplug Pluto USB DATA from the Windows PC.
- Keep the CN0566/Phaser attached to the Raspberry Pi.
- Keep the Windows PC on the same network as `phaser.local`.
- Power the HB100 and aim it at the Phaser array boresight from about 0.5-2 m.
- Stop all other radar/SENTINEL Python processes.

Run:

```powershell
python .\hb100_calibrate.py --topology reference --print-contexts
```

Expected SDR URI:

```text
ip:phaser.local:50901
```

## Direct Fallback Topology

Use this if Pluto remains attached to the Windows PC and the forwarded RPi
context is not available.

- Plug Pluto USB DATA into the Windows PC, as used by normal SENTINEL tests.
- Keep CN0566/Phaser reachable at `ip:phaser.local`.
- Power and aim the HB100 at boresight.

Run:

```powershell
python .\hb100_calibrate.py --topology direct --sample-rate 4000000 --sweep-step 1000000 --print-contexts
```

## Outputs

The script saves:

- `hb100_freq_val.pkl`: frequency value used by ADI/SENTINEL code.
- `hb100_freq_val.json`: metadata for the run.
- `hb100_freq_val.npz`: stitched spectrum for offline inspection.

This is separate from the optional CN0566 array calibration files:

- `gain_cal_val.pkl`: per-element gain calibration used by `CN0566.load_gain_cal()`.
- `phase_cal_val.pkl`: per-element phase calibration used by `CN0566.load_phase_cal()`.

If those array files are absent, SENTINEL uses default gain/phase corrections.
That does not mean the HB100 frequency cache is missing.

## Phaser Array Gain/Phase Calibration

Run this after `hb100_calibrate.py` if you want SENTINEL to stop using default
per-element array calibration. This step uses the already measured HB100
frequency and generates the two files that `CN0566.load_gain_cal()` and
`CN0566.load_phase_cal()` look for.

Use the same reference cable topology as the HB100 sweep:

```powershell
python .\phaser_array_calibrate.py --topology reference --print-contexts
```

Expected outputs:

- `channel_cal_val.pkl`: optional RX channel compensation.
- `gain_cal_val.pkl`: per-element gain correction.
- `phase_cal_val.pkl`: per-element phase correction.
- `phaser_array_cal_val.json`: metadata and saved correction values.

The file roles are different:

- `hb100_freq_val.pkl` means "what frequency is my external HB100 source?"
- `gain_cal_val.pkl` means "how much should each array element be amplitude-corrected?"
- `phase_cal_val.pkl` means "how much should each array element be phase-corrected?"

If `hb100_freq_val.pkl` exists but `gain_cal_val.pkl` / `phase_cal_val.pkl` do
not, the HB100 frequency calibration succeeded but the array calibration has
not been run yet.

After a successful run, launch SENTINEL with:

```powershell
python .\sentinel_closed_loop.py --hb100-cal auto --cal-expected-range-m 1.0 --cal-target-max-m 3.0 --auto-interrogate
```

Use `--auto-interrogate` if you want Modes B/C to start automatically after a
target lock. Without it, lock only means Mode A has a target; you must press
`Interrogate locked target` in the GUI to run Modes B/C.
