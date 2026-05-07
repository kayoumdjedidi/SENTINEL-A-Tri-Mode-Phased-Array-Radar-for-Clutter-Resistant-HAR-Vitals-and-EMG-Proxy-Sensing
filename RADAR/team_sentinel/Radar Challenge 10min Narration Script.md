# SENTINEL Radar Challenge - Dense 10 Minute Narration Script

Target duration: 9:45-10:00.

Fixed embedded video durations:

- AI / ML video on Slide 4: 1:30
- Full system demo video on Slide 5: 3:00

Pacing:

- Slide 1: 0:55
- Slide 2: 0:55
- Slide 3: 1:30
- Slide 4: 1:45 total, including 1:30 AI video
- Slide 5: 3:05 total, including 3:00 demo video
- Slide 6: 1:30

## Slide 1 - Motivation and Main Claim - 0:55

Continuous monitoring in homes and clinics is still difficult. Cameras give rich information, but they create privacy and lighting problems. Wearables give strong physiological signals, but they require compliance: the person has to wear them, charge them, and tolerate them. Radar is attractive because it is contactless and privacy-preserving, but single-mode radar is usually fragile in cluttered rooms and does not capture the full patient state.

SENTINEL is our answer to that gap. We built a tri-mode X-band phased-array radar stack on the ADI CN0566 Phaser platform. Mode A uses FMCW for target location, motion, and activity cues. Mode B uses CW phase sensing for respiration and heartbeat estimates. Mode C uses CW reflectometry features for a myo-index, which is a proxy for muscle micro-motion trends.

The goal is one radar platform that can combine activity, vitals, and longer-term physiological trends.

## Slide 2 - SOTA and Gap - 0:55

The literature shows that radar can support both HAR and vital sensing. For HAR, the key representation is the micro-Doppler spectrogram: human motion becomes a time-frequency texture that CNNs, RNNs, Transformers, and Mamba-style models can learn from. For vitals, CW radar can detect very small chest-wall motion through received phase modulation, which gives respiration and heartbeat bands after filtering.

The main weakness is robustness. Many public datasets are clean, segmented, and single-subject. Real CN0566 captures have leakage, stationary clutter, different scaling, and strong dependence on sensor placement and room geometry. So our project has two linked parts: build the real tri-mode radar acquisition stack, and prepare an ML pipeline that can later adapt from public datasets to phaser-specific data.

## Slide 3 - Full System Architecture - 1:30

This slide shows the full architecture. At the hardware level, we use the CN0566 Phaser kit with the ADAR1000 beamformer array, the ADF4159 chirp PLL, the X-band antenna array around 10.25 GHz, and the PlutoSDR transceiver, controlled through Python and pyadi-iio with Raspberry Pi and host-side scripts.

The next layer is calibration. We separated three things that are often confused: HB100 frequency reference calibration, per-element gain and phase calibration for the array, and room or target scene calibration for monitoring. This made the experiments more repeatable.

Then the mode controller configures the radar for three sensing paths. In Mode A, TDD soft-triggered FMCW chirps are processed with range FFT and Doppler FFT to produce range-Doppler maps, target range, and motion cues. We also use MTI and clutter suppression because the real phaser captures have strong stationary leakage.

Modes B and C share CW IQ. Mode B unwraps phase and converts it to chest displacement, then estimates respiration around 0.15 to 0.5 Hz and heartbeat around 0.8 to 2.5 Hz. Mode C uses amplitude-envelope and spectral features, such as energy and centroid, to form a myo-index. All outputs are recorded with metadata so they can feed a future fusion and edge-ML layer.

## Slide 4 - AI / ML Pipeline - 1:45 total, AI video 1:30

Start of slide, before or as video begins:

The AI side is focused on turning radar captures into micro-Doppler learning samples. The video shows the spectrogram-generation and dataset-preparation workflow.

While video plays:

Raw or processed radar frames are converted into time-frequency spectrogram windows. For public validation, we prepared DIAT and CI4R into standardized HDF5 datasets with resizing, channel formatting, normalization, and one-hot labels. This gave us a controlled way to compare lightweight architectures before having a large CN0566-specific dataset.

The model direction is small CNN/Mamba-style sequence modeling. The reason is practical: for always-on monitoring, the classifier should be compact enough for edge inference, not just accurate on a workstation. We observed good accuracy on public datasets, but the important limitation is domain shift. Public spectrograms do not look exactly like our phaser spectrograms because hardware leakage, clutter, and scaling differ. So the ML pipeline is best understood as ML-ready: validated on public data, then intended for fine-tuning on labeled SENTINEL recordings.

End of slide:

So the AI contribution is not only the model; it is the pipeline from radar recordings to edge-ready HAR inputs.

## Slide 5 - Full System Demo - 3:05 total, demo video 3:00

As demo video starts:

This is the experimental evidence. The video shows the system bring-up and the three demonstrated sensing modes, plus the combined dashboard.

Calibration segment:

First is calibration and hardware bring-up. We used the HB100 workflow and array calibration to establish a reference and improve repeatability. This matters because with the Phaser platform, physical topology, USB versus Ethernet routing, and IIO context ownership directly affect radar stability.

Mode A segment:

Next is Mode A, the FMCW spatial-awareness mode. Here the radar sends chirp bursts, forms range-Doppler maps, and extracts target range and motion cues. This mode is the one that tells us where the subject is and whether there is activity, posture change, or fall-like motion. In the video, the important part is the range-Doppler response and the tracking/scene-map behavior.

Mode B segment:

Then Mode B shows CW vital sensing. In this mode, we are not primarily ranging; we are using phase sensitivity. Breathing appears as a slow displacement component, and heartbeat appears in a higher frequency band. This is a radar estimate of chest-wall micro-motion, not ECG, so validation against reference sensors is future work.

Mode C segment:

Mode C reuses CW data but focuses on amplitude and spectral behavior. From the envelope spectrum, baseline, energy, and centroid, we estimate a myo-index. This is an RF proxy for muscle micro-motion trends, not clinical EMG.

Full dashboard segment:

Finally, the full-system UI shows how these pieces are intended to come together: Mode A for activity and target context, Mode B for RR and HR trends, Mode C for myo-index, and the scheduler/fusion layer to connect them into one monitoring timeline.

## Slide 6 - Conclusion - 1:30

To conclude, SENTINEL demonstrates a working tri-mode CN0566 radar stack. We implemented Mode A FMCW range-Doppler sensing for target and activity cues, Mode B CW phase sensing for respiration and heartbeat estimates, and Mode C CW reflectometry features for myo-index trends. We also built the calibration workflow, recording pipeline, dashboards, and dataset path needed to make the system usable for future ML fusion.

The main lesson is that the challenge is not only classification. The hard part is aligning the RF hardware, calibration, clutter processing, data recording, DSP, and ML pipeline. That is where the technical depth of the project sits.

The current limitations are clear. Public datasets do not match CN0566 captures; CI4R is small; continuous HAR metrics are not fully integrated; and the hardware captures still show strong stationary leakage. Also, heartbeat is chest-wall micro-motion rather than ECG, and the myo-index is an RF proxy rather than clinical EMG.

The next steps are targeted and measurable: collect a labeled phaser dataset, improve clutter removal and slow-time processing, fine-tune lightweight CNN/Mamba models, report macro-F1 and event-level F1, export TorchScript or ONNX for Raspberry Pi latency, and validate vitals and myo-index against reference sensors. SENTINEL is therefore a working prototype and a foundation for privacy-preserving multi-modal radar health monitoring.

