Mode B v1 Execution Brief
Summary
Build Mode B as a standalone CW vitals runtime first. Do not integrate it into sentinel_radar.py yet.
Reuse:
RADAR/phaser_perso/sentinel_radar.py for runtime/threading/recording structure
RADAR/phaser_perso/CW_IF_Scope_new.py for CW hardware bring-up
RADAR/matlab_phaser/heartbeat1.m as the DSP truth source
First success target: a live GUI that shows stable CW capture, phase-derived displacement, respiration estimate, heartbeat estimate, and recording support.
Implementation Changes
Add a CW backend module with connect(), capture(), and close() matching the style of the current radar backends.
Add CWConfig and CWCaptureResult types. capture() should return 1-D complex64 IQ plus config metadata.
CW hardware defaults:
output_freq = 10.25e9
signal_freq = 100e3
sample_rate = 0.6e6
frame_size = 8192
ramp disabled
RX0+RX1 enabled for AD9361 reliability; channels are coherently summed in v1
Add a CW DSP module that does:
FFT around the IF tone
complex mix-down to baseband
decimation to 100 Hz phase stream
rolling history buffer of 20 s
amplitude normalization
phase unwrap
slow drift removal
displacement conversion from phase using wavelength
respiration estimate from 0.1–0.6 Hz
heartbeat estimate from 0.8–2.5 Hz
confidence from in-band peak prominence / peak-to-noise ratio

Heartbeat Interpretation
Mode B does not measure the heart electrically like ECG. It measures mechanical
micro-motion in the radar phase. A reflector displacement `d` changes the
round-trip phase by `4*pi*d/lambda`; at 10.25 GHz, lambda is about 29 mm, so
sub-mm chest/skin motion is visible in phase.

The DSP path unwraps phase, removes slow drift, then separates bands:
- respiration: 0.15-0.50 Hz, about 9-30 breaths/min
- heartbeat candidate: 0.80-2.50 Hz, about 48-150 beats/min

The HR number is therefore the strongest sufficiently confident periodic motion
inside the heart-rate band. It is not a direct cardiac electrical measurement.
It can be contaminated by respiration harmonics, body motion, cable movement,
and multipath. A plausible HR reading should be validated against a watch,
pulse oximeter, or ECG reference.

Expected live behavior:
- IF Spectrum: strong stable carrier near the 100 kHz IF tone
- IF Waterfall: stable horizontal carrier line, with slow brightness/sideband changes
- Phase Displacement: large slow respiration trace plus smaller faster HR-band trace
- Phase PSD: distinct respiration peak and, when SNR is good, a separate HR-band peak

False-HR warning:
If the HR readout follows deliberate breathing-rate changes, jumps with small
body shifts, or sits exactly on a breathing harmonic, treat it as motion leakage
rather than verified heartbeat. True HR should remain close to an external pulse
reference while respiration changes independently.

Add sentinel_mode_b.py as the live entrypoint with a non-blocking worker thread and a PyQt/pyqtgraph GUI.
GUI panels:
IF FFT around tone
IF waterfall
phase/displacement trace
respiration and heartbeat spectra with numeric BPM readouts
GUI/CLI controls:
beam steering
RX gain
RF output frequency
record session toggle
--rpi-uri
--sdr-uri
--output-freq
--sample-rate
--signal-freq
--rx-gain
--frame-size
--history-sec
--phase-fs
--save-dir
Recording format should mirror Mode A conventions but be CW-specific:
config.json
cw_iq.npy
vitals.json
optional labels.json
Add offline replay support using the same CW DSP functions as live mode.
Claude Execution Guidance
Do not redesign the architecture from scratch.
Port the vitals chain from heartbeat1.m faithfully, then adapt it to the existing Python runtime style.
Keep Mode B standalone in v1.
Reuse existing recording/session patterns where practical instead of inventing a new format.
Keep shared utilities factored cleanly so later scheduler integration is easy.
Do not modify Mode A behavior unless a small shared helper change is required.
Test Plan
Add unit tests with synthetic IQ whose phase contains known respiration and heartbeat frequencies.
Verify respiration-only input returns correct RR and low heart confidence.
Verify heartbeat-only input returns correct HR and low respiration confidence.
Verify mixed RR+HR input resolves both peaks correctly.
Verify phase-to-displacement conversion is numerically correct.
Verify save/replay preserves estimates within tolerance.
Run py_compile on new modules and run the relevant pytest tests.
Hardware acceptance:
static scene: stable tone, low-confidence vitals, no random BPM jumps
speaker/shaker surrogate: known low-frequency motion appears at expected band
seated subject: respiration stabilizes first, heartbeat appears with lower confidence
repeated launch/close does not leave Pluto TX busy
Assumptions And Defaults
v1 is single-subject and mostly stationary.
Manual steering is acceptable in v1.
No Mode A / Mode B scheduler work in this task.
No multi-person logic in this task.
heartbeat1.m is the DSP reference; CW_IF_Scope_new.py is the hardware/UI reference.
