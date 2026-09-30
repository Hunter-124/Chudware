"""ObserverNet: output shapes, option masking, query independence, tile padding."""
from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from training.viewpoint.observer.model import ModelConfig, ObserverNet, augment, query_loss
from training.viewpoint.observer.queries import A_MAX, OPTION_MASKS, QD

_HW = (64, 96)


def _net(variant: str) -> ObserverNet:
    torch.manual_seed(0)
    return ObserverNet(ModelConfig(variant=variant, d=32, tile_hw=_HW, width=8)).eval()


def _batch(B: int, T: int, Q: int, seed: int = 0):
    g = torch.Generator().manual_seed(seed)
    tiles = torch.randint(0, 256, (B, T, 3, *_HW), generator=g, dtype=torch.uint8)
    tmask = torch.ones(B, T, dtype=torch.bool)
    qvec = torch.randn(B, Q, QD, generator=g)
    kinds = ["count", "presence_side", "through_blind", "type"]
    mask = torch.stack([torch.stack([torch.from_numpy(OPTION_MASKS[kinds[(b + q) % 4]])
                                     for q in range(Q)]) for b in range(B)])
    return tiles, tmask, qvec, mask


@pytest.mark.parametrize("variant", ["a", "b"])
@pytest.mark.parametrize("T", [1, 4])
def test_forward_shapes_and_masked_options(variant, T):
    net = _net(variant)
    tiles, tmask, qvec, mask = _batch(3, T, 5)
    with torch.no_grad():
        logits = net(tiles, tmask, qvec, mask)
    assert logits.shape == (3, 5, A_MAX) and logits.dtype == torch.float32
    p = torch.softmax(logits, -1)
    assert torch.all(p[~mask] == 0)
    assert torch.allclose(p.sum(-1), torch.ones(3, 5))
    assert torch.all(mask.gather(-1, logits.argmax(-1, keepdim=True)))


@pytest.mark.parametrize("variant", ["a", "b"])
def test_queries_do_not_influence_each_other(variant):
    net = _net(variant)
    tiles, tmask, qvec, mask = _batch(2, 4, 3)
    with torch.no_grad():
        a = net(tiles, tmask, qvec, mask)
        qvec2 = qvec.clone()
        qvec2[:, 1:] = torch.randn_like(qvec2[:, 1:])
        b = net(tiles, tmask, qvec2, mask)
    assert torch.allclose(a[:, 0], b[:, 0], atol=1e-5)
    assert not torch.allclose(a[:, 1], b[:, 1], atol=1e-3)


@pytest.mark.parametrize("variant", ["a", "b"])
def test_padded_tiles_are_ignored(variant):
    net = _net(variant)
    tiles, tmask, qvec, mask = _batch(2, 4, 2)
    tmask[:, 1:] = False
    with torch.no_grad():
        a = net(tiles, tmask, qvec, mask)
        noisy = tiles.clone()
        noisy[:, 1:] = torch.randint(0, 256, noisy[:, 1:].shape, dtype=torch.uint8)
        b = net(noisy, tmask, qvec, mask)
        single = net(tiles[:, :1], tmask[:, :1], qvec, mask)
    assert torch.allclose(a, b, atol=1e-5)
    assert torch.allclose(a, single, atol=1e-4)


def test_loss_ignores_padded_queries():
    net = _net("a")
    tiles, tmask, qvec, mask = _batch(2, 1, 3)
    logits = net(tiles, tmask, qvec, mask)
    ans = mask.float().argmax(-1)
    valid = torch.tensor([[True, True, False], [True, False, False]])
    full = query_loss(logits, ans, valid)
    ans2 = ans.clone()
    ans2[0, 2] = ans2[1, 1] = 0
    assert torch.allclose(full, query_loss(logits, ans2, valid))


def test_augment_keeps_layout_orientation():
    # A bright left half must stay on the left: no flips.
    x = torch.zeros(8, 3, *_HW)
    x[..., : _HW[1] // 2] = 1.0
    y = augment(x, torch.Generator().manual_seed(0))
    left, right = y[..., : _HW[1] // 3].mean(), y[..., -_HW[1] // 3:].mean()
    assert left > 0.7 and right < 0.3
    assert y.min() >= 0 and y.max() <= 1


def test_dense_raster_joins_manifest_ordinals_and_marks_feature_boundary():
    import numpy as np
    from services.mcp.cad_viewpoint import perspective_camera
    from training.viewpoint.datasets.importers import CANON
    from training.viewpoint.observer.dense import raster_labels

    # Two adjacent feature instances occupy opposite halves of a planar rectangle;
    # the face-array order is reversed relative to the manifest's doc_query ordinals.
    pos = np.array([[-1., -1., 0.], [0., -1., 0.], [0., 1., 0.],
                    [-1., 1., 0.], [1., -1., 0.], [1., 1., 0.]])
    mesh = {"pos": pos, "idx": np.array([[0, 1, 2], [0, 2, 3],
                                         [1, 4, 5], [1, 5, 2]], np.int32),
            "tri_face": np.array([0, 0, 1, 1], np.int32),
            "faces": [{"body": 1, "face_id": 2}, {"body": 1, "face_id": 1}],
            "edges": []}
    rec = {"faces": [{"ordinal": 0, "class": "rib", "instance": 2},
                     {"ordinal": 1, "class": "pocket", "instance": 3}]}
    box = {"min": [-1, -1, -0.1], "max": [1, 1, 0.1]}
    view = {"camera": perspective_camera(box, (0, 0, 1), 64, 64),
            "section": None}
    labels = raster_labels(mesh, rec, view, (64, 64))
    assert labels.shape == (64, 64, 2)
    assert labels[32, 20, 0] == CANON.index("pocket") + 1
    assert labels[32, 44, 0] == CANON.index("rib") + 1
    assert labels[32, 31:33, 1].any()
    assert labels[0, 0, 0] == 0


def test_xray_labels_the_occluded_target_not_the_translucent_shell():
    import numpy as np
    from services.mcp.cad_viewpoint import perspective_camera
    from training.viewpoint.datasets.importers import CANON
    from training.viewpoint.observer.dense import raster_labels

    xy = np.array([[-1., -1.], [1., -1.], [1., 1.], [-1., 1.]])
    pos = np.concatenate((np.column_stack((xy, np.zeros(4))),
                          np.column_stack((xy, np.ones(4)))), axis=0)
    mesh = {"pos": pos, "idx": np.array([[0, 1, 2], [0, 2, 3],
                                         [4, 5, 6], [4, 6, 7]], np.int32),
            "tri_face": np.array([0, 0, 1, 1], np.int32),
            "faces": [{"body": 1, "face_id": 1}, {"body": 1, "face_id": 2}],
            "edges": []}
    rec = {"faces": [{"ordinal": 0, "class": "pocket", "instance": 1},
                     {"ordinal": 1, "class": "rib", "instance": 2}]}
    box = {"min": [-1, -1, 0], "max": [1, 1, 1]}
    camera = perspective_camera(box, (0, 0, 1), 64, 64)
    opaque = raster_labels(mesh, rec, {"camera": camera}, (64, 64))
    xray = raster_labels(mesh, rec, {"camera": camera, "xray": True,
                                     "target_faces": (0,)}, (64, 64))
    assert opaque[32, 32, 0] == CANON.index("rib") + 1
    assert xray[32, 32, 0] == CANON.index("pocket") + 1


def test_dense_cache_migrates_losslessly_and_rebuilds_truncated_files(tmp_path):
    import numpy as np
    from training.viewpoint.observer.dense import compress_dense, dense_path
    from training.viewpoint.observer.train import Corpus

    folder = tmp_path / "observer" / "dense" / "64x96"
    folder.mkdir(parents=True)
    labels = np.zeros((4, 64, 96, 2), np.uint8)
    labels[:, 10:30, 15:44, 0] = 5
    labels[:, 10:30, 15, 1] = 1
    np.save(folder / "good.npy", labels)
    (folder / "corrupt.npy").write_bytes(b"\\x93NUMPY")
    result = compress_dense(tmp_path, (64, 96), workers=1, log=lambda *_: None)
    assert result == {"converted": 1, "corrupt": 1}
    assert not (folder / "good.npy").exists()
    assert not (folder / "corrupt.npy").exists()
    corpus = Corpus(tmp_path, [{"image": "images/good.png"}], {}, [[]], (64, 96), dense=True)
    assert np.array_equal(corpus.labels(0), labels)
    assert dense_path(tmp_path, "images/good.png", (64, 96)).suffix == ".npz"
    assert compress_dense(tmp_path, (64, 96), workers=1, log=lambda *_: None)["converted"] == 0
