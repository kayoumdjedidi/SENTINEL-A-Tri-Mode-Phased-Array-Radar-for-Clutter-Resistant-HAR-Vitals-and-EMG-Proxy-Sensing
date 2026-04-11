2025-11-19 — Team SENTINEL / RadMamba integration notes

- Project: SENTINEL = tri-mode X-band CN0566 phased-array radar for HAR, vitals, and EMG-proxy sensing (FMCW tracking, CW micro-Doppler, reflectometry myo-index).
- AI scope: micro-Doppler-oriented HAR + risk scoring using tiny Mamba / CNN-Mamba on spectrograms, with edge deployment on Raspberry Pi 5 (optionally AI HAT+) under <50 ms latency and <~1 MB model size.
- Time estimate (AI part only, assuming hardware + PySDR stack already working and one strong full-time engineer): ~3–4 weeks for a minimal working AI MVP; ~6–8 weeks for competition-level RadMamba-style models, ablations, and edge-polished demo.
- Integration plan: use PySDR+CN0566 to log FMCW (and later CW/reflectometry) frames, add a CN0566 dataset + preprocessing pipeline in the AIRHAR/RadMamba repo (STFT micro-Doppler spectrograms, windowing, labels), train RadMamba on this new dataset, then export a tiny model for RPi inference.
- Implementation notes: start with FMCW-only HAR (Mode A) and single-channel spectrograms, then progressively add CW vitals and myo-index as extra channels; keep hardware control (PySDR/pyadi-iio) separate from AIRHAR preprocessing/training code; for live use, wrap exported RadMamba as a callable classifier inside the CN0566 runtime loop.

---

Conversation snapshot (high-level)

- You asked for an estimate of how long the AI part of Team SENTINEL would take; after reading the 2-page SENTINEL PDF, we scoped the AI as: micro-Doppler-based HAR + vitals/myo-index trend detection, with a tiny Mamba-style model running on RPi 5 under tight latency/memory constraints.
- We broke down the AI work into: (1) data & feature pipeline (STFT, denoising, segmentation), (2) baseline models, (3) tiny Mamba / CNN-Mamba sequence model, (4) ablations/metrics, and (5) edge deployment & polishing—leading to ~3–4 weeks for a minimal MVP and ~6–8 weeks for a robust, competition-ready AI stack (full-time, 1 strong engineer, hardware already working).
- You then asked for related open-source code; we identified: (a) AIRHAR / RadMamba repo for radar HAR with Mamba on micro-Doppler, (b) other radar HAR repos (RadHAR, through-the-wall HAR, simulators), and (c) CN0566-specific tooling (pyadi-iio, MATLAB Phaser toolbox, PySDR examples).
- Next, you clarified that you’d base the hardware/control side on the PySDR CN0566 setup you already have running, and asked how to tie that into RadMamba; we outlined a concrete integration path (see below).

Integration with PySDR + CN0566 and RadMamba (detailed)

- Data capture (PySDR side)
  - Use your existing CN0566 + PlutoSDR + PySDR scripts to run Mode A FMCW (and later Modes B/C).
  - For each recording session, log:
    - Raw dechirped frames: e.g., shape [n_chirps, n_rx, n_samples].
    - Ground-truth labels as a timeline: activity segments with start/end indices (walking, standing, falls, etc.), plus optional vitals/myo annotations.
  - Store under something like `recordings/cn0566/subject_X/session_Y/{raw.npy, labels.json}` to keep it clean.

- Offline preprocessing → AIRHAR-compatible dataset
  - Add a `datasets/prepare_cn0566.py` script in the AIRHAR/RadMamba repo that:
    - Loads your logged raw frames.
    - Applies FMCW pipeline: range FFT → Doppler FFT → select subject range gate → generate micro-Doppler STFT spectrograms.
    - Optionally applies spectral denoising (thresholding, low-rank, wavelets) to match the SENTINEL PDF description.
    - Windows the spectrogram into fixed-length samples aligned with your labels (continuous HAR).
    - Writes out tensors and labels into `data/cn0566/{train,val,test}` or an HDF5 file in the same structure used for DIAT/CI4R/UoG2020.
  - For a first version, only use the FMCW mode and a single channel (e.g., summed Rx or one representative channel) to simplify.

- Dataset class in AIRHAR
  - Implement `datasets/cn0566.py` mirroring the existing dataset classes:
    - Reads your processed tensors (e.g., `.npy` or HDF5) and labels.
    - Returns `(tensor, label)` or `(tensor, label, meta)` in the expected shapes [C, F, T].
  - Register `"cn0566"` as a dataset option wherever AIRHAR selects datasets (arguments/configs).
  - Configure model input channels, frequency bins, time length, and number of classes for CN0566.

- Training RadMamba on CN0566
  - Run training using the existing AIRHAR training loop, but with `dataset=cn0566` and a tiny RadMamba variant.
  - Use the same evaluation pattern as for UoG2020:
    - Segmental F1 for continuous HAR.
    - Optional fall ROC-AUC.
    - Latency and parameter count to ensure you stay under RPi5 constraints.
  - Once satisfactory, export the trained model as TorchScript or ONNX for deployment.

- Live inference on RPi5 / host
  - In your PySDR runtime, after each new time window:
    - Run the same preprocessing pipeline used during training (FFT, STFT, denoising, normalization).
    - Build a tensor `[1, C, F, T]` and feed it into the exported RadMamba model (PyTorch, TorchScript, or ONNXRuntime).
    - Decode outputs into activity labels and risk scores; apply temporal smoothing and your rule-based fusion for vitals/myo-index trends.
  - Keep this “AI classifier module” logically separate from the hardware/IIO control code to preserve clarity.

AI effort breakdown (more granular view)

- Week 1–1.5: data logging and preprocessing
  - Stabilize CN0566 logging with labels.
  - Implement STFT + micro-Doppler pipeline and verify spectrogram quality.
  - Build the first CN0566 dataset for AIRHAR.
- Week 1.5–3: baselines and first RadMamba model
  - Implement CN0566 dataset class; get simple CNN/GRU baselines running.
  - Train a tiny RadMamba configuration on the CN0566 data; iterate until you hit usable HAR metrics.
- Week 3–5: ablations and multi-modal extensions
  - Add denoising ablations, nulling vs no-nulling, etc.
  - Start integrating CW vitals and/or myo-index as additional channels or auxiliary outputs.
  - Tune hyperparameters and architecture to respect RPi5 budgets.
- Week 5–8: edge deployment and demo polish
  - Export and deploy the model to RPi5; profile latency and memory.
  - Tighten the live pipeline (buffering, windows, smoothing, risk rules).
  - Build a minimal demo UI (CLI or simple plots) that shows activities, falls, and key metrics in real time.

Open questions / next steps (for future you)

- Decide how rich the first dataset should be (number of subjects, rooms, activities) before you rely heavily on any metrics.
- Choose whether Modes B/C (vitals, myo-index) go into the same model as extra channels/heads or remain separate models fused later.
- Decide if edge inference will live on RPi5 only or if you also want an FPGA or host-accelerated path (reusing your HamGeek E310 experience).
- Once you lock these, you can turn this note into a mini “Team SENTINEL AI v1 spec” and evolve from there.

