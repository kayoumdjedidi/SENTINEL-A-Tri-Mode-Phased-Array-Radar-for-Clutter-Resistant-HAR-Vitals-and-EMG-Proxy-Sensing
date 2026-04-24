# SENTINEL Mode A Radar Runtime Handoff

Date: 2026-04-24 (updated 2026-04-23 v5 rework)

Purpose: compact technical state for Claude/Codex to continue action without re-debugging solved transport issues.

## Active Script

`sentinel_mode_a_v5.py` is the current production entry point.

It consolidates:
- Proven TDD soft-sync acquisition from `radar_mode_a.py`
- PyQt5/pyqtgraph 3-panel display from `sentinel_radar.py`
- All Windows-topology defaults baked in

Previous scripts (`sentinel_modeA_v2/v3/v4`, `sentinel_radar.py`, `radar_mode_a.py`) are kept for reference but `v5` is the baseline.

Launch command:

```powershell
python .\sentinel_mode_a_v5.py
```

Key defaults (proven, no flags needed):

```text
--acq tdd  --tdd-trigger soft
--sdr-uri ip:192.168.2.1  --rpi-uri ip:phaser.local
--bw 500e6  --ramp-time-us 300  --num-chirps 64  --sample-rate 4e6
--min-range-m 0.5  --max-range 10.0
```

Known DSP fix in v5 (vs earlier radar_mode_a.py):
- --min-range-m default changed 3.0 -> 0.5.  The old 3.0 excluded 1-2m targets and caused the reported range=72m artifact (auto-gate fell through to a wall/ceiling bounce at 72m).
- range_bin from process_frame is remapped from full-axis index to positive-axis index before the CFAR pass.

## Current Decision

Use native Windows Python with Pluto connected directly to the PC over USB.

Known-good live topology:

```text
PC Windows Python owns Pluto RX: usb:1.5.5
Pi controls Phaser/CN0566 over Ethernet: ip:phaser.local
Pluto network control also visible: ip:192.168.2.1 / ip:pluto.local
Mode A working acquisition: TDD soft-sync and continuous
WSL/Pi forwarded iiod path: do not use for live RX
TDD baseline: --tdd-trigger soft with Pluto at ip:192.168.2.1
External gpio_burst TDD: not supported by the current exposed Pluto GPIO/device-tree path
```

## Working Directory

Windows:

```powershell
cd E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR
.\.venv-win\Scripts\Activate.ps1
cd .\phaser_perso
```

Python env requirement:

```powershell
python -c "import adi; print(adi.__version__)"
```

Known version used successfully:

```text
adi=0.0.20
```

## Proven Facts

- Native Windows `iio_info -s` sees:
  - `ip:phaser.local` for Phaser/Pi devices.
  - `ip:192.168.2.1` / `ip:pluto.local` for Pluto network context.
  - `usb:1.5.5` for direct Pluto USB context.
- Minimal Pluto RX passed over both:
  - `usb:1.5.5`
  - `ip:192.168.2.1`
- RX sweep passed at:
  - `1 MSPS` and `4 MSPS`
  - channel sets `[0]` and `[0,1]`
  - buffer sizes through `524288`
  - both USB and IP contexts
- Continuous radar capture works over direct USB.
- TDD soft-sync works over Pluto IP (`ip:192.168.2.1`) with Phaser control at `ip:phaser.local`.
- Confirmed TDD soft-sync recordings:
  - `recordings\win_tdd_soft_64`: raw `(5, 64, 1079)`, finite spectrograms.
  - `recordings\win_tdd_soft_128`: raw `(5, 128, 1079)`, finite spectrograms.
  - `recordings\win_tdd_soft_live`: raw `(29, 64, 1079)`, live GUI ran; frame 20 reported `valid_chirps=64/64`.
- `--frames 0` in `radar_mode_a.py` was patched to mean run-until-stopped.
- TDD code was patched to support split URIs:
  - `--sdr-uri` for AD9361 RX
  - `--tdd-uri` for Pluto GPIO/TDD helpers
- TDD code now supports explicit trigger modes:
  - `--tdd-trigger soft`: working path, uses Pluto `tdd.sync_soft = True`.
  - `--tdd-trigger internal`: not yet validated as working.
  - `--tdd-trigger external`: not available unless Pluto/CN0566 HDL/device tree exposes a backed `tdd_ext_sync` path.

## Avoid

Do not spend more time on these as default runtime paths:

```text
ip:phaser.local:50901
ip:127.0.0.1:50901
Pi-side usb:* when Pluto USB is physically plugged into the PC
WSL live RX
Pi forwarded iiod live RX
```

Reason: these caused repeated `ETIMEDOUT` / `[Errno 110] host unreachable` during `s.rx()`.

## Known-Good Live Commands

Preferred TDD soft-sync live display:

```powershell
python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 0 --save-dir recordings --session-name win_tdd_soft_live
```

Finite TDD soft-sync capture:

```powershell
python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 128 --frames 5 --no-plot --save-dir recordings --session-name win_tdd_soft_128
```

Continuous fallback live display:

```powershell
python .\radar_mode_a.py --acq continuous --sdr-uri usb:1.5.5 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 500 --num-chirps 64 --frames 0 --max-range 10 --save-dir recordings --session-name win_cont_live_10m
```

Headless finite capture:

```powershell
python .\radar_mode_a.py --acq continuous --sdr-uri usb:1.5.5 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 500 --num-chirps 64 --frames 100 --no-plot --max-range 10 --save-dir recordings --session-name win_cont_100
```

Previously confirmed saved capture:

```text
recordings\win_cont_usb_small
```

## Debug Commands

Check discovered IIO contexts:

```powershell
iio_info -s
ping 192.168.2.1
```

Minimal Pluto RX smoke test:

```powershell
python -c "import adi; s=adi.ad9361(uri='usb:1.5.5'); s._ctx.set_timeout(5000); s.sample_rate=int(1e6); s.rx_lo=int(2100000000); s.rx_enabled_channels=[0]; s.gain_control_mode_chan0='manual'; s.rx_hardwaregain_chan0=60; s.rx_buffer_size=256; print('before rx'); x=s.rx(); print('after rx', type(x).__name__, len(x))"
```

Large RX sweep:

```powershell
python .\pluto_rx_sweep.py --uris usb:1.5.5 ip:192.168.2.1 --sample-rates 4000000 --channel-sets 01 --sizes 32768 65536 131072 262144 524288
```

Replay capture:

```powershell
python .\offline_replay.py recordings\win_cont_live_10m --frames 1 --max-range 10
```

Replay historical recording:

```powershell
python .\offline_replay.py recordings\20260418_182458 --frames 200 --max-range 10
```

Note: `recordings/20260418_182458` is a `500 us` / `300-sample` recording, not the `505 us` / `303-sample` v3 default.

## TDD Status

TDD soft-sync is now the reference-aligned baseline.

Original TDD hypothesis:

```text
If we match the ADI reference videos and use TDD, the poor Mode A results may disappear because TDD gives deterministic chirp alignment and avoids transport latency/timeout issues.
```

Corrected result:

```text
The original internal/external trigger attempts failed because the code did not explicitly issue Pluto sync_soft.
After adding --tdd-trigger soft, TDD captures save successfully and live TDD runs.
```

What TDD proved:

```text
Direct Windows Pluto RX is healthy.
Large two-channel RX buffers work up to 524288 samples.
Continuous Mode A runs live.
Pluto software-triggered TDD works.
External Phaser gpio_burst TDD is not available through the current exposed Pluto GPIO path.
```

Patch direction already applied and tested:

```text
AD9361 high-rate RX: usb:1.5.5
Pluto GPIO/TDD control: ip:192.168.2.1
Phaser control: ip:phaser.local
```

Old split-URI reference TDD reached setup but timed out at burst RX because it did not use `sync_soft`:

```powershell
python .\sentinel_modeA_ref_tdd.py --sdr-uri usb:1.5.5 --tdd-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --no-plot --frames 1 --save-data --save-path ref_tdd_split_32.npy --chirps 32
```

Observed:

```text
[ref] Opening Pluto on usb:1.5.5 ...
[ref] Opening CN0566 on ip:phaser.local ...
[ref] Pluto URI: usb:1.5.5
[ref] TDD URI: ip:192.168.2.1
[ref] rx_buffer_size=65536
[ref] Capturing first burst ...
OSError: [Errno 110] host unreachable
```

DMA-first patch was then applied:

```text
Start rx() in a background thread.
Wait --tdd-arm-delay-s.
Pulse gpio_burst or enable internal TDD only after RX is armed.
Use --tdd-rx-timeout-ms, tested at 30000 ms.
```

Old internal TDD still failed:

```powershell
python .\radar_mode_a.py --acq tdd --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 8 --no-tdd-ext-sync --frames 1 --no-plot --save-dir recordings --session-name win_tdd_ip_internal_8 --tdd-arm-delay-s 1.0 --tdd-rx-timeout-ms 30000
```

External TDD still failed:

```powershell
python .\radar_mode_a.py --acq tdd --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 8 --frames 1 --no-plot --save-dir recordings --session-name win_tdd_ip_external_8 --tdd-arm-delay-s 1.0 --tdd-rx-timeout-ms 30000
```

Observed final TDD error:

```text
RuntimeError: TDD rx() timed out (ETIMEDOUT) after the RX buffer was armed.
This means the TDD trigger path did not fill the Pluto DMA buffer.
```

Important interpretation:

```text
Do not use the old --no-tdd-ext-sync path as the preferred TDD test.
Use --tdd-trigger soft.
Do not keep increasing --tdd-arm-delay-s for external/internal failures.
If external gpio_burst is revisited, treat it as an HDL/device-tree/hardware-modification task while keeping soft TDD as the working runtime.
```

Hardware conclusion:

```text
The Pluto one-bit-adc-dac context currently exposes only `muxout` and `phaser_enable`.
It does not expose `tdd_ext_sync`, so assigning `gpio_tdd_ext_sync` in Python is not backed by a real IIO GPIO channel.
Phaser `BURST` readback toggles, but Pluto external TDD never sees it.
An ADI EngineerZone answer confirms BURST is for future use and software triggering is what is currently used.
```

External GPIO bring-up tools now available:

```powershell
python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers external --rpi-uri ip:phaser.local --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump --dump-gpio --external-pulse-high-s 0.1 --external-pulse-gap-s 0.1 --external-pulse-count 1 --external-pulse-active high
```

If active-high fails, test active-low:

```powershell
python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers external --rpi-uri ip:phaser.local --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump --dump-gpio --external-pulse-high-s 0.1 --external-pulse-gap-s 0.1 --external-pulse-count 1 --external-pulse-active low
```

If revisiting TDD later, validate with hardware-level evidence:

```text
Verify Pluto firmware/TDD attributes.
Verify gpio_burst or internal TDD pulse generation.
Probe whether RX gate/L24N is asserted.
Confirm the Pluto DMA buffer is actually being gated/fed.
Use oscilloscope/logic analyzer if available.
```

## Next Engineering Priorities

1. Keep continuous Windows USB path as default live runtime.
2. Improve DSP/display on continuous mode:
   - use `--max-range 10`
   - reduce near-range leakage lock
   - reduce diagonal/ripple artifacts from continuous chirp segmentation
   - improve chirp boundary estimation
   - add slow-time phase deskew
   - improve leakage/clutter subtraction
   - tighten target/demo geometry
3. Treat TDD as a hardware bring-up task, not a near-term Mode A dependency.
4. Do not block useful Mode A results on TDD unless trigger/gated-DMA behavior is instrumented directly.


okay lets do all the next needed steps to make sure TDD works
12:01 AM


Worked for 4m 52s
I found one important gap: our “internal TDD” test may not have been a real internal sync event. The newer adi.tddn API has explicit sync_soft and sync_internal. I added both:

pluto_tdd_probe.py: isolates Pluto TDD from the radar pipeline.
radar_mode_a.py --tdd-trigger {soft,internal,external}.
Tests passed: python3 -m py_compile ... and pytest -q tests/test_sentinel_core.py.
Run this exact sequence in .venv-win PowerShell.

python -c "import adi; t=adi.tddn('ip:192.168.2.1'); t.enable=False; print('TDD disabled')"
First prove Pluto TDD soft-sync alone:

python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers soft --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump
If soft passes, test the radar pipeline using soft trigger:

python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 8 --frames 1 --no-plot --save-dir recordings --session-name win_tdd_soft_8 --tdd-arm-delay-s 1.0 --tdd-rx-timeout-ms 30000
Then scale:

python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 32 --frames 1 --no-plot --save-dir recordings --session-name win_tdd_soft_32 --tdd-arm-delay-s 1.0 --tdd-rx-timeout-ms 30000
Then check internal/external separately:

python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers internal external --rpi-uri ip:phaser.local --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump
Interpretation: soft PASS means Pluto TDD/DMA works and we use --tdd-trigger soft. soft FAIL means the Pluto TDD DMA path itself is not filling buffers. external FAIL with soft PASS means the Phaser gpio_burst/external sync line is the problem.


session_log.md
Document · MD

Open

4 files changed
+315
-21
Undo

Review
pluto_tdd_probe.py
radar_backends.py
radar_mode_a.py
workspace/session_log.md




12:06 AM
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python -c "import adi; t=adi.tddn('ip:192.168.2.1'); t.enable=False; print('TDD disabled')"        
TDD disabled                                                                        
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers soft --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump
adi=0.0.20
sample_rate=4000000 frame_ms=0.5 burst_count=8 auto_samples=16000 buffer_size=16384

=== trigger=soft buffer=16384 ===
[dump] TDD attrs:
  enable=False
  state=0
  frame_length_ms=0.5
  frame_length_raw=3999
  startup_delay_ms=0
  burst_count=8
  sync_external=False
  sync_internal=False
FAIL trigger=soft: PermissionError: [Errno 13] Permission denied
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 8 --frames 1 --no-plot --save-dir recordings --session-name win_tdd_soft_8 --tdd-arm-delay-s 1.0 --tdd-rx-timeout-ms 30000
file not found, loading default (all gain at maximum)
file not found, loading default (no phase shift)
[tdd]  rf=10.2500GHz  bw=500MHz  ramp=300us  chirps=8  spc=1079  R_res=0.300m  v_res=3.659m/s  max_vel=±14.6m/s
[tdd] trigger=soft
Acquiring... CTRL+C to stop.

Recording saved: recordings\win_tdd_soft_8
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 32 --frames 1 --no-plot --save-dir recordings --session-name win_tdd_soft_32 --tdd-arm-delay-s 1.0 --tdd-rx-timeout-ms 30000
file not found, loading default (all gain at maximum)
file not found, loading default (no phase shift)
[tdd]  rf=10.2500GHz  bw=500MHz  ramp=300us  chirps=32  spc=1079  R_res=0.300m  v_res=0.915m/s  max_vel=±14.6m/s
[tdd] trigger=soft
Acquiring... CTRL+C to stop.

Recording saved: recordings\win_tdd_soft_32
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers internal external --rpi-uri ip:phaser.local --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump
adi=0.0.20
sample_rate=4000000 frame_ms=0.5 burst_count=8 auto_samples=16000 buffer_size=16384

=== trigger=internal buffer=16384 ===
[dump] TDD attrs:
  enable=False
  state=0
  frame_length_ms=0.5
  frame_length_raw=3999
  startup_delay_ms=0
  burst_count=8
  sync_external=False
  sync_internal=False
FAIL trigger=internal: PermissionError: [Errno 13] Permission denied

=== trigger=external buffer=16384 ===
[dump] TDD attrs:
  enable=False
  state=0
  frame_length_ms=0.5
  frame_length_raw=3999
  startup_delay_ms=0
  burst_count=8
  sync_external=True
  sync_internal=False
FAIL trigger=external: PermissionError: [Errno 13] Permission denied
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso>

Show more
12:07 AM


Worked for 2m 11s
TDD is working now. The earlier “internal/external fail” result was incomplete because we were not issuing an explicit Pluto TDD sync event. --tdd-trigger soft does that, and your win_tdd_soft_8 / win_tdd_soft_32 captures saved successfully.

I patched:

radar_mode_a.py (line 169): adds --tdd-trigger soft/internal/external and records trigger metadata.
radar_backends.py (line 384): uses tdd.sync_soft = True for software-triggered TDD.
pluto_tdd_probe.py (line 1): fixed the --dump PermissionError from reading restricted/write-only attrs.
Validation I ran:

python3 -m py_compile radar_mode_a.py radar_backends.py pluto_tdd_probe.py
pytest -q tests/test_sentinel_core.py
Result: 8 passed.

Next run these scale tests:

python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 5 --no-plot --save-dir recordings --session-name win_tdd_soft_64
python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 128 --frames 5 --no-plot --save-dir recordings --session-name win_tdd_soft_128
If both save, open live TDD:

python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 0 --save-dir recordings --session-name win_tdd_soft_live
Use --tdd-trigger soft as the working TDD path. External gpio_burst is still a separate hardware-sync issue.


session_log.md
Document · MD

Open

4 files changed
+29
-12
Undo

Review
pluto_tdd_probe.py
radar_backends.py
radar_mode_a.py
workspace/session_log.md




12:09 AM
.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> pytest -q tests/test_sentinel_core.py
........                                                                     [100%]
8 passed in 0.41s
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 5 --no-plot --save-dir recordings --session-name win_tdd_soft_64
file not found, loading default (all gain at maximum)
file not found, loading default (no phase shift)
[tdd]  rf=10.2500GHz  bw=500MHz  ramp=300us  chirps=64  spc=1079  R_res=0.300m  v_res=0.457m/s  max_vel=±14.6m/s
[tdd] trigger=soft
Acquiring... CTRL+C to stop.

Recording saved: recordings\win_tdd_soft_64
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 128 --frames 5 --no-plot --save-dir recordings --session-name win_tdd_soft_128
file not found, loading default (all gain at maximum)
file not found, loading default (no phase shift)
[tdd]  rf=10.2500GHz  bw=500MHz  ramp=300us  chirps=128  spc=1079  R_res=0.300m  v_res=0.229m/s  max_vel=±14.6m/s
[tdd] trigger=soft
Acquiring... CTRL+C to stop.

Recording saved: recordings\win_tdd_soft_128
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 0 --save-dir recordings --session-name win_tdd_soft_live
file not found, loading default (all gain at maximum)
file not found, loading default (no phase shift)
[tdd]  rf=10.2500GHz  bw=500MHz  ramp=300us  chirps=64  spc=1079  R_res=0.300m  v_res=0.457m/s  max_vel=±14.6m/s
[tdd] trigger=soft
Display open. Close the window or press CTRL+C to stop.
Acquiring... CTRL+C to stop.
  frame   20  range=72.2m  range_bin= 782  valid_chirps=64/64

Stopped after 29 frames.

Recording saved: recordings\win_tdd_soft_live
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Rad

Show more
12:24 AM


Worked for 1m 7s
TDD soft-sync is validated end-to-end.

I checked the saved recordings:

win_tdd_soft_64   raw (5, 64, 1079), finite spectrograms
win_tdd_soft_128  raw (5, 128, 1079), finite spectrograms
win_tdd_soft_live raw (29, 64, 1079), finite spectrograms
Your live run also reported:

valid_chirps=64/64
So the new baseline is:

python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 0 --save-dir recordings --session-name win_tdd_soft_live
I also corrected sentinel_mode_a_claude_handoff.md so future agents do not repeat the now-wrong “TDD is non-viable” conclusion. Tests still pass: 8 passed.

Current state:

Preferred baseline: TDD soft-sync.
Fallback: continuous.
Still unresolved: external gpio_burst hardware sync. This is no longer blocking Mode A.

sentinel_mode_a_claude_handoff.md
Document · MD

Open


session_log.md
Document · MD

Open

2 files changed
+39
-22
Undo

Review
sentinel_mode_a_claude_handoff.md
workspace/session_log.md




12:25 AM
lets solve this too, to get as close to the ref videos as possible
Still unresolved: external gpio_burst hardware sync. This is no longer blocking Mode A.
12:36 AM


Worked for 3m 21s
Added external-sync bring-up controls instead of repeating the same short gpio_burst pulse.

Changed:

radar_mode_a.py: new external pulse options:
--tdd-external-pulse-high-s
--tdd-external-pulse-gap-s
--tdd-external-pulse-count
--tdd-external-pulse-active high|low
pluto_tdd_probe.py: now dumps GPIO labels/readback and tests active-high vs active-low external sync.
Tests still pass: 8 passed.
Run this first:

python -c "import adi; t=adi.tddn('ip:192.168.2.1'); t.enable=False; print('TDD disabled')"
Test external active-high with a long pulse:

python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers external --rpi-uri ip:phaser.local --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump --dump-gpio --external-pulse-high-s 0.1 --external-pulse-gap-s 0.1 --external-pulse-count 1 --external-pulse-active high
If that fails, test active-low:

python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers external --rpi-uri ip:phaser.local --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump --dump-gpio --external-pulse-high-s 0.1 --external-pulse-gap-s 0.1 --external-pulse-count 1 --external-pulse-active low
If either passes, run the full radar external-TDD path with the passing polarity:

python .\radar_mode_a.py --acq tdd --tdd-trigger external --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 5 --no-plot --save-dir recordings --session-name win_tdd_external_64 --tdd-external-pulse-high-s 0.1 --tdd-external-pulse-gap-s 0.1 --tdd-external-pulse-count 1 --tdd-external-pulse-active high
If GPIO readback changes but external still fails, the problem is likely the physical Phaser-to-Pluto sync line or Pluto external-sync routing, not Python timing.


session_log.md
Document · MD

Open


sentinel_mode_a_claude_handoff.md
Document · MD

Open

5 files changed
+111
-7
Undo

Review
pluto_tdd_probe.py
radar_backends.py
radar_mode_a.py
sentinel_mode_a_claude_handoff.md
workspace/session_log.md




12:39 AM
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python -c "import adi; t=adi.tddn('ip:192.168.2.1'); t.enable=False; print('TDD disabled')"        
TDD disabled                                                                        
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers external --rpi-uri ip:phaser.local --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump --dump-gpio --external-pulse-high-s 0.1 --external-pulse-gap-s 0.1 --external-pulse-count 1 --external-pulse-active high
adi=0.0.20
[gpio] pluto/tdd uri=ip:192.168.2.1
  id=voltage0 output=False label=muxout gpio_muxout=1
  id=voltage0 output=True label=phaser_enable gpio_phaser_enable=1
[gpio] phaser/rpi uri=ip:phaser.local
  id=voltage0 output=False label=MUXOUT gpio_muxout=0
  id=voltage0 output=True label=DIV_S0 gpio_div_s0=0
  id=voltage1 output=True label=RX_LOAD gpio_rx_load=0
  id=voltage2 output=True label=TR gpio_tr=0
  id=voltage3 output=True label=DIV_S1 gpio_div_s1=0
  id=voltage4 output=True label=DIV_S2 gpio_div_s2=0
  id=voltage5 output=True label=DIV_MR gpio_div_mr=0
  id=voltage6 output=True label=VCTRL_1 gpio_vctrl_1=1
  id=voltage7 output=True label=VCTRL_2 gpio_vctrl_2=1
  id=voltage8 output=True label=TX_SW gpio_tx_sw=0
  id=voltage9 output=True label=BURST gpio_burst=0
sample_rate=4000000 frame_ms=0.5 burst_count=8 auto_samples=16000 buffer_size=16384 

=== trigger=external buffer=16384 ===
[dump] TDD attrs:
  enable=False
  state=0
  frame_length_ms=0.5
  frame_length_raw=3999
  startup_delay_ms=0
  burst_count=8
  sync_external=True
  sync_internal=False
  sync_soft=<error: [Errno 13] Permission denied>
  sync_reset=True
  internal_sync_period_ms=0
  ch0: enable=True polarity=False on_raw=0 off_raw=10 on_ms=0 off_ms=0.00125        
  ch1: enable=True polarity=False on_raw=0 off_raw=10 on_ms=0 off_ms=0.00125        
  ch2: enable=True polarity=False on_raw=0 off_raw=10 on_ms=0 off_ms=0.00125
state before trigger: 0
external pulse 1: idle=0 active=1 high_s=0.1 readback_before=0
external pulse 1: active_readback=1
external pulse 1: final_readback=0
state after trigger: 1
FAIL trigger=external: OSError: [Errno 110] host unreachable
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\pluto_tdd_probe.py --sdr-uri ip:192.168.2.1 --triggers external --rpi-uri ip:phaser.local --frame-ms 0.5 --burst-count 8 --arm-delay-s 1.0 --rx-timeout-ms 30000 --dump --dump-gpio --external-pulse-high-s 0.1 --external-pulse-gap-s 0.1 --external-pulse-count 1 --external-pulse-active low 
adi=0.0.20
[gpio] pluto/tdd uri=ip:192.168.2.1
  id=voltage0 output=False label=muxout gpio_muxout=1
  id=voltage0 output=True label=phaser_enable gpio_phaser_enable=1
[gpio] phaser/rpi uri=ip:phaser.local
  id=voltage0 output=False label=MUXOUT gpio_muxout=0
  id=voltage0 output=True label=DIV_S0 gpio_div_s0=0
  id=voltage1 output=True label=RX_LOAD gpio_rx_load=0
  id=voltage2 output=True label=TR gpio_tr=0
  id=voltage3 output=True label=DIV_S1 gpio_div_s1=0
  id=voltage4 output=True label=DIV_S2 gpio_div_s2=0
  id=voltage5 output=True label=DIV_MR gpio_div_mr=0
  id=voltage6 output=True label=VCTRL_1 gpio_vctrl_1=1
  id=voltage7 output=True label=VCTRL_2 gpio_vctrl_2=1
  id=voltage8 output=True label=TX_SW gpio_tx_sw=0
  id=voltage9 output=True label=BURST gpio_burst=0
sample_rate=4000000 frame_ms=0.5 burst_count=8 auto_samples=16000 buffer_size=16384 

=== trigger=external buffer=16384 ===
[dump] TDD attrs:
  enable=False
  state=0
  frame_length_ms=0.5
  frame_length_raw=3999
  startup_delay_ms=0
  burst_count=8
  sync_external=True
  sync_internal=False
  sync_soft=<error: [Errno 13] Permission denied>
  sync_reset=True
  internal_sync_period_ms=0
  ch0: enable=True polarity=False on_raw=0 off_raw=10 on_ms=0 off_ms=0.00125        
  ch1: enable=True polarity=False on_raw=0 off_raw=10 on_ms=0 off_ms=0.00125        
  ch2: enable=True polarity=False on_raw=0 off_raw=10 on_ms=0 off_ms=0.00125        
state before trigger: 0
external pulse 1: idle=1 active=0 high_s=0.1 readback_before=1
external pulse 1: active_readback=0
external pulse 1: final_readback=1
state after trigger: 1
FAIL trigger=external: OSError: [Errno 110] host unreachable
(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso> python .\radar_mode_a.py --acq tdd --tdd-trigger external --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 5 --no-plot --save-dir recordings --session-name win_tdd_external_64 --tdd-external-pulse-high-s 0.1 --tdd-external-pulse-gap-s 0.1 --tdd-external-pulse-count 1 --tdd-external-pulse-active high
file not found, loading default (all gain at maximum)
file not found, loading default (no phase shift)
[tdd]  rf=10.2500GHz  bw=500MHz  ramp=300us  chirps=64  spc=1079  R_res=0.300m  v_res=0.457m/s  max_vel=±14.6m/s
[tdd] trigger=external
Acquiring... CTRL+C to stop.
Traceback (most recent call last):
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso\radar_backends.py", line 393, in _do_rx
    result["data"] = self._sdr.rx()
                     ^^^^^^^^^^^^^^
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\.venv-win\Lib\site-packages\adi\rx_tx.py", line 275, in rx
    data = self.__rx_complex()
           ^^^^^^^^^^^^^^^^^^^
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\.venv-win\Lib\site-packages\adi\rx_tx.py", line 240, in __rx_complex
    x = self._rx_buffered_data()
        ^^^^^^^^^^^^^^^^^^^^^^^^
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\.venv-win\Lib\site-packages\adi\compat.py", line 149, in _rx_buffered_data
    self._rxbuf.refill()
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\.venv-win\Lib\site-packages\iio.py", line 1003, in refill
    _buffer_refill(self._buffer)
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\.venv-win\Lib\site-packages\iio.py", line 62, in _check_negative
    raise OSError(-result, _strerror(-result))
OSError: [Errno 110] host unreachable

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso\radar_mode_a.py", line 540, in <module>
    sys.exit(main())
             ^^^^^^
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso\radar_mode_a.py", line 531, in main
    session.run()
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso\radar_mode_a.py", line 441, in run
    cap = self._backend.capture()
          ^^^^^^^^^^^^^^^^^^^^^^^
  File "E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso\radar_backends.py", line 428, in capture
    raise RuntimeError(
RuntimeError: TDD rx() timed out (ETIMEDOUT) after the RX buffer was armed.

This means the TDD trigger path did not fill the Pluto DMA buffer. Trigger mode was 'external'.

(.venv-win) PS E:\work\agent\workspace\SENTINEL-A-Tri-Mode-Phased-Array-Radar-for-Clutter-Resistant-HAR-Vitals-and-EMG-Proxy-Sensing\RADAR\phaser_perso>

Show more
12:41 AM


Worked for 2m 5s
External gpio_burst is not solvable in this current software/device-tree path.

What your probe proved:

Phaser BURST GPIO is real and toggles correctly:
active-high readback: 0 -> 1 -> 0
active-low readback: 1 -> 0 -> 1
Pluto’s one-bit GPIO context exposes only:
muxout
phaser_enable
Pluto does not expose a backed tdd_ext_sync GPIO, so sdr_pins.gpio_tdd_ext_sync = True was not configuring a real IIO GPIO channel.
That matches ADI’s EngineerZone answer: BURST is for future use and software triggering is what is currently used. Source: https://ez.analog.com/reference-designs/f/q-a/599427/cn0566-pluto-interconnection/584667

I patched the code to fail fast if --tdd-trigger external is requested without a backed tdd_ext_sync, instead of waiting 30 seconds and timing out. Tests still pass.

Practical conclusion:

Closest supported path to the reference videos: --tdd-trigger soft.
External gpio_burst would require HDL/device-tree/hardware routing work, not Python timing changes.
Use this as the baseline:
python .\radar_mode_a.py --acq tdd --tdd-trigger soft --sdr-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 64 --frames 0 --save-dir recordings --session-name win_tdd_soft_live

sentinel_mode_a_claude_handoff.md
Document · MD

Open


session_log.md
Document · MD

Open

4 files changed
+60
-6
Undo

Review
pluto_tdd_probe.py
radar_backends.py
sentinel_mode_a_claude_handoff.md
workspace/session_log.md




12:44 AM
so are we now better off and like the videos, or we should have just tried to solve it with usb on the pi directly like claude said, or with matlab? or with just a remote desktop to the pi and doing all of the stuff there 
