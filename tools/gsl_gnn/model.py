"""Per-face GNN classifier: 3-layer GraphSAGE + linear classification head.

GATED SCAFFOLD — see README.md. `torch` / `torch_geometric` are imported
lazily inside `build_model` so this module stays importable and
`py_compile`-clean without either package installed.
"""
from __future__ import annotations


def _require_torch():
    try:
        import torch  # noqa: F401
        import torch.nn as nn
        from torch_geometric.nn import SAGEConv
    except ImportError as exc:  # pragma: no cover - exercised only w/o deps
        raise ImportError(
            "tools/gsl_gnn requires torch and torch-geometric. Install with "
            "`pip install -r tools/gsl_gnn/requirements.txt` (install torch "
            "first for your CUDA version per pytorch.org). "
            f"Original import error: {exc}"
        ) from exc
    return torch, nn, SAGEConv


def build_model(in_channels: int, num_classes: int, hidden_channels: int = 128, dropout: float = 0.2):
    """3-layer GraphSAGE (hidden 128, ReLU, dropout 0.2) + linear per-node head.

    Output is per-face class logits only. Per the training gate in
    README.md, any consumer of this model's predictions must route them
    through a TEMPLATED caption — never free-form numbers — and must never
    drop the raw `doc_query` face list the prediction was computed from.
    """
    torch, nn, SAGEConv = _require_torch()

    class GSLFaceGNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = SAGEConv(in_channels, hidden_channels)
            self.conv2 = SAGEConv(hidden_channels, hidden_channels)
            self.conv3 = SAGEConv(hidden_channels, hidden_channels)
            self.dropout = nn.Dropout(dropout)
            self.head = nn.Linear(hidden_channels, num_classes)

        def forward(self, x, edge_index):
            h = torch.relu(self.conv1(x, edge_index))
            h = self.dropout(h)
            h = torch.relu(self.conv2(h, edge_index))
            h = self.dropout(h)
            h = torch.relu(self.conv3(h, edge_index))
            h = self.dropout(h)
            return self.head(h)

    return GSLFaceGNN()
