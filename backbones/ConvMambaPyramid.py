import torch
from torch import nn, Tensor
from einops.layers.torch import Rearrange, Reduce
from einops import rearrange
from backbones.SSM import SSM


class DropPath(nn.Module):
    def __init__(self, drop_prob: float = 0.0):
        super().__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        if self.drop_prob == 0.0 or not self.training:
            return x
        keep = 1 - self.drop_prob
        shape = (x.shape[0],) + (1,) * (x.ndim - 1)
        mask = torch.rand(shape, device=x.device, dtype=x.dtype) < keep
        return x * mask / keep


class SE(nn.Module):
    def __init__(self, c, r=4):
        super().__init__()
        m = max(1, c // r)
        self.se = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(c, m, 1),
            nn.SiLU(),
            nn.Conv2d(m, c, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        return x * self.se(x)


class MambaBlock(nn.Module):
    def __init__(self, dim, dt_rank, dim_inner, d_state, drop_path=0.0):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.proj_f = nn.Conv1d(dim, dim, 1)
        self.proj_b = nn.Conv1d(dim, dim, 1)
        self.ssm_f = SSM(dim, dt_rank, dim_inner, d_state)
        self.ssm_b = SSM(dim, dt_rank, dim_inner, d_state)
        self.gate = nn.Conv1d(dim, dim, 3, padding=1)
        self.out = nn.Conv1d(dim, dim, 1)
        self.dp = DropPath(drop_path)
        self.act = nn.SiLU()

    def forward(self, x):  # x: (B, S, D)
        skip = x
        x = self.norm(x)
        x = rearrange(x, "b s d -> b d s")
        g = self.act(self.gate(x))
        xf = self.proj_f(x)
        xb = self.proj_b(torch.flip(x, dims=[2]))
        yf = self.ssm_f(rearrange(xf, "b d s -> b s d"))
        yb = self.ssm_b(rearrange(xb, "b d s -> b s d"))
        yb = torch.flip(yb, dims=[1])
        y = yf + yb
        y = rearrange(y, "b s d -> b d s")
        y = self.out(y * g)
        y = rearrange(y, "b d s -> b s d")
        return skip + self.dp(y)


class ConvMambaPyramid(nn.Module):
    """
    Two-branch conv stem + Mamba blocks over vertical stripe tokens.
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
        depth: int = 3,
        drop_path_rate: float = 0.1,
        patch_width: int = 2,
    ):
        super().__init__()
        self.channels = channels

        # dual-branch depthwise stems
        self.stem3 = nn.Sequential(
            nn.Conv2d(channels, channels, 3, padding=1, groups=channels),
            nn.Conv2d(channels, dim // 4, 1),
            nn.BatchNorm2d(dim // 4),
            nn.SiLU(),
        )
        self.stem5 = nn.Sequential(
            nn.Conv2d(channels, channels, 5, padding=2, groups=channels),
            nn.Conv2d(channels, dim // 4, 1),
            nn.BatchNorm2d(dim // 4),
            nn.SiLU(),
        )
        self.stem_fuse = nn.Conv2d(dim // 2, dim, 1)
        self.se = SE(dim)
        self.bn = nn.BatchNorm2d(dim)

        # vertical stripe patching: full H, stripe width=2
        patch_height = image_height
        patch_width = 2
        self.patch = Rearrange(
            "b c (h ph) (w pw) -> b (h w) (ph pw c)",
            ph=patch_height,
            pw=patch_width,
        )
        self.patch_height = patch_height
        self.patch_width = patch_width if patch_width > 0 else 1
        patch_dim = dim * self.patch_height * self.patch_width  # after fuse to dim
        self.patch_proj = nn.Sequential(
            nn.Linear(patch_dim, dim),
            nn.LayerNorm(dim),
            nn.Dropout(dropout),
        )

        dpr = torch.linspace(0, drop_path_rate, steps=depth).tolist()
        self.blocks = nn.ModuleList(
            [
                MambaBlock(dim=dim, dt_rank=dt_rank, dim_inner=dim_inner, d_state=d_state, drop_path=dpr[i])
                for i in range(depth)
            ]
        )

        self.head = nn.Sequential(
            Reduce("b s d -> b d", "mean"),
            nn.LayerNorm(dim),
            nn.Linear(dim, dim),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Linear(dim, num_classes),
        )

    def forward(self, x: Tensor):
        if x.dim() == 3:
            x = x.unsqueeze(1)
        # stem
        x3 = self.stem3(x)
        x5 = self.stem5(x)
        x = torch.cat([x3, x5], dim=1)
        x = self.stem_fuse(x)
        x = self.se(x)
        x = self.bn(x)

        # patching
        if x.shape[-1] % self.patch_width != 0:
            pad_w = self.patch_width - (x.shape[-1] % self.patch_width)
            x = nn.functional.pad(x, (0, pad_w, 0, 0))
        # overlapping stripes if stride < patch_width
        stride = 1 if self.patch_width > 1 else 1
        if stride < self.patch_width:
            # unfold with overlap
            b, c, h, w = x.shape
            # reshape to (b, c, h, num_patches, patch_width)
            patches = x.unfold(3, self.patch_width, stride)
            # combine h and num_patches
            patches = patches.permute(0, 3, 2, 1, 4)  # b, np, h, c, pw
            patches = patches.reshape(b, -1, h * c * self.patch_width)
            tokens = patches
        else:
            tokens = self.patch(x)
        tokens = self.patch_proj(tokens)

        for blk in self.blocks:
            tokens = blk(tokens)

        logits = self.head(tokens)
        return logits
