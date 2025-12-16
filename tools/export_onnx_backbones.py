#!/usr/bin/env python3
"""
Export AIRHAR backbones to ONNX for visualization (e.g., in Netron).

This tries multiple plausible input shapes per backbone because some backbones
expect [B,C,H,W] while others historically used [B,H,W] or even [B,T,C,H,W].

Usage:
  python tools/export_onnx_backbones.py --dataset CI4R
  python tools/export_onnx_backbones.py --dataset DIAT --out_dir onnx_exports --device cpu
  python tools/export_onnx_backbones.py --dataset CI4R --backbones radmamba conv_mamba_pyr_lite
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import torch


@dataclass
class ExportResult:
    backbone: str
    ok: bool
    onnx_path: Optional[str]
    input_shape: Optional[List[int]]
    params: Optional[int]
    file_bytes: Optional[int]
    error: Optional[str]


def _load_dataset_spec(dataset: str) -> Dict[str, Any]:
    spec_path = REPO_ROOT / "datasets" / dataset / "spec.json"
    if not spec_path.exists():
        raise FileNotFoundError(f"Missing dataset spec: {spec_path}")
    return json.loads(spec_path.read_text())


def _candidate_input_shapes(
    batch: int, channels: int, h: int, w: int
) -> List[Tuple[str, Tuple[int, ...]]]:
    return [
        ("BCHW", (batch, channels, h, w)),
        ("BHW", (batch, h, w)),
        ("BTC_HW", (batch, 1, channels, h, w)),  # (time=1)
        ("BC_THW", (batch, channels, 1, h, w)),  # (time=1) alternative layout
    ]


def _build_model(
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


def _param_count(model: torch.nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())


def _try_forward_shapes(
    model: torch.nn.Module,
    shapes: Sequence[Tuple[str, Tuple[int, ...]]],
    device: torch.device,
) -> Tuple[Tuple[str, Tuple[int, ...]], torch.Tensor]:
    last_err: Optional[BaseException] = None
    for name, shape in shapes:
        try:
            x = torch.randn(*shape, device=device, dtype=torch.float32)
            with torch.no_grad():
                y = model(x)
            if not isinstance(y, torch.Tensor):
                raise RuntimeError(f"Model returned non-tensor output type: {type(y)}")
            return ((name, shape), x)
        except BaseException as e:
            last_err = e
            continue
    raise RuntimeError(f"No compatible input shape found. Last error: {last_err}")


def _dynamic_axes_for(shape: Tuple[int, ...]) -> Dict[str, Dict[int, str]]:
    # Always make batch dimension dynamic.
    return {"input": {0: "batch"}, "logits": {0: "batch"}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export AIRHAR backbones to ONNX.")
    parser.add_argument("--dataset", default="CI4R", help="Dataset name (e.g., CI4R, DIAT, UoG20).")
    parser.add_argument("--out_dir", default="onnx_exports", help="Output directory for ONNX files.")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"], help="Device for tracing/export.")
    parser.add_argument("--batch", type=int, default=1, help="Batch size for dummy input.")
    parser.add_argument("--dim", type=int, default=80, help="Model dim (used by Mamba-based backbones).")
    parser.add_argument(
        "--hidden",
        type=int,
        default=None,
        help="Hidden size for CNN/RNN backbones. Defaults to dataset spec Classification_hidden_size or 64.",
    )
    parser.add_argument("--opset", type=int, default=17, help="ONNX opset version.")
    parser.add_argument(
        "--backbones",
        nargs="*",
        default=None,
        help="Subset of backbones to export. Default: all backbones wired in models.py.",
    )
    parser.add_argument("--pyr_depth", type=int, default=3, help="conv_mamba_pyr(*) depth.")
    parser.add_argument("--pyr_drop_path", type=float, default=0.1, help="conv_mamba_pyr(*) drop-path rate.")
    parser.add_argument("--pyr_patch_width", type=int, default=2, help="conv_mamba_pyr(*) patch stripe width.")
    parser.add_argument("--pyr_stem_out", type=int, default=32, help="conv_mamba_pyr_lite stem out channels.")

    args = parser.parse_args()

    try:
        import onnx  # noqa: F401
    except Exception:
        raise SystemExit(
            "Missing dependency: `onnx`.\n"
            "Install it in your AIRHAR env, e.g.:\n"
            "  pip install onnx\n"
            "Optional (for the new exporter):\n"
            "  pip install onnxscript\n"
        )

    spec = _load_dataset_spec(args.dataset)
    h = int(args.hidden if args.hidden is not None else spec.get("Classification_hidden_size", 64))
    h_img = int(spec.get("image_height", 224))
    w_img = int(spec.get("frame_length", spec.get("image_width", 224)))
    channels = int(spec.get("channels", 1))
    device = torch.device(args.device if args.device == "cpu" or torch.cuda.is_available() else "cpu")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Backbones wired in models.py (kept explicit so we don't export helper modules).
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

    results: List[ExportResult] = []
    for backbone in backbones:
        onnx_path: Optional[str] = None
        input_shape: Optional[List[int]] = None
        params: Optional[int] = None
        file_bytes: Optional[int] = None
        error: Optional[str] = None

        try:
            model = _build_model(
                backbone=backbone,
                spec=spec,
                dim=int(args.dim),
                hidden=h,
                pyr_depth=int(args.pyr_depth),
                pyr_drop_path=float(args.pyr_drop_path),
                pyr_patch_width=int(args.pyr_patch_width),
                pyr_stem_out=int(args.pyr_stem_out),
                device=device,
            )
            params = _param_count(model)

            shapes = _candidate_input_shapes(args.batch, channels, h_img, w_img)
            (_, shape), x = _try_forward_shapes(model, shapes, device=device)
            input_shape = list(shape)

            fname = f"{args.dataset}_{backbone}_dim{args.dim}_H{h}.onnx"
            path = out_dir / fname
            dynamic_axes = _dynamic_axes_for(shape)

            # Torch 2.9+ defaults to the Dynamo-based exporter which depends on onnxscript.
            # Use the legacy path (dynamo=False) for portability in minimal environments.
            torch.onnx.export(
                model,
                (x,),
                str(path),
                export_params=True,
                opset_version=int(args.opset),
                do_constant_folding=True,
                input_names=["input"],
                output_names=["logits"],
                dynamic_axes=dynamic_axes,
                dynamo=False,
                external_data=False,
                optimize=False,
            )

            onnx_path = str(path)
            file_bytes = path.stat().st_size if path.exists() else None
        except BaseException:
            error = traceback.format_exc(limit=50)

        results.append(
            ExportResult(
                backbone=backbone,
                ok=onnx_path is not None and error is None,
                onnx_path=onnx_path,
                input_shape=input_shape,
                params=params,
                file_bytes=file_bytes,
                error=error,
            )
        )

    manifest = {
        "dataset": args.dataset,
        "dim": int(args.dim),
        "hidden": h,
        "image_height": h_img,
        "image_width": w_img,
        "channels": channels,
        "device": str(device),
        "opset": int(args.opset),
        "pyr_depth": int(args.pyr_depth),
        "pyr_drop_path": float(args.pyr_drop_path),
        "pyr_patch_width": int(args.pyr_patch_width),
        "pyr_stem_out": int(args.pyr_stem_out),
        "results": [asdict(r) for r in results],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    ok_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count
    print(f"ONNX export done. ok={ok_count} fail={fail_count}. See {out_dir/'manifest.json'}")
    if fail_count:
        print("Failed backbones:")
        for r in results:
            if not r.ok:
                print(f" - {r.backbone}")


if __name__ == "__main__":
    main()
