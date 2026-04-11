import torch
from torch import nn, Tensor
from einops import rearrange
from einops.layers.torch import Rearrange, Reduce

from backbones.SSM import SSM


class DropPath(nn.Module):
    def __init__(self, drop_prob: float = 0.0):
        super().__init__()
        self.drop_prob = float(drop_prob)

    def forward(self, x: Tensor) -> Tensor:
        if self.drop_prob == 0.0 or not self.training:
            return x
        keep = 1.0 - self.drop_prob
        shape = (x.shape[0],) + (1,) * (x.ndim - 1)
        mask = (torch.rand(shape, device=x.device) < keep).to(x.dtype)
        return x * mask / keep


class LiteSSMBlock(nn.Module):
    """
    CPU-friendly token mixer:
    - Pre-norm
    - Light local mixing via depthwise Conv1d
    - Single-direction SSM (no bidirectional scan)
    - Residual + DropPath
    """

    def __init__(self, dim: int, dt_rank: int, dim_inner: int, d_state: int, drop_path: float = 0.0):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.dw = nn.Conv1d(dim, dim, kernel_size=5, padding=2, groups=dim)
        self.pw = nn.Conv1d(dim, dim, kernel_size=1)
        self.ssm = SSM(dim, dt_rank, dim_inner, d_state)
        self.out = nn.Linear(dim, dim)
        self.act = nn.SiLU()
        self.dp = DropPath(drop_path)

    def forward(self, x: Tensor) -> Tensor:  # (B, S, D)
        skip = x
        x = self.norm(x)
        x1 = rearrange(x, "b s d -> b d s")
        x1 = self.act(self.pw(self.dw(x1)))
        x1 = rearrange(x1, "b d s -> b s d")
        x2 = self.ssm(x1, pscan=False)  # sequential scan is more portable than zeta pscan
        x2 = self.out(self.act(x2))
        return skip + self.dp(x2)


class ConvMambaPyramidLite(nn.Module):
    """
    Lightweight alternative to ConvMambaPyramid for CPU / Raspberry Pi inference.

    Design goals:
    - Fewer parameters & FLOPs
    - Avoid heavy bidirectional SSM + large patch dims
    - Preserve micro-Doppler structure with vertical stripe tokens
    """

    def __init__(
        self,
        dim: int,
        dt_rank: int,
        dim_inner: int,
        d_state: int,
        num_classes: int,
        image_height: int,
        image_width: int,
        channels: int,
        dropout: float = 0.1,
        depth: int = 2,
        drop_path_rate: float = 0.02,
        patch_width: int = 4,
        stem_out: int = 32,
        stem_pool_hw: tuple[int, int] = (8, 2),
    ):
        super().__init__()
        self.channels = channels
        self.patch_width = max(1, int(patch_width))
        self.patch_height = int(image_height)

        # Compact stem: depthwise-separable conv + pooling
        stem_c = int(stem_out)
        pool_h, pool_w = stem_pool_hw
        self.stem = nn.Sequential(
            nn.Conv2d(channels, channels, kernel_size=5, padding=2, groups=channels),
            nn.Conv2d(channels, stem_c, kernel_size=1),
            nn.BatchNorm2d(stem_c),
            nn.SiLU(),
            nn.AvgPool2d(kernel_size=(pool_h, pool_w), stride=(pool_h, pool_w)),
        )

        # Update expected patch height after pooling
        self.patch_height = max(1, self.patch_height // pool_h)

        # Tokenize: full (pooled) height, small stripe width
        self.patch = Rearrange(
            "b c (h ph) (w pw) -> b (h w) (ph pw c)",
            ph=self.patch_height,
            pw=self.patch_width,
        )

        patch_dim = stem_c * self.patch_height * self.patch_width
        self.patch_proj = nn.Sequential(
            nn.Linear(patch_dim, dim),
            nn.LayerNorm(dim),
            nn.Dropout(dropout),
        )

        dpr = torch.linspace(0.0, float(drop_path_rate), steps=int(depth)).tolist()
        self.blocks = nn.ModuleList(
            [
                LiteSSMBlock(
                    dim=dim,
                    dt_rank=dt_rank,
                    dim_inner=dim_inner,
                    d_state=d_state,
                    drop_path=dpr[i],
                )
                for i in range(int(depth))
            ]
        )

        self.head = nn.Sequential(
            Reduce("b s d -> b d", "mean"),
            nn.LayerNorm(dim),
            nn.Linear(dim, num_classes),
        )

    def forward(self, x: Tensor) -> Tensor:
        if x.dim() == 3:
            x = x.unsqueeze(1)

        x = self.stem(x)

        # Pad width for clean patching
        if x.shape[-1] % self.patch_width != 0:
            pad_w = self.patch_width - (x.shape[-1] % self.patch_width)
            x = nn.functional.pad(x, (0, pad_w, 0, 0))

        tokens = self.patch(x)
        tokens = self.patch_proj(tokens)
        for blk in self.blocks:
            tokens = blk(tokens)
        return self.head(tokens)
