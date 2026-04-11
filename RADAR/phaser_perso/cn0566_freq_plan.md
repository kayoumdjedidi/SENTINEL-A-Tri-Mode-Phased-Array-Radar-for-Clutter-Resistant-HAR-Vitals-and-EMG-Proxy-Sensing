# CN0566 / Phaser Frequency Plan Notes (2025-11-24)

Sources: Analog Devices CN0566 circuit note (latest crawl 2025-11-22) citeturn0search0 and ADAR1000 product page (2025-11-21) citeturn0search4.

## Hardware ranges
- ADAR1000 beamformers: 8–16 GHz RF paths. CN0566 antenna and filtering optimized for **10.0–10.5 GHz** (−3 dB antenna BW ≈ 9.9–10.8 GHz) citeturn0search4.
- On-board synthesizer (ADF4159 + HMC735 VCO): **10.5–12.7 GHz LO range**. Default radar examples set LO ≈ 12.2 GHz to target 10.0 GHz RF with 2.2 GHz IF. citeturn0search0turn0search5
- Mixers (LTC5548) run high-side: RF = LO − IF (IF fixed at ~2.2 GHz in the Pluto/Phaser labs).
- On-board RF chain filtering: LPF @10.6 GHz after mixer; BPF 9.7–11.95 GHz at PA output. So practical TX/RX RF **~10.0–10.5 GHz** for clean passband citeturn0search5.

## Naming convention warning
- ADI's older waterfall-family examples often name the programmed ADF4159 LO as `output_freq`, even though it is not the final radiated RF center.
- In those scripts, the practical relationship is:
  `radiated_rf ~= pll_lo - (center_freq + signal_freq)`.
- Our newer SENTINEL / range-Doppler code uses `output_freq` to mean the actual radiated RF center and computes:
  `pll_lo = output_freq + center_freq + signal_freq`.
- Do not mix those two conventions in the same script.
