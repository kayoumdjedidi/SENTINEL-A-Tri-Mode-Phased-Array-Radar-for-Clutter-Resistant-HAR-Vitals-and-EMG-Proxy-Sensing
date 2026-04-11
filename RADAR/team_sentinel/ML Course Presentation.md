Deep-Learning-Based Continuous Multi-Subject
Radar HAR Under Clutter
TEL-358 Course Project
Realized by:
Kayoum DJEDIDI
Datasets &
Preprocessing
02 04
TABLE OF
```
CONTENTS:
```
01
Introduction
01
General context
Motivation
Background
System
Architecture
and
Implementation
03
Workflow
Results
Results &
Perspectives,
Limitations &
future work
Introduction
C H A P T E R 1
02
01: Introduction
03
THE DRIVING FORCE: THE 5G/6G VISION
```
Source: ITU approves a new 6G vision | Communications Today
```
Integrated AI and
communication
Hyper Reliable
and Low-Latency
Communication
Ubiquitous
Connectvivity
Massive
Communication
Integrated Sensing
and Communication
Immersive
Communication
USAGE SCENARIOS OF IMT-2023
01: Introduction
04
WHY THIS PROJECT?
```
SENTINEL: A Tri-Mode Phased-Array Radar for
```
Clutter-Resistant HAR,Vitals, and EMG-Proxy
Sensing, built on the CN0566 kit from ADI
We are Finalists in the IEEE AESS Radar
Challenge 2026 with our project proposal:
```
Mode A: FMCW: target detection/track/AoA/posture/falls – Linear chirps, B≈200 MHz (≈0.75 m resolution), 200 μs ramps @5 kHz.
```
```
Mode B: CW: cardio-respiratory via chest wall micro-Doppler – Single-tone CW, long coherent integration, update 0.3–2 Hz (RR), 1–4 Hz (HR).EMG.
```
```
Mode C: reflectometry: muscle proxies (EMG/water shifts). – Spectral energy & centroid features → “myo-index”.
```
```
Scheduler: interleave FMCW bursts with CW dwells (ADF4159 markers); guard times avoid self-interference; common time base for fusion.
```
01: Introduction
05
```
Source: ieee-dataport: DIAT-μRadHAR: Radar micro-
```
Doppler Signature dataset for Human Suspicious Activity
Recognition
```
HUMAN ACTIVITY RECOGNITION (HAR) IN 1 MINUTE
```
```
HAR = automatically recognizing what a person
```
```
is doing (walk, sit, fall…) from sensor data
```
Radar HAR:
privacy‑preserving + works in
dark/through clothing but needs
harder signal processing + prone
to clutter
Camera HAR:
high detail but with privacy +
lighting + occlusion issues
```
Wearable HAR (IMU):
```
accurate but needs compliance
```
(bulky, must wear/charge)
```
```
Source: Data Quality and Reliability Assessment of
```
Wearable EMG and IMU Sensor for Construction Activity
```
Source: Multi-Camera-Based Human Activity Recognition
```
for Human–Robot Collaboration in Construction
01: Introduction
06
Radar HAR:
privacy‑preserving + works in
dark/through clothing but needs
harder signal processing + prone
to clutter
This project focuses on radar HAR using ML
that can run on small devices
```
Source:Vid2Doppler: Synthesizing Doppler Radar Data from Videos for Privacy-Preserving Activity Recognition
```
MODE A
```
(FMCW)
```
SENTINEL'S TRI‑MODE RADAR SENSING + SCHEDULER
KEY IDEA: ML MODEL HANDLES HAR/TRACKING
EFFICIENTLY SO SIGNAL PROCESSING BUDGET STAYS
AVAILABLE FOR VITALS + REFLECTOMETRY.
01: Introduction
07
MODE B
```
(CW)
```
MODE C
```
(REFLECTOMETRY)
```
detection, tracking, AoA,
posture/falls
```
Linear chirps, B≈200 MHz (0.75
```
```
m), 200 μs ramps @ ~5 kHz
```
Tracks subjects, range‑Doppler
and fall cues
respiration + heartbeat
```
(micro‑Doppler)
```
Long coherent integration
```
RR: ~0.3–2 Hz, HR: ~1–4 Hz”
```
```
reflectometry: muscle proxies
```
```
(EMG/water shifts)
```
Spectral energy + centroid →
‘myo‑index’
TAKEAWAYS FROM SOTA & LITERATURE REVIEWS
Radar enables contactless HAR + vitals using micro‑Doppler/time‑frequency
signatures.
What works:
Micro‑Doppler spectrograms enable high HAR accuracy in controlled settings.
CW micro‑Doppler can recover respiration/heartbeat via tiny chest motion.
```
Deep learning (CNN/RNN/Transformers) learns richer features than hand‑crafted
```
methods.
```
Gaps:
```
```
Cross‑environment robustness is weak (models degrade across
```
```
rooms/clutter/sensor placement).
```
```
Most datasets are limited (often single‑subject, clean lab conditions).
```
```
Edge constraints matter (need small models for always‑on / multi‑sensor
```
```
deployments).
```
Around 14 academic sources & papers, with 10+ from 2020
01: Introduction
08
Why it matters:
Competitive accuracy across datasets with very small parameter counts.
```
Designed around micro‑Doppler structure (not a generic vision model).
```
I aim to reproduce this pipeline, then extend toward hardware/domain shift.
KEY INSIGHT: EFFICIENT SEQUENCE MODELING FOR
MICRO‑DOPPLER HAR
01: Introduction
09
True doppler effect: Change in frequency of the
sound based on approaching or withdrawal
MICRO‑DOPPLER: TURNING MOTION INTO A
‘TEXTURE’ FOR ML
```
Sources:
```
Radar Micro-Doppler Signature Generation Based on Time-Domain Digital Coding Metasurface
Radar Application: Stacking Multiple Classifiers for Human Walking Detection Using Micro-Doppler
01: Introduction
10
Example spectrogram from Public
```
dataset (benchmark)
```
MICRO‑DOPPLER: TURNING MOTION INTO A
‘TEXTURE’ FOR ML
This is What HAR models learn from.
```
Activity texture (time‑varying
```
```
Doppler)
```
Clear motion ridge /
micro‑Doppler
What my sensor outputs
```
“Strong carrier / leakage at ~100 kHz (yellow line)”
```
“Motion shows up as faint tracks around it
01: Introduction
11
RUNNING MODES A AND B WITH CLASSIC SIGNAL
PROCESSING TECHNIQUES
Locking onto breathing and heartbeat signals of a target
01: Introduction
12
RUNNING MODES A AND B WITH CLASSIC SIGNAL
PROCESSING TECHNIQUES
Setup + simulating heartbeat and breathing with a speaker
01: Introduction
13
Datasets &
Preprocessing
C H A P T E R 2
14
Dataset
Radar type /
scenario
Classes Split sizes Format after prep
DIAT
micro‑Doppler
images
```
(non‑continuous)
```
6
train 2649, val 377,
test 754
datasets/DIAT/diat_
data.h5
CI4R
FMCW activity
spectrograms
```
(non‑continuous)
```
11 train 519, test 134
datasets/CI4R/ci4r_
data.h5
UoG20
continuous
```
sequences (target
```
```
use‑case)
```
6 planned not finalized
02: Datasets & Preprocessing
DATASETS + PREPROCESSING: STANDARDIZING
BENCHMARKS
```
All datasets converted to a consistent HDF5 layout: train/test/(spectrograms, labels) (+ val when available).
```
Common input size: resize to 224×224, normalize per sample.
15
POTENTIALLY BUILDING MY OWN DATASET: CONTINUOUS
SPECTROGRAM CAPTURE
Collecting continuous spectrogram data for labeled dataset creation
02: Datasets & Preprocessing
```
HOW I PREPARED THE DATASETS (SCRIPTS → HDF5)
```
1. Input (raw)
a. DIAT: class folders of JPG spectrogram images
b. CI4R: class folders of PNG spectrogram images
2. Preprocessing (per sample)
a. Resize → 224×224
b. Channel formatting:
i. DIAT → 3 channels (RGB)
ii. CI4R → 1 channel (grayscale)
c. Normalization:
i. DIAT: min–max normalization per image
ii. CI4R: z‑score normalization per image (mean/std)
3. Output (standardized)
a. Save to HDF5: train/(spectrograms,labels), test/(spectrograms,labels) (+ val for
```
DIAT)
```
b. Labels: integer class → one‑hot vector
16
02: Datasets & Preprocessing
17
ENVIRONMENT
Conda env
```
PyTorch + CUDA on laptop GPU (RTX 4060)
```
Single command entry: python main.py
REPRODUCIBILITY
Seeds logged per run
```
Determinism level: soft/hard (cudnn
```
```
benchmark control)
```
Automatic logging: CSV + checkpoints
via weights and biasses
DEV ENVIRONMENT & REPRODUCIBILITY
```
datasets/: preparation scripts + spec.json per dataset (default hyperparams)
```
main.py + project.py: loads args + spec, builds loaders/model, trains/evals
```
models.py + backbones/: swap architectures (ResNet, CNN‑RNNs, RadMamba)
```
modules/: dataloading + train/eval loops + logging utilities
```
Outputs:
```
```
save/<dataset>/classify/*.pt (best checkpoint)
```
```
log/<dataset>/classify/{history,best}/*.csv (metrics per run)
```
CODEBASE STRUCTURE
02: Datasets & Preprocessing
System
Architecture
and
Implementation
C H A P T E R 3
18
```
Model Model family DIAT Test Acc CI4R Test Acc Params (from logs) Notes
```
RadMamba.py
```
(radmamba)
```
CNN front‑end +
tokenization + Mamba/SSM
```
98.81% 86.57% (dim=80)
```
```
249,172 (DIAT) / 250,021
```
```
(CI4R)
```
Good “small+accurate”
baseline
```
ResNet.py (resnet) 2D CNN residual blocks 88.99% — 9,371
```
```
Strong simple baseline;
```
only DIAT logged
```
CnnLstm.py (cnnlstm)
```
CNN feature extractor +
LSTM temporal model
— 82.84% 328,523 CI4R logged
```
CnnGru.py (cnngru)
```
CNN feature extractor +
stacked BiGRU
— 81.34% 497,724 CI4R logged
ConvMambaPyramid.py
```
(conv_mamba_pyr)
```
Conv pyramid + Mamba
```
blocks (heavier)
```
```
99.60% (dim=8) 84.33%
```
```
3,002,614 (DIAT) /
```
```
3,076,947 (CI4R)
```
Higher compute, slightly
better, not worth it.
ConvMambaPyramidLit
e.py
```
(conv_mamba_pyr_lite)
```
Lite conv‑mamba pyramid 98.20% 83.58%
```
21,668 (DIAT dim=8) /
```
```
71,289 (CI4R dim=80)
```
Best tradeoff, smaller
footprint + similar accuracy
to baseline
```
BiLSTM.py (bilstm)
```
Sequence model on
flattened spectrogram
— — —
Implemented, not logged
yet
RadMamba_modif.py
```
(radmamba_modif)
```
Experimental RadMamba
tweaks
— — —
Implemented, not in final
results
```
RadMamba_modif_1.py Experimental variant (v2) — — —
```
Implemented, not in final
results
```
SSM.py Mamba/SSM building block (internal) (internal) — Not a standalone classifier
```
03: System Architecture and Implementation
```
BASELINE MODELS (ARCHITECTURE → ACCURACY → SIZE)
```
19
03: System Architecture and Implementation
```
EXPERIMENT ROADMAP (WHAT I TRIED, IN ORDER)
```
1. Reproduce RadMamba pipeline (DIAT / CI4R / UoG20 target)
2. Data Preprocessing → ingestion → standard HDF5 format
3. Run baselines + log results
4. Try model variants beyond the reference papers
5. Improve on existing base models with my ideas
6. Add tooling for better experimentation and visualization.
7. Hardware capture (phaser) → “my data” → domain shift
20
03: System Architecture and Implementation
```
DATASET ENGINEERING EXPERIMENTS (BIGGEST
```
```
FRICTION + WHAT I BUILT)
```
1. Initial blockers
a. Paths didn’t match dataset layout (CI4R/DIAT/UoG20 expected files
```
missing)
```
b. CI4R raw archive issues (corrupt/incorrect download path/format)
2. Solution: Make training expect one thing
a. Standard output: HDF5 train/test/(spectrograms,labels) (+ val
```
where available)
```
b. Training never reads PNG/BIN/DAT directly → only HDF5
3. Implemented fast path
a. datasets/data_prepare_CI4R_png.py: PNG → resize 224×224 →
normalize → split 80/20 → HDF5
b. datasets/data_prepare_DIAT.py: JPG → resize 224×224 → normalize
→ train/val/test → HDF5
20
03: System Architecture and Implementation
MODEL EXPERIMENTS
1. Baselines (sanity + comparison)
a. ResNet (CNN)
b. CNN‑LSTM / CNN‑GRU / BiLSTM (sequence baselines)
2. Paper baseline (RadMamba)
a. Multiple dims/hidden sizes
b. Logged runs + saved checkpoints
3. My extensions (new models + variants)
a. radmamba_modif and radmamba_modif_1: tried learned pos, stem conv, MLP
```
head, DropPath, SE (optional)
```
b. ConvMambaPyramid / ConvMambaPyramidLite: tried conv pyramid + mamba,
then a ~250k‑param lite version
4. Outcome summary: RadMambaPyramid stayed best on my logged runs; bigger
variants didn’t consistently help → shows baseline is strong + improvements
need targeted robustness/domain‑shift focus.”
21
03: System Architecture and Implementation
CNNGRU MODEL ARCHITECTURE
Input / shape
```
Spectrogram B×C×H×W (time = W, Doppler bins = H)
```
```
CNN front‑end (feature extraction + compression)
```
```
Conv2D C→16 with a tall kernel (10×5) → captures Doppler‑dominant patterns
```
```
(legs/arms micro‑Doppler)
```
```
MaxPool (2×1) → downsamples Doppler axis, keeps time resolution
```
```
Conv2D 16→32 then 32→32 (k=5) → builds higher‑level motion texture
```
features
```
1×1 Conv2D 32→1 → compresses channels to a single feature map (efficient
```
```
for RNN stage)
```
Sequence mapping
Squeeze channel → B×H'×W
Permute to B×W×H' → each time step sees a Doppler‑feature vector
Temporal modeling
Two stacked Bi‑GRU layers
```
GRU1: learns short/medium motion dynamics
```
```
GRU2: refines temporal context
```
```
Bidirectional = uses past + future context (best for offline classification)
```
Head
Flatten sequence output → Linear → num_classes22
03: System Architecture and Implementation
RADMAMBA MODEL ARCHITECTURE
```
Source:
```
```
RadMamba: Efficient Human Activity Recognition through Radar-based Micro-Doppler-Oriented Mamba State-Space Model
```
23
03: System Architecture and Implementation
STATE‑SPACE MODELS & MAMBA: EFFICIENT
SEQUENCE MODELING
```
Source:
```
```
RadMamba: Efficient Human Activity Recognition through Radar-based
```
Micro-Doppler-Oriented Mamba State-Space Model
```
The problem :We need to model time sequences (micro‑Doppler evolves
```
```
over time). → Sequence models must capture both short motion (steps,
```
```
arm swing) and long context (activity phases).
```
What an SSM is: SSM = a dynamical system with a compact hidden state
that updates over time. At each time step: update state → produce
output.
Key point: It’s like an RNN conceptually, but implemented in a way that
can be stable and efficient.
Where Mamba fits
Mamba is a modern, learnable SSM layer. It uses input‑dependent
```
(selective) state updates so the model can focus on important parts of
```
the sequence. Designed to be fast and memory‑efficient for long
sequences.
Why choose Mamba/SSM over GRU/Transformer here?
Compared to Transformers: avoids quadratic attention cost with
sequence length → better for long time windows.
Compared to GRU/LSTM: can be more parallel‑friendly and efficient
while still modeling long context.
For radar micro‑Doppler: strong fit because data is naturally
sequential, and we want edge‑ready compute.24
03: System Architecture and Implementation
RADMAMBA MODEL ARCHITECTURE
25
03: System Architecture and Implementation
RADMAMBA MODEL ARCHITECTURE
Input micro‑Doppler spectrogram B×C×H×W
```
H = Doppler bins, W = time
```
```
Channel Fusion + Downsampling (CNN front‑end)
```
```
Purpose: mix channels (if multi‑channel) and reduce resolution early
```
```
Effect: suppress redundancy + cut compute
```
Outputs a smaller feature map B×C'×H'×W'
```
Doppler‑aligned tokenization (sequence construction)
```
```
Purpose: treat the spectrogram as a sequence of “time tokens” while respecting Doppler structure
```
Common interpretation: tokens step along time, each token summarizes Doppler bins/features
Convolutional token projection
```
Purpose: local smoothing/feature mixing before the sequence model
```
Makes tokens more robust than raw flattening
```
Mamba / State‑Space blocks (core sequence model)
```
```
Purpose: model long‑range temporal dependencies with efficient (linear‑ish) scaling
```
Why it’s good here: captures motion evolution across time windows without heavy attention
Pooling + classifier head
```
Purpose: convert token sequence to a single prediction
```
```
Output: num_classes logits
```
From my observations, the authors took this approach because:
It's Designed for micro‑Doppler: time‑sequence modeling + Doppler‑aware preprocessing
It's more Edge‑friendly: small parameter counts and fast inference compared to large CNN/Transformer baselines”
26
03: System Architecture and Implementation
CONV MAMBA PYRAMID MODEL ARCHITECTURE
27
03: System Architecture and Implementation
I called it "pyramid" because the network builds features across multiple scales: resolution goes down
while semantic abstraction goes up.”
3 "pyramid" architectural stages
a. High‑resolution stage (fine details)
Conv blocks at near‑input resolution
```
Captures fine micro‑Doppler texture (short time patterns, sharp Doppler edges)
```
b. Mid‑resolution stage (motion structures)
```
Downsample (stride/pool) → fewer time/frequency bins
```
Wider channels / richer features
```
Captures activity “motifs” (repeated patterns)
```
c. Low‑resolution stage (global context)
Most compressed representation
Mamba/SSM blocks operate on fewer tokens → model long temporal context efficiently
Final pooling + classifier
Key ideas
```
Early convs = local pattern extraction; later Mamba = longer‑range temporal modeling.
```
Downsampling reduces tokens → makes sequence modeling cheaper at higher levels.
```
Tradeoff: more parameters/compute than RadMamba, potentially more capacity.
```
CONV MAMBA PYRAMID MODEL ARCHITECTURE
28
03: System Architecture and Implementation
CONV MAMBA PYRAMID LITE MODEL ARCHITECTURE
29
03: System Architecture and Implementation
Core idea: Keep the Conv‑pyramid + Mamba concept, but cut width/depth so it’s
edge‑friendly.
Conv pyramid front‑end: progressively down samples and increases feature abstraction
```
(local patterns first, then broader context).
```
```
Fewer / smaller Mamba blocks than the full pyramid model (reduced hidden
```
```
dimensions / fewer stages).
```
```
Light classifier head (pool → linear) for minimal overhead.
```
```
Why it’s lighter (explicit deltas):
```
```
Reduced model width (smaller dim / channels per stage)
```
Reduced number of stages/blocks
More aggressive early down sampling → fewer tokens/features processed later
CONV MAMBA PYRAMID LITE MODEL ARCHITECTURE
Results &
Perspectives,
C H A P T E R 4
30
```
Model Model family DIAT Test Acc CI4R Test Acc Params (from logs) Notes
```
RadMamba.py
```
(radmamba)
```
CNN front‑end +
tokenization + Mamba/SSM
```
98.81% 86.57% (dim=80)
```
```
249,172 (DIAT) / 250,021
```
```
(CI4R)
```
Good “small+accurate”
baseline
```
ResNet.py (resnet) 2D CNN residual blocks 88.99% — 9,371
```
```
Strong simple baseline;
```
only DIAT logged
```
CnnLstm.py (cnnlstm)
```
CNN feature extractor +
LSTM temporal model
— 82.84% 328,523 CI4R logged
```
CnnGru.py (cnngru)
```
CNN feature extractor +
stacked BiGRU
— 81.34% 497,724 CI4R logged
ConvMambaPyramid.py
```
(conv_mamba_pyr)
```
Conv pyramid + Mamba
```
blocks (heavier)
```
```
99.60% (dim=8) 84.33%
```
```
3,002,614 (DIAT) /
```
```
3,076,947 (CI4R)
```
Higher compute, slightly
better, not worth it.
ConvMambaPyramidLit
e.py
```
(conv_mamba_pyr_lite)
```
Lite conv‑mamba pyramid 98.20% 83.58%
```
21,668 (DIAT dim=8) /
```
```
71,289 (CI4R dim=80)
```
Best tradeoff, smaller
footprint + similar accuracy
to baseline
```
BiLSTM.py (bilstm)
```
Sequence model on
flattened spectrogram
— — —
Implemented, not logged
yet
RadMamba_modif.py
```
(radmamba_modif)
```
Experimental RadMamba
tweaks
— — —
Implemented, not in final
results
```
RadMamba_modif_1.py Experimental variant (v2) — — —
```
Implemented, not in final
results
```
SSM.py Mamba/SSM building block (internal) (internal) — Not a standalone classifier
```
04: Results & Perspectives,
MODELS OVERVIEW
31
MODELS PERFORMANCE COMPARAISON ON DIAT DATASET
04: Results & Perspectives,
32
MODELS PERFORMANCE COMPARAISON ON DIAT DATASET
04: Results & Perspectives,
33
MODELS PERFORMANCE COMPARAISON ON CI4R DATASET
04: Results & Perspectives,
34
MODELS PERFORMANCE COMPARAISON ON CI4R DATASET
04: Results & Perspectives,
35
MODELS PERFORMANCE COMPARAISON ON CI4R DATASET
04: Results & Perspectives,
35
CONV MAMBA PYRAMID LITE MODEL PERFORMANCE
04: Results & Perspectives,
36
MODELS PERFORMANCE COMPARAISON ON CI4R DATASET
04: Results & Perspectives,
38
Domain shift: public datasets ≠ CN0566/phaser
```
spectrogram distribution (leakage/clutter/scale
```
```
differ).
```
```
CI4R scale + split: my CI4R HDF5 is small (train
```
```
519 / test 134) → results are sensitive to
```
split/variant.
Continuous HAR not finalized: UoG20/sequence
evaluation is not fully integrated yet → limited
“timeline” metrics.
```
Metrics focus: mostly accuracy; need
```
segment‑level / macro‑F1 and robustness under
clutter.
Hardware realism: current phaser captures show
```
strong stationary line; micro‑Doppler needs
```
better clutter removal / slow‑time processing for
clean HAR features.
Limitations
Domain adaptation: fine‑tune on a small labeled
```
phaser set (even 5–10 min per activity) + report
```
gain.
```
Robustness training: augmentations (Doppler
```
shift, time warp, SNR drops, moving clutter
```
streaks) + evaluate on corrupted tests.
```
Continuous metrics: add event/segment F1
```
(onset/offset tolerant) + sliding‑window inference
```
```
timeline demo (offline video OK).
```
Data alignment: standardize spectrogram
```
generation (Fs, window, normalization, notch
```
```
around 0‑Doppler) to match training distribution.
```
Edge deployment: export TorchScript/ONNX +
```
measure batch‑1 latency on CPU/Pi (report
```
```
ms/frame + model size).
```
Areas of improvement
39
```
Data & evaluation (robust, realistic)
```
```
Build a labeled CN0566/phaser dataset (single + multi‑subject, cluttered room, multiple
```
```
angles/ranges).
```
```
Add continuous HAR evaluation: sliding window inference + segment/event F1 (not only
```
```
accuracy).
```
Signal processing ↔ ML integration
Improve front‑end: clutter/DC removal, slow‑time decimation, consistent STFT settings,
```
optional range‑gating (FMCW) to isolate subject.
```
Fusion for tri‑mode: use Mode‑A tracking/AoA to steer ROI for Mode‑B vitals and Mode‑C
reflectometry features.
Edge deployment
```
Export best model to TorchScript/ONNX, measure batch‑1 latency, then quantize (INT8)
```
for Raspberry Pi class hardware.
Build an offline “demo pipeline”: phaser PNG stream → preprocessing → on‑device
inference → timeline output.
Future work
Thank you for your
attention
Q&A!