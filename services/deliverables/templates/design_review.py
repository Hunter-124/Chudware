"""``design_review`` — the design-review deck (pptx), an engineering template.

The slides a professional engineer presents at a design review, built from live project state
(contract §4 — every load-bearing number resolved from an attached tool result, never typed):

- ``title``    — title slide (heading + subtitle prose).
- ``views``    — rendered views / contact sheet: a slide of render PNGs (``images``: project
                 paths, free prose) with captions. Missing files degrade to captions.
- ``massprops``— mass-properties slide; load-bearing slots ``mass``, ``volume``,
                 ``surface_area``, ``cg_x``/``cg_y``/``cg_z`` (from describe_part / measure_part).
- ``fea_margins`` — FEA margins slide; load-bearing slots ``max_stress``, ``min_fos``,
                 ``max_disp``, ``yield_strength``, ``margin`` (from run_fea_static).
- ``risks``    — open risks / questions from the run record (free-prose bullets or a ``rows``
                 table of risk/severity/owner; no load-bearing numbers).

Registered as ``kind='presentation'`` so it is driven by the existing ``make_presentation`` MCP
tool — no contract change (contract §8 generic registry).
"""
from __future__ import annotations

from pathlib import Path

from . import Template, register_template, resolve_image, slot_lines

_MASSPROPS = {"mass", "volume", "surface_area", "cg_x", "cg_y", "cg_z"}
_FEA = {"max_stress", "min_fos", "max_disp", "yield_strength", "margin"}


def _add_bulleted(slide, lines) -> None:
    if len(slide.placeholders) > 1:
        slide.placeholders[1].text = "\n".join(lines)


def _build(resolved: dict, fmt: str, out_path: Path) -> None:
    from pptx import Presentation  # noqa: PLC0415
    from pptx.util import Inches  # noqa: PLC0415

    prs = Presentation()
    blocks = resolved["blocks"] or [{"type": "title", "heading": resolved.get("title", ""),
                                     "body": [], "slots": {}, "rows": []}]
    for blk in blocks:
        btype = blk.get("type")
        if btype == "title":
            slide = prs.slides.add_slide(prs.slide_layouts[0])
            slide.shapes.title.text = blk.get("heading") or resolved.get("title", "")
            if len(slide.placeholders) > 1:
                slide.placeholders[1].text = "\n".join(blk.get("body", []))
            continue

        if btype == "views":
            slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only
            slide.shapes.title.text = blk.get("heading", "Views")
            image_refs = list(blk.get("images") or [])
            resolved_images = [(ref, resolve_image(ref)) for ref in image_refs]
            available_images = [(ref, path) for ref, path in resolved_images if path is not None]
            captions = list(blk.get("body", []))
            failed_refs = [ref for ref, path in resolved_images if path is None]
            successful_insertions = 0
            if available_images:
                n = len(available_images)
                w = Inches(9.0 / max(1, min(n, 3)))
                for i, (ref, img) in enumerate(available_images[:6]):
                    left = Inches(0.5) + (i % 3) * w
                    top = Inches(1.6) + (i // 3) * Inches(3.0)
                    try:
                        slide.shapes.add_picture(str(img), left, top, height=Inches(2.6))
                    except Exception:  # noqa: BLE001 - degrade visibly when a render is unreadable
                        failed_refs.append(ref)
                    else:
                        successful_insertions += 1

            degraded_lines = [f"[render unavailable: {ref}]" for ref in failed_refs]
            if successful_insertions == 0 and (captions or degraded_lines):
                tb = slide.shapes.add_textbox(Inches(0.5), Inches(1.6), Inches(9), Inches(3))
                tb.text_frame.text = "\n".join(captions + degraded_lines)
            elif degraded_lines:
                tb = slide.shapes.add_textbox(Inches(0.5), Inches(1.15), Inches(9), Inches(0.4))
                tb.text_frame.text = "  ".join(degraded_lines)
            continue

        # massprops / fea_margins / risks / generic → a content slide with resolved values.
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = blk.get("heading", btype.replace("_", " ").title())
        lines = list(blk.get("body", [])) + slot_lines(blk)
        for row in blk.get("rows", []):
            cells = [c.get("text", c.get("formula", "")) for c in row]
            lines.append(" | ".join(str(x) for x in cells))
        _add_bulleted(slide, lines)

    prs.save(str(out_path))


register_template(Template(
    "design_review", "presentation", _build,
    lb_slots={"massprops": set(_MASSPROPS), "fea_margins": set(_FEA)}))
