#!/bin/bash
CI4R_BASE_DIR="${CI4R_BASE_DIR:-/home/kayoum/projects/AIRHAR/datasets/CI4R}"
DIAT_BASE_DIR="${DIAT_BASE_DIR:-/home/kayoum/projects/AIRHAR/datasets/DIAT}"
GLASGOW_BASE_DIR="${GLASGOW_BASE_DIR:-/home/kayoum/projects/AIRHAR/datasets/UoG20}"

echo "Preparing the CI4R dataset..."
if [ -d "$CI4R_BASE_DIR" ]; then
  python3 datasets/data_prepare_CI4R.py --path "$CI4R_BASE_DIR"
else
  echo "  Skip CI4R: directory not found -> $CI4R_BASE_DIR"
fi

echo "Preparing the DIAT dataset..."
if [ -d "$DIAT_BASE_DIR" ]; then
  python3 datasets/data_prepare_DIAT.py --path "$DIAT_BASE_DIR"
else
  echo "  Skip DIAT: directory not found -> $DIAT_BASE_DIR"
fi

echo "Preparing the Glasgow dataset..."
if [ -f "$GLASGOW_BASE_DIR/Spectrograms.mat" ] && [ -f "$GLASGOW_BASE_DIR/Label.mat" ]; then
  python3 datasets/data_prepare_UoG20.py --path "$GLASGOW_BASE_DIR"
else
  echo "  Skip UoG20: missing Spectrograms.mat or Label.mat in $GLASGOW_BASE_DIR"
fi

echo "Done. Check messages above for any skipped datasets."
