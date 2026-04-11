#!/usr/bin/env python3
"""Reference-first baseline launcher.

This intentionally delegates to the existing minimal waterfall script so the
known-good live bring-up remains a separate, stable checkpoint from the newer
SENTINEL runtime.
"""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> int:
    baseline = Path(__file__).resolve().with_name("RADAR_FFT_Waterfall_baseline.py")
    runpy.run_path(str(baseline), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
