# SENTINEL Mode C - CW Reflectometry Muscle Proxy

Mode C is the SENTINEL "myo" mode. It uses the same CW hardware style as Mode B, but instead of tracking phase for breathing/heartbeat, it tracks amplitude-envelope changes that may correlate with local muscle activation.

## What Mode C Is Measuring

Mode C is not contact EMG. It does not measure motor-unit electrical potentials through electrodes.

It is a radar reflectometry proxy. A muscle contraction can change the received CW amplitude through small tissue motion, water redistribution, and dielectric/permittivity changes. The code treats increased amplitude-envelope spectral energy in the selected band as a relative activation feature called `myo-index`.

Use this wording for claims:

- Correct: "radar muscle-activity proxy", "myo-index", "reflectometry-derived activation feature"
- Not correct yet: "measured EMG", "clinical EMG", "muscle electrical signal"

## DSP Chain

Runtime: `sentinel_mode_c.py`

DSP: `myo_dsp.py`

Current chain:

1. Transmit a fixed 100 kHz IF CW tone through Pluto/CN0566.
2. Receive RX0+RX1 and coherently sum both channels.
3. Mix the IF tone to baseband with phase-continuous mixing.
4. Take the amplitude envelope with `abs(baseband)`.
5. Decimate to about 2 kHz envelope sample rate.
6. Remove the envelope mean from the current window to suppress DC carrier leakage.
7. FFT a short window, default `0.25 s`.
8. Sum spectral energy in the EMG-proxy band, default `10-500 Hz`.
9. Compute spectral centroid inside that band.
10. Normalize current energy against a rest baseline to produce `myo-index`.

The normalization is:

```text
myo_index = clip((E_current - E_baseline_mean) / (3 * E_baseline_std + eps), 0, 1)
```

The first `--baseline-sec` seconds must be a true rest period. If the subject moves or contracts during calibration, the baseline becomes invalid.

## What The Panels Mean

### IF Spectrum

Top-left panel.

This is the hardware sanity check. You should see a strong stable peak around the 100 kHz IF tone. If this peak is absent, unstable, or buried, Mode C is not measuring anything useful.

Good signs:

- One clear peak near 100 kHz.
- Noise floor well below the peak.
- No obvious clipping or flat-topped saturation.

Bad signs:

- No carrier peak.
- Peak jumps randomly while nothing moves.
- Peak is too close to full-scale or the spectrum looks clipped.

### Amplitude Spectrum

Top-right panel.

This is the current amplitude-envelope spectrum after baseband mixing and decimation. The orange shaded band is the band used for the myo-index, default `10-500 Hz`.

During rest, this should be relatively low and stable. During a controlled contraction, energy in the shaded band should rise. A real response should be repeatable and time-locked to clench/release blocks.

This panel is not an EMG waveform. It is an amplitude-modulation spectrum.

### Myo-Index

Bottom-left panel.

This is the normalized activation feature from `0` to `1`.

Expected behavior:

- During calibration/rest: stays near `0`.
- During a deliberate contraction: rises above the threshold line.
- During release: falls back toward `0`.

If it sticks at `1.0` during rest, the current run is not valid. Recalibrate while fully still, reduce RX gain if saturated, or raise the threshold.

### Spectral Centroid

Bottom-right panel.

This is the energy-weighted center frequency of the active band. It can help distinguish broad high-frequency muscle-like activity from low-frequency bulk motion.

Useful behavior:

- During true contraction, centroid may shift consistently or occupy a repeatable range.
- During gross motion, centroid often jumps erratically or tracks the motion transient rather than the held contraction.

Do not use centroid alone as proof of muscle activation.

## How To Know If It Is Really Working

A valid Mode C response should pass three tests.

### 1. Hardware Alive Test

Run:

```powershell
python .\sentinel_mode_c.py --no-record
```

Check:

- IF Spectrum has a clear 100 kHz peak.
- Calibration completes after the configured rest period.
- At rest, `myo-index` remains mostly below threshold.

### 2. Controlled Clench/Rest Test

Aim the beam at one nearby muscle group, ideally forearm or bicep, at close range.

Protocol:

```text
0-3 s: rest and calibrate
3-8 s: rest
8-12 s: clench/contract
12-17 s: rest
17-21 s: clench/contract
21-26 s: rest
```

Expected:

- `myo-index` rises during clench blocks.
- `ACTIVE` appears during clench blocks.
- `myo-index` drops during rest blocks.
- The amplitude spectrum shows more energy in the shaded band during clench.

If the response is not time-locked to clench/release, do not treat it as a valid muscle proxy.

### 3. Negative Control Test

Aim at a static object or away from the muscle.

Expected:

- IF carrier may still exist.
- `myo-index` should stay low.
- `ACTIVE` should not repeatedly trigger.

If static objects produce the same activation pattern as the muscle, the threshold/baseline/environment is wrong.

## Interpreting The Screenshot

The screenshot shows a strong IF carrier, so the RF path is alive. The myo-index also reaches `1.0` for long periods.

That can mean two different things:

- Valid if those high periods line up with deliberate contraction/hold blocks.
- Invalid if you were resting, moving casually, or the run calibrated while you were not still.

For proof, record a labeled clench/rest sequence and compare the myo-index transitions to the labels.

## Tuning

Useful commands:

```powershell
python .\sentinel_mode_c.py --baseline-sec 5 --threshold 0.4
python .\sentinel_mode_c.py --rx-gain 45 --baseline-sec 5 --threshold 0.4
python .\sentinel_mode_c.py --band-lo 20 --band-hi 450 --threshold 0.4
```

Tuning rules:

- If `myo-index` is always high at rest, press `Recalibrate` while still.
- If it still stays high, reduce `--rx-gain` or raise `--threshold`.
- If contractions do nothing, improve aiming/range, raise RX gain carefully, or lower threshold.
- If movement dominates, brace the limb and isolate isometric contractions.
- If the IF peak is clipped or near saturation, lower RX gain before trusting the index.

## Failure Modes

Common false positives:

- Whole-arm motion instead of muscle contraction.
- Cable/table movement.
- Phaser or subject moving during calibration.
- Multipath changes from people moving nearby.
- RX saturation.
- Baseline rest period contaminated by contraction or motion.

Common false negatives:

- Beam aimed at the wrong body part.
- Target too far away.
- RX gain too low.
- Muscle contraction too weak or not isometric.
- Band settings exclude the actual modulation.

## Validation Standard

Mode C should be considered "working" only when:

- The IF carrier is stable.
- Rest baseline produces low myo-index.
- Repeated clench/rest trials produce repeatable myo-index transitions.
- Static-object negative control does not produce similar activation.
- Optional ground truth, such as video or contact EMG, agrees with the radar trend.

Until then, call it an experimental radar myo-index, not EMG.
