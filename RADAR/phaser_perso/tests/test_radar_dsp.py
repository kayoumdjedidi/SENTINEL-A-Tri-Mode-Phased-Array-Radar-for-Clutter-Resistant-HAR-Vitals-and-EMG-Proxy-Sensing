import sys
from pathlib import Path

import numpy as np
import pytest


THIS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = THIS_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


from radar_dsp import range_bin_for_distance, select_range_gate  # noqa: E402
from radar_mode_a import build_arg_parser  # noqa: E402


def test_range_bin_for_distance_picks_nearest_physical_bin():
    range_axis_m = np.array([-2.0, 0.0, 2.0, 4.0, 6.0], dtype=float)
    assert range_bin_for_distance(range_axis_m, 4.7) == 3
    assert range_bin_for_distance(range_axis_m, 5.6) == 4


def test_select_range_gate_supports_meter_based_filters():
    profile = np.array([2.0, 90.0, 10.0, 80.0, 70.0], dtype=np.float32)
    range_axis_m = np.array([-2.0, 2.0, 4.0, 6.0, 8.0], dtype=float)

    assert select_range_gate(
        profile,
        manual_m=6.1,
        range_axis_m=range_axis_m,
    ) == 3

    assert select_range_gate(
        profile,
        min_range_m=5.0,
        range_axis_m=range_axis_m,
    ) == 3


def test_mode_a_parser_accepts_meter_based_range_controls():
    parser = build_arg_parser()

    args = parser.parse_args(["--range-m", "6.5", "--min-range-m", "4.0"])
    assert args.range_m == 6.5
    assert args.min_range_m == 4.0

    with pytest.raises(SystemExit):
        parser.parse_args(["--range-bin", "10", "--range-m", "6.5"])

    with pytest.raises(SystemExit):
        parser.parse_args(["--min-range-bin", "100", "--min-range-m", "4.0"])
