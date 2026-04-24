# SENTINEL Mode A Radar Runtime Handoff

Date: 2026-04-24

Purpose: compact technical state for Claude/Codex to continue action without re-debugging solved transport issues.

## Current Decision

Use native Windows Python with Pluto connected directly to the PC over USB.

Known-good live topology:

```text
PC Windows Python owns Pluto RX: usb:1.5.5
Pi controls Phaser/CN0566 over Ethernet: ip:phaser.local
Pluto network control also visible: ip:192.168.2.1 / ip:pluto.local
Mode A working acquisition: continuous
WSL/Pi forwarded iiod path: do not use for live RX
TDD: separate unresolved task
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
- `--frames 0` in `radar_mode_a.py` was patched to mean run-until-stopped.
- TDD code was patched to support split URIs:
  - `--sdr-uri` for AD9361 RX
  - `--tdd-uri` for Pluto GPIO/TDD helpers

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

## Known-Good Live Command

Use this for live display:

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

TDD is not the baseline yet. Continuous is the proven live path.

Known TDD issue:

```text
adi.one_bit_adc_dac(...) / tdd helpers failed against Windows usb: URI.
```

Patch direction already applied:

```text
AD9361 high-rate RX: usb:1.5.5
Pluto GPIO/TDD control: ip:192.168.2.1
Phaser control: ip:phaser.local
```

Smallest internal-trigger TDD test:

```powershell
python .\radar_mode_a.py --acq tdd --sdr-uri usb:1.5.5 --tdd-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 32 --no-tdd-ext-sync --frames 1 --no-plot --save-dir recordings --session-name win_tdd_split_internal_32
```

If that passes, external sync:

```powershell
python .\radar_mode_a.py --acq tdd --sdr-uri usb:1.5.5 --tdd-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --sample-rate 4000000 --ramp-time-us 300 --num-chirps 32 --frames 1 --no-plot --save-dir recordings --session-name win_tdd_split_external_32
```

Reference TDD test:

```powershell
python .\sentinel_modeA_ref_tdd.py --sdr-uri usb:1.5.5 --tdd-uri ip:192.168.2.1 --rpi-uri ip:phaser.local --no-plot --frames 1 --save-data --save-path ref_tdd_split_32.npy --chirps 32
```

TDD failure interpretation:

```text
Fails before rx(): TDD/GPIO/control-device issue.
Reaches rx() then times out: trigger sequencing / burst timing issue.
Saves file: TDD is viable; compare output against continuous baseline.
```

## Next Engineering Priorities

1. Keep continuous Windows USB path as default live runtime.
2. Improve DSP/display on continuous mode:
   - use `--max-range 10`
   - reduce near-range leakage lock
   - reduce diagonal/ripple artifacts from continuous chirp segmentation
3. Test TDD only with split URIs.
4. If TDD remains unstable, do not block Mode A on it; ship continuous baseline first.

