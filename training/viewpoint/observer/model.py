"""ObserverNet: a from-scratch vision network that answers structured geometry queries from a
rendered view set (no pretrained weights anywhere).

Input: the grid's tiles (uint8 [B, T, 3, H, W], T = 1 or 4, already cut apart and downsampled
from the render by train.tile_array), which tiles are real ([B, T] bool), the query vectors
([B, Q, QD]) and each query's valid options ([B, Q, A_MAX]). Output: logits [B, Q, A_MAX] with
invalid options at NEG (softmax gives them exactly 0).

Variant "a" (the cross-fit labeller): shared ResNet-ish tile encoder with GroupNorm (batch
statistics would leak between the sets of a batch and vary with batch composition), stride 32
tokens + learned position and tile-slot embeddings, one self-attention layer across ALL tiles'
tokens (counting and "which side" need evidence from several views together), then two
query-conditioned cross-attention blocks: each query attends to the image tokens on its own
(queries never attend to each other, so one query cannot leak into another's answer).
Variant "b" (the independent judge): a wider, shallower encoder with no token self-attention,
one 8-head cross-attention block plus a masked mean-pool of the tokens.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import torch
from torch import nn
from torch.nn import functional as F

from .queries import A_MAX, QD

NEG = -1e9
VARIANTS = {
    #        width, stage multipliers, blocks per stage, token self-attn layers, cross blocks, heads
    "a": dict(width=32, mult=(1, 2, 4, 6), blocks=(2, 2, 2, 2), self_layers=1, cross=2, heads=4),
    "b": dict(width=40, mult=(1, 2, 4, 8), blocks=(1, 2, 2, 1), self_layers=0, cross=1, heads=8),
}


@dataclass(frozen=True)
class ModelConfig:
    variant: str = "a"
    d: int = 192
    tile_hw: tuple = (192, 256)        # (H, W) each tile is resized to before the encoder
    max_tiles: int = 4
    width: int = 0                     # 0 = the variant's width
    dropout: float = 0.1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tile_hw"] = list(self.tile_hw)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ModelConfig":
        return cls(**{**d, "tile_hw": tuple(d["tile_hw"])})


def _groups(c: int) -> int:
    for g in (8, 4, 2):
        if c % g == 0:
            return g
    return 1


class _Block(nn.Module):
    def __init__(self, cin: int, cout: int, stride: int):
        super().__init__()
        self.c1 = nn.Conv2d(cin, cout, 3, stride, 1, bias=False)
        self.n1 = nn.GroupNorm(_groups(cout), cout)
        self.c2 = nn.Conv2d(cout, cout, 3, 1, 1, bias=False)
        self.n2 = nn.GroupNorm(_groups(cout), cout)
        self.skip = (nn.Sequential(nn.Conv2d(cin, cout, 1, stride, bias=False),
                                   nn.GroupNorm(_groups(cout), cout))
                     if stride != 1 or cin != cout else nn.Identity())

    def forward(self, x):
        y = F.relu(self.n1(self.c1(x)))
        y = self.n2(self.c2(y))
        return F.relu(y + self.skip(x))


def _grid(n: int, strides: int) -> int:
    for _ in range(strides):
        n = (n + 1) // 2
    return n


class _TileEncoder(nn.Module):
    """stride 2 stem + four stride-2 stages -> [N, d, H/32, W/32]."""

    def __init__(self, width: int, mult: tuple, blocks: tuple, d: int):
        super().__init__()
        self.stem = nn.Sequential(nn.Conv2d(3, width, 3, 2, 1, bias=False),
                                  nn.GroupNorm(_groups(width), width), nn.ReLU(inplace=True),
                                  nn.Conv2d(width, width, 3, 1, 1, bias=False),
                                  nn.GroupNorm(_groups(width), width), nn.ReLU(inplace=True))
        layers = []
        cin = width
        for m, nb in zip(mult, blocks):
            cout = width * m
            layers.append(_Block(cin, cout, 2))
            layers += [_Block(cout, cout, 1) for _ in range(nb - 1)]
            cin = cout
        self.stages = nn.Sequential(*layers)
        self.proj = nn.Conv2d(cin, d, 1)

    def forward(self, x):
        return self.proj(self.stages(self.stem(x)))


class _CrossBlock(nn.Module):
    def __init__(self, d: int, heads: int, dropout: float):
        super().__init__()
        self.nq = nn.LayerNorm(d)
        self.nk = nn.LayerNorm(d)
        self.attn = nn.MultiheadAttention(d, heads, dropout=dropout, batch_first=True)
        self.nm = nn.LayerNorm(d)
        self.mlp = nn.Sequential(nn.Linear(d, 2 * d), nn.GELU(), nn.Dropout(dropout),
                                 nn.Linear(2 * d, d))

    def forward(self, q, mem, pad):
        kv = self.nk(mem)
        q = q + self.attn(self.nq(q), kv, kv, key_padding_mask=pad, need_weights=False)[0]
        return q + self.mlp(self.nm(q))


class ObserverNet(nn.Module):
    def __init__(self, cfg: ModelConfig = ModelConfig()):
        super().__init__()
        if cfg.variant not in VARIANTS:
            raise ValueError(f"unknown ObserverNet variant {cfg.variant!r}")
        v = VARIANTS[cfg.variant]
        self.cfg = cfg
        d = cfg.d
        self.encoder = _TileEncoder(cfg.width or v["width"], v["mult"], v["blocks"], d)
        gh, gw = _grid(cfg.tile_hw[0], 5), _grid(cfg.tile_hw[1], 5)
        self.n_tok = gh * gw
        self.pos = nn.Parameter(torch.randn(1, 1, self.n_tok, d) * 0.02)
        self.slot = nn.Parameter(torch.randn(1, cfg.max_tiles, 1, d) * 0.02)
        self.self_attn = nn.ModuleList(
            nn.TransformerEncoderLayer(d, v["heads"], 2 * d, cfg.dropout, activation="gelu",
                                       batch_first=True, norm_first=True)
            for _ in range(v["self_layers"]))
        self.qmlp = nn.Sequential(nn.Linear(QD, d), nn.GELU(), nn.Linear(d, d))
        self.cross = nn.ModuleList(_CrossBlock(d, v["heads"], cfg.dropout)
                                   for _ in range(v["cross"]))
        self.pool_mean = cfg.variant == "b"
        hin = d * (3 if self.pool_mean else 2)
        self.head = nn.Sequential(nn.LayerNorm(hin), nn.Linear(hin, d), nn.GELU(),
                                  nn.Dropout(cfg.dropout), nn.Linear(d, A_MAX))

    def tokens(self, tiles: torch.Tensor, tile_mask: torch.Tensor) -> tuple:
        """Image tokens [B, T*n_tok, d] and their padding mask [B, T*n_tok] (True = pad)."""
        B, T = tiles.shape[:2]
        x = tiles.reshape(B * T, *tiles.shape[2:])
        if x.dtype == torch.uint8:
            x = x.float() / 255.0
        x = (x - 0.5) / 0.25
        f = self.encoder(x.contiguous(memory_format=torch.channels_last))
        f = f.flatten(2).transpose(1, 2).reshape(B, T, self.n_tok, -1)
        f = f + self.pos + self.slot[:, :T]
        mem = f.reshape(B, T * self.n_tok, -1)
        pad = (~tile_mask.bool()).repeat_interleave(self.n_tok, dim=1)
        for layer in self.self_attn:
            mem = layer(mem, src_key_padding_mask=pad)
        return mem, pad

    def forward(self, tiles, tile_mask, qvec, opt_mask):
        mem, pad = self.tokens(tiles, tile_mask)
        q0 = self.qmlp(qvec.to(mem.dtype) if qvec.dtype != mem.dtype else qvec)
        q = q0
        for blk in self.cross:
            q = blk(q, mem, pad)
        feats = [q, q0]
        if self.pool_mean:
            keep = (~pad).unsqueeze(-1).to(mem.dtype)
            mean = (mem * keep).sum(1) / keep.sum(1).clamp_min(1.0)
            feats.append(mean.unsqueeze(1).expand(-1, q.shape[1], -1))
        logits = self.head(torch.cat(feats, -1)).float()
        return logits.masked_fill(~opt_mask.bool(), NEG)


def p_answer(logits: torch.Tensor, answer: torch.Tensor) -> torch.Tensor:
    """Softmax probability of the true answer (the Observer's P(correct))."""
    return torch.softmax(logits.float(), -1).gather(-1, answer.long().unsqueeze(-1)).squeeze(-1)


def query_loss(logits, answer, qvalid, label_smoothing: float = 0.0) -> torch.Tensor:
    """Mean CE over real queries. Label smoothing is spread over the VALID options only."""
    lp = torch.log_softmax(logits.float(), -1)
    nll = -lp.gather(-1, answer.long().unsqueeze(-1)).squeeze(-1)
    if label_smoothing > 0:
        valid = logits > NEG / 2
        smooth = -(lp.masked_fill(~valid, 0.0).sum(-1) / valid.sum(-1).clamp_min(1))
        nll = (1 - label_smoothing) * nll + label_smoothing * smooth
    w = qvalid.float()
    return (nll * w).sum() / w.sum().clamp_min(1.0)


def augment(tiles: torch.Tensor, gen: torch.Generator, *, scale=(0.9, 1.06),
            shift: float = 0.04, jitter: float = 0.12) -> torch.Tensor:
    """Geometry-preserving augmentation of float tiles [N, 3, H, W] in 0..1: brightness /
    contrast / per-channel colour gain, a small zoom and shift. Never a flip or a rotation:
    sides, the XYZ triad and the letter badges must keep their meaning."""
    N = tiles.shape[0]
    dev = tiles.device

    def u(lo, hi, *shape):
        return torch.rand(*shape, generator=gen, device=dev) * (hi - lo) + lo

    s = u(scale[0], scale[1], N)
    tx, ty = u(-shift, shift, N), u(-shift, shift, N)
    theta = torch.zeros(N, 2, 3, device=dev)
    theta[:, 0, 0] = s
    theta[:, 1, 1] = s
    theta[:, 0, 2] = tx
    theta[:, 1, 2] = ty
    grid = F.affine_grid(theta, list(tiles.shape), align_corners=False)
    x = F.grid_sample(tiles, grid, mode="bilinear", padding_mode="border", align_corners=False)
    mean = x.mean(dim=(1, 2, 3), keepdim=True)
    x = (x - mean) * u(1 - jitter, 1 + jitter, N, 1, 1, 1) + mean
    x = x * u(1 - jitter, 1 + jitter, N, 1, 1, 1) * u(1 - jitter / 3, 1 + jitter / 3, N, 3, 1, 1)
    return x.clamp_(0.0, 1.0)


def param_count(model: nn.Module) -> int:
    return int(sum(p.numel() for p in model.parameters()))


__all__ = ["ModelConfig", "NEG", "ObserverNet", "VARIANTS", "augment", "p_answer",
           "param_count", "query_loss"]
