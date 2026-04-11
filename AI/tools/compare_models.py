#!/usr/bin/env python3
import argparse
import glob
import os
import time
import sys
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

import torch


@dataclass
class ModelSpec:
    name: str
    backbone: str
    dim: int
    hidden: int
    pyr_depth: int
    pyr_drop_path: float
    pyr_patch_width: int
    pyr_stem_out: int
    ckpt: Optional[str] = None


@dataclass
class Result:
    name: str
    backbone: str
    dim: int
    hidden: int
    params: int
    ckpt: str
    ckpt_mb: float
    device: str
    batch: int
    ms_per_inf: float
    test_acc: Optional[float]


def load_spec_json(dataset: str) -> Dict[str, Any]:
    import json

    path = os.path.join("datasets", dataset, "spec.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def count_params(m: torch.nn.Module) -> int:
    return sum(p.numel() for p in m.parameters())


def find_latest_ckpt(dataset: str, backbone: str) -> Optional[str]:
    save_dir = os.path.join("save", dataset, "classify")
    if not os.path.isdir(save_dir):
        return None
    key = f"_M_{backbone.upper()}_"
    candidates = [p for p in glob.glob(os.path.join(save_dir, "*.pt")) if key in os.path.basename(p)]
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]

def parse_ckpt_hparams(path: str) -> Dict[str, Any]:
    """
    Parse common hyperparams encoded in AIRHAR checkpoint filename, e.g.:
    CL_S_0_M_RADMAMBA_B_16_LR_0.0050_H_64_P_71289_FL_224_ST_1.pt
    """
    base = os.path.basename(path)
    parts = base.split("_")
    out: Dict[str, Any] = {}
    for i in range(len(parts) - 1):
        k = parts[i]
        v = parts[i + 1]
        if k == "M":
            out["backbone"] = v.lower()
        if k == "H":
            try:
                out["hidden"] = int(v)
            except ValueError:
                pass
        if k == "DIM":
            try:
                out["dim"] = int(v)
            except ValueError:
                pass
        if k == "LR":
            try:
                out["lr"] = float(v)
            except ValueError:
                pass
        if k == "B":
            try:
                out["batch"] = int(v)
            except ValueError:
                pass
    return out


def infer_dim_from_state(state: Dict[str, Any], num_classes: int) -> Optional[int]:
    """
    Infer model embedding dim from a checkpoint state_dict by looking for the final
    classifier weight shaped (num_classes, dim).
    """
    for k, v in state.items():
        if not hasattr(v, "shape"):
            continue
        if getattr(v, "ndim", 0) == 2 and v.shape[0] == num_classes:
            return int(v.shape[1])
    # fallback: try common token/pos shapes
    for k, v in state.items():
        if not hasattr(v, "shape"):
            continue
        if k.endswith("cls_token") and getattr(v, "ndim", 0) == 3:
            return int(v.shape[-1])
    return None


def ckpt_size_mb(path: Optional[str]) -> float:
    if not path or not os.path.exists(path):
        return 0.0
    return os.path.getsize(path) / (1024 * 1024)


def build_model(spec: ModelSpec, dataset_spec: Dict[str, Any], device: torch.device) -> torch.nn.Module:
    # Ensure repo root is on sys.path when running as a script
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import models

    m = models.CoreModel(
        hidden_size=spec.hidden,
        num_layers=int(dataset_spec.get("Classification_num_layers", 1)),
        backbone_type=spec.backbone,
        dim=spec.dim,
        dt_rank=int(dataset_spec.get("dt_rank", 0)),
        d_state=int(dataset_spec.get("d_state", 4)),
        image_height=int(dataset_spec.get("image_height", 224)),
        image_width=int(dataset_spec.get("frame_length", 224)),
        num_classes=int(dataset_spec.get("num_classes", 11)),
        channels=int(dataset_spec.get("channels", 1)),
        dropout=float(dataset_spec.get("dropout", 0.0)),
        optional_avg_pool=bool(dataset_spec.get("optional_avg_pool", False)),
        channel_confusion_layer=int(dataset_spec.get("channel_confusion_layer", 1)),
        channel_confusion_out_channels=int(dataset_spec.get("channel_confusion_out_channels", 1)),
        time_downsample_factor=int(dataset_spec.get("time_downsample_factor", 4)),
        pyr_depth=spec.pyr_depth,
        pyr_drop_path=spec.pyr_drop_path,
        pyr_patch_width=spec.pyr_patch_width,
        pyr_stem_out=spec.pyr_stem_out,
    ).to(device)

    if spec.ckpt and os.path.exists(spec.ckpt):
        state = torch.load(spec.ckpt, map_location=device)
        try:
            m.load_state_dict(state, strict=True)
        except RuntimeError as e:
            raise RuntimeError(
                f"Checkpoint mismatch for backbone={spec.backbone}. "
                f"Pass matching --dim/--hidden/--pyr_* flags or disable --auto_ckpt.\n{e}"
            )
    m.eval()
    return m


def bench_latency(model: torch.nn.Module, x: torch.Tensor, iters: int = 200, warmup: int = 30) -> float:
    device = x.device
    with torch.no_grad():
        for _ in range(warmup):
            _ = model(x)
        if device.type == "cuda":
            torch.cuda.synchronize()
        t0 = time.time()
        for _ in range(iters):
            _ = model(x)
        if device.type == "cuda":
            torch.cuda.synchronize()
        t1 = time.time()
    return (t1 - t0) / iters * 1000.0


def eval_test_acc(model: torch.nn.Module, dataset: str, batch: int, device: torch.device) -> float:
    from modules.data_collection import Radardataloader, RadarFrameDataset
    from torch.utils.data import DataLoader

    rd = Radardataloader(root=dataset, subset="test")
    ds_spec = load_spec_json(dataset)
    frame_length = int(ds_spec.get("frame_length", 224))
    stride = int(ds_spec.get("stride", 1))
    continuous = bool(ds_spec.get("continuous", False))
    test_frames = RadarFrameDataset(rd, frame_length=frame_length, stride=stride, subset="test", continuous=continuous)
    dl = DataLoader(test_frames, batch_size=batch, shuffle=False)

    correct = 0
    total = 0
    with torch.no_grad():
        for xb, yb in dl:
            xb = xb.to(device)
            yb = yb.to(device)
            logits = model(xb)
            pred = logits.argmax(dim=1)
            gt = yb.argmax(dim=1)
            correct += (pred == gt).sum().item()
            total += gt.numel()
    return 100.0 * correct / max(1, total)


def print_table(results: List[Result]):
    cols = ["name", "backbone", "dim", "hidden", "params", "ckpt_mb", "device", "batch", "ms_per_inf", "test_acc"]
    rows = []
    for r in results:
        d = asdict(r)
        rows.append([d[c] for c in cols])

    widths = [max(len(str(c)), max(len(str(row[i])) for row in rows)) for i, c in enumerate(cols)]
    header = " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cols))
    sep = "-+-".join("-" * widths[i] for i in range(len(cols)))
    print(header)
    print(sep)
    for row in rows:
        print(" | ".join(str(row[i]).ljust(widths[i]) for i in range(len(cols))))


def parse_args():
    p = argparse.ArgumentParser(description="Compare AIRHAR backbones: params, checkpoint size, latency, test accuracy.")
    p.add_argument("--dataset", required=True, help="Dataset name (e.g., DIAT, CI4R)")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"], help="Device for latency + eval")
    p.add_argument("--batch", type=int, default=1, help="Batch size for latency benchmark")
    p.add_argument("--bench_iters", type=int, default=200)
    p.add_argument("--eval", action="store_true", help="Also compute test accuracy (requires datasets/<dataset>/<dataset>_data.h5)")

    p.add_argument("--dim", type=int, default=80)
    p.add_argument("--hidden", type=int, default=64)
    p.add_argument("--pyr_depth", type=int, default=2)
    p.add_argument("--pyr_drop_path", type=float, default=0.02)
    p.add_argument("--pyr_patch_width", type=int, default=4)
    p.add_argument("--pyr_stem_out", type=int, default=32)

    p.add_argument(
        "--backbones",
        nargs="+",
        default=["radmamba", "conv_mamba_pyr_lite"],
        help="Backbone names to compare (e.g., radmamba conv_mamba_pyr_lite cnnlstm)",
    )
    p.add_argument("--auto_ckpt", action="store_true", help="Auto-pick latest checkpoint per backbone from save/<dataset>/classify/")
    return p.parse_args()


def main():
    args = parse_args()
    ds_spec = load_spec_json(args.dataset)
    device = torch.device(args.device if args.device == "cpu" or torch.cuda.is_available() else "cpu")

    # Input shape uses dataset spec
    c = int(ds_spec.get("channels", 1))
    h = int(ds_spec.get("image_height", 224))
    w = int(ds_spec.get("frame_length", 224))
    x = torch.randn(args.batch, c, h, w, device=device)

    results: List[Result] = []
    for backbone in args.backbones:
        ckpt = find_latest_ckpt(args.dataset, backbone) if args.auto_ckpt else None
        if ckpt:
            parsed = parse_ckpt_hparams(ckpt)
            # Use checkpoint hidden size if present to avoid load mismatches
            ckpt_hidden = parsed.get("hidden")
            ckpt_dim = parsed.get("dim")
            # If DIM not encoded in filename, infer from checkpoint tensors
            if ckpt_dim is None:
                try:
                    state_cpu = torch.load(ckpt, map_location="cpu")
                    ckpt_dim = infer_dim_from_state(state_cpu, int(ds_spec.get("num_classes", 11)))
                except Exception:
                    ckpt_dim = None
        else:
            ckpt_hidden = None
            ckpt_dim = None
        spec = ModelSpec(
            name=f"{args.dataset}_{backbone}_dim{ckpt_dim or args.dim}_H{ckpt_hidden or args.hidden}",
            backbone=backbone,
            dim=int(ckpt_dim or args.dim),
            hidden=int(ckpt_hidden or args.hidden),
            pyr_depth=args.pyr_depth,
            pyr_drop_path=args.pyr_drop_path,
            pyr_patch_width=args.pyr_patch_width,
            pyr_stem_out=args.pyr_stem_out,
            ckpt=ckpt,
        )
        model = build_model(spec, ds_spec, device)
        ms = bench_latency(model, x, iters=args.bench_iters)
        acc = eval_test_acc(model, args.dataset, batch=max(1, args.batch), device=device) if args.eval else None
        results.append(
            Result(
                name=spec.name,
                backbone=backbone,
                dim=args.dim,
                hidden=args.hidden,
                params=count_params(model),
                ckpt=spec.ckpt or "",
                ckpt_mb=round(ckpt_size_mb(spec.ckpt), 2),
                device=device.type,
                batch=args.batch,
                ms_per_inf=round(ms, 3),
                test_acc=None if acc is None else round(acc, 3),
            )
        )

    print_table(results)


if __name__ == "__main__":
    main()
