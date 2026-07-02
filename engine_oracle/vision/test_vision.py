"""Tests for engine.vision — ingestion caps, the known-artifact fiducial measurement, the
out-of-plane flag, and the intake-routing honesty guarantee (a photo never satisfies a press fit).

The OpenCV-backed tests ``importorskip("cv2")`` so the suite stays green on a box that hasn't
provisioned the optional dep; the ingestion + intake-routing tests are pure Pillow/stdlib and
always run.
"""
from __future__ import annotations

import collections

import pytest

from engine.vision import (
    PREVIEW_MAX_BYTES,
    PREVIEW_MAX_PX,
    HONESTY_STRING,
    MeasureResult,
    PhotoRegistry,
    ingest_image,
)


# --------------------------------------------------------------------------- ingestion (no cv2)

def _make_photo(path, size=(3000, 2000)):
    from PIL import Image
    # A noisy gradient — photographic content PNG-compresses poorly, so this exercises the
    # JPEG fallback + dimension-reduction path that guarantees the <1 MB cap.
    import random
    random.seed(0)
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(0, size[1], 1):
        for x in range(0, size[0], 1):
            px[x, y] = ((x * 3 + y) % 256, (y * 5) % 256, (x + y * 2) % 256)
    im.save(path)
    return path


def test_ingest_preview_obeys_agent_caps(tmp_path):
    src = _make_photo(tmp_path / "part.png")
    rec = ingest_image(str(src), cache_dir=str(tmp_path / "cache"))
    assert max(rec.preview_width, rec.preview_height) <= PREVIEW_MAX_PX
    assert rec.preview_bytes <= PREVIEW_MAX_BYTES
    assert rec.width == 3000 and rec.height == 2000          # full-res untouched
    assert rec.resource_link == rec.path                     # full-res linked, not inlined
    assert rec.photo_id.startswith("ph_")
    from pathlib import Path
    assert Path(rec.preview_path).is_file()


def test_ingest_is_idempotent_and_roundtrips(tmp_path):
    src = _make_photo(tmp_path / "p.png", size=(1200, 800))
    reg = PhotoRegistry()
    a = reg.register(str(src), cache_dir=str(tmp_path))
    b = reg.register(str(src), cache_dir=str(tmp_path))
    assert a.photo_id == b.photo_id                          # same bytes -> same id
    reg2 = PhotoRegistry.from_dict(reg.to_dict())
    assert reg2.get(a.photo_id).path == a.path


# ------------------------------------------------------------------- known-artifact fiducial

def _charuco_sheet_and_row_pair(tmp_path):
    import cv2
    from engine.vision import DEFAULT_BOARD, generate_charuco_sheet
    sheet = str(tmp_path / "sheet.png")
    generate_charuco_sheet(DEFAULT_BOARD, out_path=sheet, dpi=300)
    img = cv2.imread(sheet)
    board = DEFAULT_BOARD.cv_board()
    cc, cid, _mc, _mid = cv2.aruco.CharucoDetector(board).detectBoard(img)
    bpts = board.getChessboardCorners()
    ids = cid.flatten()
    byrow = collections.defaultdict(list)
    corners = cc.reshape(-1, 2)
    for k, i in enumerate(ids):
        x, y, _z = bpts[i]
        byrow[round(float(y), 3)].append((float(x), corners[k]))
    row = max(byrow.values(), key=len)
    row.sort(key=lambda t: t[0])
    (x0, p0), (x1, p1) = row[0], row[-1]
    return sheet, img, tuple(p0), tuple(p1), abs(x1 - x0)


def test_charuco_measures_within_tolerance(tmp_path):
    pytest.importorskip("cv2")
    from engine.vision import measure_from_photo
    sheet, _img, p0, p1, known_mm = _charuco_sheet_and_row_pair(tmp_path)
    res = measure_from_photo(sheet, p0, p1)
    assert res.ok, res.reason
    assert res.value_mm == pytest.approx(known_mm, rel=0.05)   # within ±5% (DoD tolerance)
    assert res.in_plane is True
    assert res.tier in ("high", "measured")                   # 300 dpi fronto-parallel -> high
    assert res.provenance["method"] == "photo_fiducial"


def test_out_of_plane_is_flagged_low(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from engine.vision import measure_from_photo
    sheet, img, p0, p1, _known = _charuco_sheet_and_row_pair(tmp_path)
    # Warp the sheet with a strong perspective tilt -> near markers swell, far ones shrink, so the
    # marker-area CoV rises and the board reads out-of-plane.
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[0, 0], [w, 0], [int(w * 0.78), h], [int(w * 0.22), h]])
    warped = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h),
                                 borderValue=(255, 255, 255))
    tilted = str(tmp_path / "tilted.png")
    cv2.imwrite(tilted, warped)
    # Endpoints don't matter for the planarity verdict; reuse two corners far apart.
    res = measure_from_photo(tilted, (int(w * 0.3), int(h * 0.5)), (int(w * 0.7), int(h * 0.5)))
    if res.ok:                                                 # detection may still succeed
        assert res.in_plane is False
        assert res.tier == "low"


# --------------------------------------------------------------- intake routing (no cv2 needed)

def test_measure_result_to_claim_carries_fiducial_tier():
    res = MeasureResult(ok=True, value_mm=18.02, tier="high", uncert_mm=0.36, in_plane=True,
                        provenance={"method": "photo_fiducial", "px_per_mm": 11.8})
    claim = res.to_claim("mount_holes", "spacing_mm", fit="location")
    assert claim["tier"] == "high"                            # optics-derived, not re-scored
    assert claim["value_mm"] == 18.02
    assert claim["scale_source"] == "card"                    # a printed fiducial is a card ref
    assert claim["provenance"]["method"] == "photo_fiducial"
    assert claim["fit"] == "location"
    assert HONESTY_STRING and "±2–5%" in HONESTY_STRING


def test_photo_high_never_satisfies_a_press_fit():
    """The core honesty guarantee: a photo tops out at 'high', but press/bearing/threaded fits
    require a typed-caliper 'measured' value — so vision can never silently close a tight fit."""
    from engine.intake import intake as ik
    assert ik.required_tier("press") == "measured"
    assert not ik.is_satisfied("high", ik.required_tier("press"))    # photo 'high' < 'measured'
    assert ik.is_satisfied("high", ik.required_tier("slip"))          # but a slip fit is content
