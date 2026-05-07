# SENTINEL Radar Challenge Final Deck Build

Working deck target: 5-6 slides, 16:9, standard Windows fonts, self-contained.

Conference scoring criteria to optimize:

- Technical depth
- Creativity of the problem/solution
- Slide/poster clarity
- Q&A performance

Core message:

> SENTINEL is a tri-mode X-band phased-array radar prototype that combines FMCW activity/target sensing, CW respiration/heartbeat extraction, and CW reflectometry myo-index sensing to move beyond single-mode radar demos toward privacy-preserving continuous health monitoring in cluttered indoor environments.

## Slide 1 - Hook / System Claim

Title:

> SENTINEL: Tri-Mode X-Band Phased-Array Radar for Privacy-Preserving Human Monitoring

Subtitle:

> Activity tracking + respiration/heartbeat + muscle micro-motion proxy on the ADI CN0566 platform

Layout:

- Dark navy/black background.
- Left 55%: one large hero visual.
- Right 45%: three stacked mode cards.
- Bottom strip: team name, IEEE AESS Radar Challenge 2026, institution/team logos if available.

Hero visual options, strongest first:

1. A still from the final demo video showing the hardware setup and GUI together.
2. A clean photo of the Phaser/CN0566 + Pluto + laptop.
3. A 2x2 collage: hardware, Mode A range-Doppler, Mode B vitals, Mode C myo-index.

Three mode cards:

- Mode A - FMCW: Range-Doppler target/activity sensing
- Mode B - CW: Respiration + heartbeat from chest-wall phase
- Mode C - CW Reflectometry: Myo-index from amplitude/spectral micro-motion

One-line value claim:

> One radar, three sensing modes, one privacy-preserving monitoring stack.

Speaker note:

> SENTINEL addresses the gap between clean lab radar demos and real patient monitoring. Instead of relying on one radar mode, we built and demonstrated three complementary modes on the CN0566 phased-array platform: FMCW for where the person is and how they move, CW phase sensing for respiration and heartbeat, and CW reflectometry features for muscle micro-motion trends. The goal is not just fall detection; it is continuous, privacy-preserving monitoring that can later fuse activity, vital signs, and longer-term health proxies.

## Slide 2 - Problem / Why Single-Mode Sensing Fails

Title:

> Why Always-On Monitoring Still Breaks in Real Homes

Main message:

> Cameras are intrusive, wearables require compliance, and single-mode radars struggle with clutter, multiple movers, and continuous sequences.

Build:

- Three problem columns: Cameras, Wearables, Single-Mode Radar.
- Bottom: SENTINEL design response.

Exact slide layout:

- Top title band, 10-12% height.
- Center: three large cards across the page.
- Bottom: one horizontal "SENTINEL response" band.

Card 1:

> Cameras
> High detail, but privacy-invasive and sensitive to lighting/occlusion.

Icon/visual:

- Simple camera icon with a privacy/eye-slash symbol.

Card 2:

> Wearables
> Good signals, but require the patient to wear, charge, and tolerate devices.

Icon/visual:

- Watch/patch icon with a small "compliance" warning.

Card 3:

> Single-Mode Radar
> Contactless, but brittle under clutter, multi-person motion, and continuous activity.

Icon/visual:

- Radar waves hitting multiple reflectors / clutter.

Bottom response band:

> SENTINEL response: combine complementary radar evidence - spatial motion (FMCW), vital micro-motion (CW phase), and muscle/activity proxy trends (CW reflectometry).

Add a small scoring-aligned label in the corner:

> Challenge target: robust sensing beyond clean single-person lab demos.

Speaker note:

> The motivation is not simply to replace cameras or wearables with radar. The hard problem is always-on monitoring in rooms where people move, clutter changes, and the system must work continuously without asking the patient to wear anything. A single sensing mode gives incomplete evidence: FMCW gives spatial motion but not stable heartbeat, CW gives excellent phase sensitivity but weak localization, and reflectometry features are useful as trends but not as standalone identity. SENTINEL's core idea is to combine these complementary radar modes into one monitoring stack.

## Slide 3 - Architecture / What We Built

Title:

> System Architecture: Phaser Array + Pluto SDR + Mode Scheduler + DSP/ML-Ready Fusion

Main message:

> We implemented the full RF-to-plot stack: calibration, hardware control, data capture, range-Doppler, phase-vitals, myo-index, recordings, and demo dashboards.

Build:

- Block diagram from hardware to modes to fusion.
- Include calibration files/steps as a small side strip.

Exact slide layout:

- Left 65%: system block diagram.
- Right 35%: "What we implemented" checklist.
- Bottom strip: calibration + recording pipeline.

Block diagram:

> CN0566 Phaser Array + Pluto SDR
> -> RF control / calibration / beam steering
> -> Mode A FMCW bursts
> -> Mode B/C CW windows
> -> DSP feature extraction
> -> Demo dashboard + recordings + ML-ready fusion

Mode branch details:

- Mode A FMCW: TDD chirp bursts -> range FFT -> Doppler FFT -> target/activity cues
- Mode B CW: tone IQ -> phase unwrap -> displacement -> respiration/heartbeat PSD
- Mode C CW: tone IQ -> envelope/spectrum -> myo-index + centroid trends

Right checklist:

> Built end-to-end:
> - CN0566/Pluto control scripts
> - TDD FMCW capture + range-Doppler
> - CW vitals processing
> - Myo-index reflectometry processing
> - HB100 + array calibration workflow
> - Recording/demo dashboards for subjects

Bottom calibration/recording strip:

> Calibration: HB100 frequency reference + gain/phase array files + room/target scene calibration
> Output: saved IQ/metadata recordings, screenshots, and demo videos for each mode

Speaker note:

> This slide is the technical spine of the project. We did not only train a model or show a single radar plot. We built the RF control, calibration, acquisition, signal processing, and visualization layers around the CN0566 phased-array and Pluto SDR. Mode A uses FMCW chirp bursts to produce range-Doppler and activity cues. Modes B and C share CW IQ data: Mode B extracts phase displacement for respiration and heartbeat, while Mode C uses amplitude-spectrum features as a myo-index proxy. The architecture is also prepared for ML fusion: the recordings and metadata can become the dataset for a small edge classifier.

## Slide 4 - Mode A Results: FMCW Activity / Target Sensing

Title:

> Mode A: FMCW Range-Doppler for Target Localization and Activity Cues

Main message:

> Mode A acts as the spatial awareness layer: detect, range, steer/scan, and observe movement signatures.

Build:

- Best real Mode A screenshot/video still.
- 3 annotated callouts: target range, Doppler/motion, clutter suppression/beam steering.

## Slide 5 - Mode B/C Results: Vitals + Myo Proxy

Title:

> Modes B/C: CW Micro-Motion for Vitals and Muscle-Activity Proxy

Main message:

> CW sensing extracts slow chest-wall phase displacement for respiration/heartbeat and amplitude-spectrum features for myo-index trends.

Build:

- Left: Mode B phase displacement + PSD screenshot.
- Right: Mode C myo-index + amplitude spectrum screenshot.
- Bottom: what is measured vs inferred.

## Slide 6 - System-Level Contribution / Why It Matters

Title:

> From Radar Demo to Multi-Modal Health Sentinel

Main message:

> The contribution is not a single plot; it is an integrated tri-mode sensing stack that creates richer evidence for robust, privacy-preserving monitoring.

Build:

- 4 contribution tiles: tri-mode sensing, clutter-aware tracking, calibration/recording pipeline, ML-ready fusion.
- Final line: current limits + next step.
