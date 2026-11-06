from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from services.parts import contact_sheet


def _png(color):
    data = io.BytesIO()
    Image.new("RGB", (50, 30), color).save(data, format="PNG")
    return data.getvalue()


def test_sheet_orders_labels_images_and_missing_tile(monkeypatch):
    labels = []
    text = ImageDraw.ImageDraw.text

    def capture(self, xy, value, *args, **kwargs):
        labels.append(value)
        return text(self, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    data = contact_sheet.compose_contact_sheet([
        {"index": 4, "title": "Red screw", "vendor": "Acme", "designation": "M3",
         "image": _png("red"), "provenance": "product_image"},
        {"index": 7, "title": "Blue bearing", "image": _png("blue"),
         "provenance": "cad_render"},
        {"index": 9, "title": "Unknown part", "image": None},
    ])
    with Image.open(io.BytesIO(data)) as sheet:
        assert sheet.format == "PNG" and sheet.size == (1152, 360)
        assert sheet.getpixel((192, 200))[:3] == (255, 0, 0)
        assert sheet.getpixel((384 + 192, 200))[:3] == (0, 0, 255)
    assert [label for label in labels if label in ("#4", "#7", "#9")] == ["#4", "#7", "#9"]
    assert "Red screw" in labels and "Blue bearing" in labels and "Unknown part" in labels
    assert "Acme · M3" in labels
    assert "Product photo" in labels and "CAD render" in labels and "No preview" in labels


def test_sheet_bounds_and_invalid_preview():
    tiles = [{"index": i, "title": "A deliberately long part title " * 4, "image": b"not an image"}
             for i in range(16)]
    with Image.open(io.BytesIO(contact_sheet.compose_contact_sheet(tiles, cols=1))) as sheet:
        assert max(sheet.size) <= 1568
    with pytest.raises(ValueError):
        contact_sheet.compose_contact_sheet(tiles + [tiles[0]])


def test_presentation_provenance_missing_and_network_policy(monkeypatch):
    from services.parts import thumbs
    calls = []

    def resolve(items, *, budget_s, allow_network):
        calls.append((items, budget_s, allow_network))
        return [
            {"item_id": items[0]["item_id"], "ok": True, "data": _png("green"),
             "provenance": "product_image"},
            {"item_id": items[1]["item_id"], "ok": True, "data": b"bad",
             "provenance": "cad_render"},
        ]

    monkeypatch.setattr(thumbs, "resolve_previews", resolve)
    payload = {"ok": True, "results": [
        {"index": 3, "part_id": "one", "title": "One", "thumbnail_url": "https://x/one.png"},
        {"index": 8, "part_id": "two", "title": "Two"},
    ]}
    body, png = contact_sheet.present_search(payload, allow_network=False)
    assert png and body["preview_sheet"]["pictured"] == [3, 8]
    assert body["results"][0]["preview"] == {"provenance": "product_image", "reason": ""}
    assert body["results"][1]["preview"] == {"provenance": "", "reason": "invalid_preview"}
    assert calls == [([{"item_id": "one", "image_url": "https://x/one.png"},
                       {"item_id": "two", "image_url": ""}], 8.0, False)]
    disabled, png = contact_sheet.present_search(payload, previews=False)
    assert png is None and disabled["preview_sheet"]["reason"] == "previews_disabled"
    empty, png = contact_sheet.present_search({"ok": True, "results": []})
    assert png is None and empty["preview_sheet"]["reason"] == "no_results"
    assert len(calls) == 1


def test_compose_failure_is_reported_without_failing_search(monkeypatch):
    from services.parts import thumbs
    monkeypatch.setattr(thumbs, "resolve_previews", lambda items, **kw: [])
    monkeypatch.setattr(contact_sheet, "compose_contact_sheet", lambda tiles: (_ for _ in ()).throw(ValueError("bad")))
    body, image = contact_sheet.present_search({"ok": True, "results": [{"part_id": "p", "title": "Part"}]})
    assert image is None and body["ok"]
    assert body["preview_sheet"]["pictured"] == []
    assert body["preview_sheet"]["omitted"] == [0]
    assert "composition_failed" in body["preview_sheet"]["reason"]


def test_subset_keeps_only_requested_tiles():
    tiles = [{"index": i, "title": str(i), "image": _png(color), "provenance": "product_image"}
             for i, color in enumerate(("red", "green", "blue"))]
    sheet = contact_sheet.compose_contact_sheet(tiles)
    with Image.open(io.BytesIO(contact_sheet.subset_contact_sheet(sheet, [0, 1, 2], [2, 0]))) as result:
        assert result.size == (768, 360)
        assert result.getpixel((192, 200))[:3] == (0, 0, 255)
        assert result.getpixel((576, 200))[:3] == (255, 0, 0)
