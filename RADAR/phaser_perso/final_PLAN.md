# Reference-First SENTINEL Build Plan (TDD Included)

## Summary
- Build SENTINEL from the verified CN0566 reference chain upward.
- Keep both acquisition paths in scope:
  - Golden-reference TDD path from `reference/Range_Doppler_Plot.py`
  - Network-safe no-TDD path from `Range_Doppler_NoTDD.py`
- Do not start from the current `sentinel_mode_a.py`; rebuild `Mode A` from smaller proven pieces.
- Make TDD a first-class validation track, but keep it isolated from shared DSP so a transport issue does not stall the whole project.

## Key Architecture
- Preserve `reference/` as read-only golden reference.
- Split the system into two acquisition backends with one shared processing stack.
- Backend contract:
  - Input: current radar settings
  - Output: `complex64` chirp matrix shaped `[num_chirps, samples_per_chirp]`
- Backends:
  - `tdd_burst`: derived as directly as possible from `reference/Range_Doppler_Plot.py`
  - `continuous_sawtooth`: derived from `Range_Doppler_NoTDD.py`
- Shared DSP stack:
  - chirp matrix -> optional MTI -> 2D FFT -> range-Doppler -> range profile -> optional CFAR -> selected range gate -> micro-Doppler history -> recorder/export
- Rebuild `sentinel_mode_a.py` only after both the backend contract and shared DSP are stable.

## Reference Order
- `workspace/radar/phaser_perso/reference/FMCW_RADAR_Waterfall.py`: first live hardware sanity baseline.
- `workspace/radar/phaser_perso/reference/Range_Doppler_Plot.py`: golden TDD drone-demo reference.
- `workspace/radar/phaser_perso/reference/Range_Doppler_Processing.py`: golden offline processing reference.
- `workspace/radar/phaser_perso/Range_Doppler_NoTDD.py`: proven non-TDD acquisition fallback.
- `workspace/radar/phaser_perso/reference/CFAR_RADAR_Waterfall.py`: CFAR logic reference only.
- `workspace/radar/phaser_perso/CW_IF_Scope.py`: later `Mode B` foundation.
- `workspace/radar/team_sentinel/Team SENTINEL - AESS RADAR competition.pdf` and `workspace/radar/cn0566.pdf`: target capability and hardware constraints.

## Implementation Changes
- Stage 0: clone the minimal live FMCW waterfall path and verify repeated bring-up, motion response, and clean shutdown on the current WSL -> Pi -> Pluto path.
- Stage 1: build an offline range-Doppler replay tool using the ADI config schema and verify it reproduces the expected behavior from saved captures.
- Stage 2: implement the shared DSP stack as a backend-neutral module that accepts only the chirp matrix contract.
- Stage 3: implement `tdd_burst` by staying as close as possible to `reference/Range_Doppler_Plot.py`.
- Allowed TDD edits:
  - connection URIs
  - timeout handling
  - environment-specific sync and buffer setup
  - instrumentation/logging for capture diagnostics
- Not allowed in the TDD track:
  - changing the DSP shape
  - redesigning the chirp timing model
  - mixing in no-TDD segmentation logic
- Stage 4: implement `continuous_sawtooth` as the second acquisition backend using the existing proven segmentation model.
- Stage 5: add minimum SENTINEL `Mode A` features on top of the shared DSP:
  - MTI toggle
  - manual/auto range gate
  - micro-Doppler history
  - save raw bursts and config
  - placeholder labels file
  - steering control
- Stage 6: compose the stable pieces into a new `sentinel_mode_a.py` with an explicit acquisition-mode selector:
  - `--acq tdd`
  - `--acq continuous`
- Stage 7: only after `Mode A` is stable, start separate tracks for `Mode B` CW, `Mode C` reflectometry, scheduler, adaptive nulling, and virtual-array work.

## Decisions For Claude
- Try the golden TDD path first because it is the strongest demo reference and the closest to the drone videos.
- If TDD fails on the current transport, treat that as an acquisition/backend issue, not a reason to rewrite the DSP.
- Keep the no-TDD path implemented and working in parallel as the guaranteed progress path for SENTINEL data collection.
- Shared saved-config schema must remain compatible with `Range_Doppler_Processing.py`: `[sample_rate, signal_freq, output_freq, num_chirps, chirp_BW, ramp_time_s, frame_length_ms]`.
- Default tuning ladder:
  - `500 MHz / 500 us / 64 chirps`
  - `500 MHz / 500 us / 128 chirps`
  - `200 MHz / 500 us / 128 chirps`
  - `200 MHz / 200 us / 128 chirps`
  - `200 MHz / 200 us / 256 chirps` only after stability is proven

## Test Plan
- Waterfall sanity test: GUI launches, stays responsive, and detects motion across 10 clean restarts.
- Offline replay test: same saved burst file yields repeatable range-Doppler output with fixed dimensions.
- TDD backend test: capture returns a full chirp matrix with no silent truncation and matches the shared DSP interface.
- Continuous backend test: capture returns a full chirp matrix with explicit padding/truncation handling and no shape errors.
- Backend parity test: the same scene processed through both backends shows the same qualitative target structure.
- Save/replay test: recorded sessions reopen offline without code changes.
- `Mode A` acceptance test: one human-motion session produces raw bursts, range-Doppler frames, and micro-Doppler history from a single run.

## Assumptions And Defaults
- TDD is in scope.
- No-TDD remains in scope as a supported backend, not just a temporary hack.
- The immediate delivery target is a reliable FMCW `Mode A` foundation for tracking, micro-Doppler logging, and dataset generation.
- The current `sentinel_mode_a.py` is reference material only; the new implementation should be rebuilt from the validated backend-plus-DSP split.
