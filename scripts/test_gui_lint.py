"""Behavioral regressions for physical draw arguments and local DPI scaling."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("gui_lint.py")
spec = importlib.util.spec_from_file_location("gui_lint", SCRIPT)
gui_lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui_lint)


def lint_source(tmp_path, source, rules=None):
    path = tmp_path / "panel.cpp"
    path.write_text(source, encoding="utf-8")
    findings = []
    gui_lint.lint_file(str(path), set(), rules or {"scale"}, findings)
    return {(line, rule) for _, line, rule, _, _ in findings}


def test_draw_colors_flags_and_segment_counts_are_not_pixels(tmp_path):
    assert lint_source(tmp_path, "\n".join([
        "dl->AddCircle(center, ui::metrics::px(6), 255, 20, ui::metrics::stroke(4));",
        "dl->AddCircleFilled(center, metrics::px(2), ink, 12);",
        "dl->AddNgon(center, px(6), ink, 6, metrics::stroke(3));",
        "dl->AddRect(a, b, 255, metrics::rounding(4), 240, metrics::stroke(4));",
        "dl->AddRectFilled(a, b, IM_COL32(20, 30, 40, 255), metrics::px(4), 240);",
        "dl->AddTriangle(a, b, c, 255, ui::metrics::stroke(4));",
        "dl->AddTriangleFilled(a, b, c, 255);",
        "dl->AddLine(a, b, 255, metrics::stroke(4));",
    ])) == set()


def test_each_physical_circle_dimension_requires_its_own_scaling(tmp_path):
    assert lint_source(tmp_path, "\n".join([
        "dl->AddCircle(center, 6, ink, 20, ui::metrics::stroke(4));",
        "dl->AddCircle(center, ui::metrics::px(6), ink, 20, 4);",
        "dl->AddCircle(center, ui::metrics::px(6), ink, 20, ui::metrics::stroke(4));",
    ])) == {(1, "scale"), (2, "scale")}


def test_balanced_arguments_preserve_draw_argument_positions(tmp_path):
    assert lint_source(tmp_path, "\n".join([
        'dl->AddCircle (screen({x, y}), metrics::px(std::max(w, 6)), color("),(", shades[index]), 20, metrics::stroke(4));',
        'dl->AddCircle(screen({x, y}), 6, color("),(", shades[index]), 20, metrics::stroke(4));',
        "dl->AddCircleFilled(a, metrics::px(2), ink, 6); dl->AddCircleFilled(b, 8, ink, 12);",
    ])) == {(2, "scale"), (3, "scale")}


def test_scaled_axis_does_not_hide_raw_other_axis(tmp_path):
    assert lint_source(tmp_path, "\n".join([
        "ImVec2(metrics::px(4), 8);",
        "ImVec2(8, metrics::px(4));",
        "ImVec2(ImGui::GetFrameHeight(), 8);",
        "ImVec2(4 * ui::uiScale, 8);",
        "ImVec2(metrics::px(4), metrics::stroke(8));",
    ])) == {(1, "scale"), (2, "scale"), (3, "scale"), (4, "scale")}


def test_scaling_covers_only_enclosed_terms_not_adjacent_offsets(tmp_path):
    assert lint_source(tmp_path, "\n".join([
        "ImVec2(metrics::px(4) + 8, 0);",
        "ImVec2(8 + metrics::px(4), 0);",
        "ImVec2(ImGui::GetFrameHeight() + 8, 0);",
        "ImVec2(metrics::px(4 + 8), 0);",
        "ImVec2(ImGui::GetFrameHeight() * 3, 0);",
        "ImVec2(x + 8 + 2 * sc, 0);",
        "ImVec2(4 * ui::uiScale, ui::uiScale * 8);",
        "ImVec2(not_px(8), 0);",
    ])) == {(1, "scale"), (2, "scale"), (3, "scale"), (6, "scale"), (8, "scale")}


def test_namespace_aliases_and_grouped_dimensions_keep_scaling_local(tmp_path):
    assert lint_source(tmp_path, "\n".join([
        "ImVec2(M::px(120), M::px(80));",
        "dl->AddCircle(c, (dragging ? 7.0f : 5.0f) * sc, ink, 0, 1.5f * sc);",
        "ImVec2(sc * (hot ? 7 : 5), 0);",
        "ImVec2((hot ? 7 : 5) * sc + 8, 0);",
        "ImVec2((hot ? 7 : 5) + 8 * sc, 0);",
        "ImVec2((80 + 260) * uS, (80 + 400) * uS);",
        "ImVec2(g == 2 ? 24 * S : 0, 0);",
        "ImVec2(g == 2 ? 24 : 0, 0);",
    ])) == {(4, "scale"), (5, "scale"), (8, "scale")}


def test_other_rules_still_report_non_geometry_violations(tmp_path):
    assert lint_source(tmp_path, "\n".join([
        'getIcon("missing");',
        "applyTheme(style, 1.0f);",
        "dl->AddCircleFilled(center, metrics::px(6), IM_COL32(20, 30, 40, 255), 12);",
    ]), {"scale", "icons", "theme", "color"}) == {
        (1, "icons"), (2, "theme"), (3, "color"),
    }


def test_empty_scan_cannot_pass_or_erase_baseline(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    baseline = tmp_path / "baseline.json"
    previous = b'{"keys": ["preserved"], "count": 1}\n'
    baseline.write_bytes(previous)
    assert gui_lint.main([
        str(root), "--rule", "theme", "--update-baseline", str(baseline),
    ]) == 2
    assert baseline.read_bytes() == previous


def test_missing_root_cannot_hide_behind_a_valid_scan(tmp_path):
    valid = tmp_path / "valid"
    valid.mkdir()
    (valid / "panel.cpp").write_text("void draw() {}\n", encoding="utf-8")
    assert gui_lint.main([
        str(valid), str(tmp_path / "missing"), "--rule", "theme", "--no-baseline",
    ]) == 2
    assert gui_lint.main([str(valid), "--rule", "theme", "--no-baseline"]) == 0
