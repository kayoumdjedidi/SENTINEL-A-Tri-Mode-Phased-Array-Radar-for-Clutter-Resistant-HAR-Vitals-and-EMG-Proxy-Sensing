#!/usr/bin/env python3
"""
Pretty model summaries for presentation using torchinfo.

Examples:
  conda run -n AIRHAR python tools/model_summary.py --dataset CI4R --backbones radmamba conv_mamba_pyr_lite
  conda run -n AIRHAR python tools/model_summary.py --dataset DIAT --device cuda --out reports/diat_models.md
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import torch


try:
    from torchinfo import summary as torchinfo_summary
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: torchinfo\n"
        "Install it in your AIRHAR env:\n"
        "  pip install torchinfo\n"
        f"\nOriginal import error: {type(e).__name__}: {e}\n"
    )


@dataclass
class SummaryResult:
    backbone: str
    ok: bool
    input_shape: Optional[List[int]]
    params: Optional[int]
    mult_adds: Optional[int]
    summary_text: Optional[str]
    error: Optional[str]


def load_dataset_spec(dataset: str) -> Dict[str, Any]:
    spec_path = REPO_ROOT / "datasets" / dataset / "spec.json"
    if not spec_path.exists():
        raise FileNotFoundError(f"Missing dataset spec: {spec_path}")
    return json.loads(spec_path.read_text())


def candidate_input_shapes(
    batch: int, channels: int, h: int, w: int
) -> List[Tuple[str, Tuple[int, ...]]]:
    return [
        ("BCHW", (batch, channels, h, w)),
        ("BHW", (batch, h, w)),
        ("BTC_HW", (batch, 1, channels, h, w)),  # time=1
        ("BC_THW", (batch, channels, 1, h, w)),  # time=1
    ]


def build_model(
    backbone: str,
    spec: Dict[str, Any],
    dim: int,
    hidden: int,
    pyr_depth: int,
    pyr_drop_path: float,
    pyr_patch_width: int,
    pyr_stem_out: int,
    device: torch.device,
) -> torch.nn.Module:
    import models

    m = models.CoreModel(
        hidden_size=hidden,
        num_layers=int(spec.get("Classification_num_layers", 1)),
        backbone_type=backbone,
        dim=dim,
        dt_rank=int(spec.get("dt_rank", 0)),
        d_state=int(spec.get("d_state", 4)),
        image_height=int(spec.get("image_height", 224)),
        image_width=int(spec.get("frame_length", spec.get("image_width", 224))),
        num_classes=int(spec.get("num_classes", 11)),
        channels=int(spec.get("channels", 1)),
        dropout=float(spec.get("dropout", 0.0)),
        optional_avg_pool=int(spec.get("optional_avg_pool", 0)),
        channel_confusion_layer=int(spec.get("channel_confusion_layer", 0)),
        channel_confusion_out_channels=int(spec.get("channel_confusion_out_channels", 1)),
        time_downsample_factor=int(spec.get("time_downsample_factor", 1)),
        pyr_depth=pyr_depth,
        pyr_drop_path=pyr_drop_path,
        pyr_patch_width=pyr_patch_width,
        pyr_stem_out=pyr_stem_out,
    ).to(device)
    m.eval()
    return m


def param_count(model: torch.nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())


def try_summary(
    model: torch.nn.Module,
    shapes: Sequence[Tuple[str, Tuple[int, ...]]],
    device: torch.device,
) -> Tuple[Tuple[str, Tuple[int, ...]], Any]:
    last_err: Optional[BaseException] = None
    for _, shape in shapes:
        try:
            x = torch.randn(*shape, device=device, dtype=torch.float32)
            s = torchinfo_summary(
                model,
                input_data=x,
                verbose=0,
                col_names=("input_size", "output_size", "num_params", "mult_adds"),
                row_settings=("var_names", "depth"),
            )
            return (("shape", shape), s)
        except BaseException as e:
            last_err = e
    raise RuntimeError(f"torchinfo summary failed for all shapes. Last error: {last_err}")


def render_markdown(dataset: str, results: List[SummaryResult]) -> str:
    lines: List[str] = []
    lines.append(f"# Model Summary ({dataset})")
    lines.append("")
    for r in results:
        lines.append(f"## {r.backbone}")
        lines.append("")
        if not r.ok:
            lines.append("**Status:** failed")
            lines.append("")
            lines.append("```")
            lines.append((r.error or "unknown error")[:8000])
            lines.append("```")
            lines.append("")
            continue

        lines.append(f"- input: `{r.input_shape}`")
        if r.params is not None:
            lines.append(f"- params: `{r.params:,}`")
        if r.mult_adds is not None:
            lines.append(f"- mult_adds (approx): `{r.mult_adds:,}`")
        lines.append("")
        lines.append("```")
        lines.append((r.summary_text or "").rstrip())
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate torchinfo tables for AIRHAR backbones.")
    parser.add_argument("--dataset", default="CI4R", help="Dataset name (CI4R, DIAT, UoG20).")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"], help="Device for summary run.")
    parser.add_argument("--batch", type=int, default=1, help="Batch size for dummy input.")
    parser.add_argument("--dim", type=int, default=80, help="Model dim for Mamba backbones.")
    parser.add_argument("--hidden", type=int, default=None, help="Hidden size for CNN/RNN backbones.")
    parser.add_argument("--out", default=None, help="Write markdown report to this path.")
    parser.add_argument(
        "--backbones",
        nargs="*",
        default=None,
        help="Subset of backbones to summarize. Default: all backbones wired in models.py.",
    )
    parser.add_argument("--pyr_depth", type=int, default=3, help="conv_mamba_pyr(*) depth.")
    parser.add_argument("--pyr_drop_path", type=float, default=0.1, help="conv_mamba_pyr(*) drop-path rate.")
    parser.add_argument("--pyr_patch_width", type=int, default=2, help="conv_mamba_pyr(*) patch stripe width.")
    parser.add_argument("--pyr_stem_out", type=int, default=32, help="conv_mamba_pyr_lite stem out channels.")
    args = parser.parse_args()

    spec = load_dataset_spec(args.dataset)
    channels = int(spec.get("channels", 1))
    h_img = int(spec.get("image_height", 224))
    w_img = int(spec.get("frame_length", spec.get("image_width", 224)))
    hidden = int(args.hidden if args.hidden is not None else spec.get("Classification_hidden_size", 64))

    if args.device == "cuda" and not torch.cuda.is_available():
        print("CUDA not available, falling back to CPU.")
        device = torch.device("cpu")
    else:
        device = torch.device(args.device)

    default_backbones = [
        "vgg16",
        "resnet",
        "bilstm",
        "cnnlstm",
        "cnngru",
        "radmamba",
        "radmamba_modif",
        "radmamba_modif_1",
        "conv_mamba_pyr",
        "conv_mamba_pyr_lite",
    ]
    backbones = args.backbones if args.backbones else default_backbones

    results: List[SummaryResult] = []
    for backbone in backbones:
        try:
            model = build_model(
                backbone=backbone,
                spec=spec,
                dim=int(args.dim),
                hidden=hidden,
                pyr_depth=int(args.pyr_depth),
                pyr_drop_path=float(args.pyr_drop_path),
                pyr_patch_width=int(args.pyr_patch_width),
                pyr_stem_out=int(args.pyr_stem_out),
                device=device,
            )
            params = param_count(model)
            shapes = candidate_input_shapes(args.batch, channels, h_img, w_img)
            (_, shape), s = try_summary(model, shapes, device=device)

            # torchinfo Summary has .total_mult_adds and .total_params in recent versions.
            mult_adds = getattr(s, "total_mult_adds", None)
            summary_text = str(s)

            results.append(
                SummaryResult(
                    backbone=backbone,
                    ok=True,
                    input_shape=list(shape),
                    params=params,
                    mult_adds=int(mult_adds) if mult_adds is not None else None,
                    summary_text=summary_text,
                    error=None,
                )
            )
        except BaseException:
            results.append(
                SummaryResult(
                    backbone=backbone,
                    ok=False,
                    input_shape=None,
                    params=None,
                    mult_adds=None,
                    summary_text=None,
                    error=traceback.format_exc(limit=80),
                )
            )

    md = render_markdown(args.dataset, results)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md)
        print(f"Wrote {out_path}")
    else:
        print(md)

    # Also drop a machine-readable json next to the markdown, if requested.
    if args.out:
        json_path = Path(args.out).with_suffix(".json")
        json_path.write_text(json.dumps([asdict(r) for r in results], indent=2))
        print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()

