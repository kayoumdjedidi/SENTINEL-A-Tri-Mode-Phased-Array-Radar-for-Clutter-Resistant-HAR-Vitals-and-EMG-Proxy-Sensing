# SENTINEL: A Tri-Mode Phased-Array Radar for HAR, Vitals, and EMG-Proxy Sensing

This repository contains code, datasets, and documentation for the IEEE Radar Challenge 2026 project **SENTINEL**.

## Project Overview
SENTINEL implements a tri-mode X-band phased-array radar system on the ADI CN0566 platform:
- FMCW beamforming for tracking, AoA, posture, and fall detection  
- CW micro-Doppler for respiration and heartbeat monitoring  
- EMG-proxy sensing (muscle micro-motions, hydration trends)  

The system integrates adaptive nulling, virtual arrays, and lightweight on-edge inference to operate robustly in cluttered environments.

## Features
- Tri-mode scheduler (FMCW + CW interleaved)  
- Adaptive beamforming (MVDR/Capon, virtual arrays)  
- Tiny Mamba/SSM models (<1 MB, <50 ms latency on Raspberry Pi 5)  
- Fusion of HAR, vitals, and EMG-proxy into trend-based risk alerts  

## Planed Repository Structure
├── docs/             # Documentation & reports
├── data/             # Sample datasets (HAR, vitals, EMG proxy)
├── scripts/          # Python/MATLAB processing scripts
├── models/           # Tiny Mamba/SSM inference code
├── hardware/         # CN0566 configs, FPGA pipeline (HamGeek E310)
└── README.md         # This file


## Team
- Kayoum Djedidi – PhD Student, INRS Montréal  
- Olivier Tomé – PhD Student, INRS Montréal  
**Mentor:** Prof. Tarek Djerafi, INRS Montréal  

## Timeline
- Nov 2025: Setup and baseline tutorial  
- Jan 2026: Range validation experiments  
- Feb–Apr 2026: Pipeline integration and ablations  
- May 2026: Final demo at IEEE Radar 2026, Phoenix, AZ  
