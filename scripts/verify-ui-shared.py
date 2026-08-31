#!/usr/bin/env python3
"""Chudware side of the chud-ui-shared-v1 contract (assets/ui-shared-manifest.json).

Chudware and Chudcode share a product-UI *vocabulary* — semantic role names,
Tone/Density naming, animation duration tokens, usage thresholds, icon cell
sizes, and font provenance — never widget code, and never a cross-repo link.
Each product keeps an independent native implementation plus a byte-identical
checked-in copy of the manifest; this script verifies the Chudware copy against
the Chudware implementation:

  assets/ui-shared-manifest.json   byte-identical to the frozen Chudcode original
  host/src/ui_theme.h              kUiSharedSchema / contract version / usage thresholds
                                   + shared-name Palette roles
  host/src/ui/widgets/kit.h        Tone / Density enums
  host/src/ui/anim.h               duration tokens (kInstant .. kMarch)
  host/src/ui/icons.h              kIconCellSmall / kIconCellLarge
  assets/icons/                    atlas.json cell sizes, atlas textures, generators
  assets/fonts/                    six shared font files frozen by SHA-256 + OFL.txt

Stdlib only, script-relative (runs from any CWD), no sibling checkout and no
runtime dependency. Exit 0 on full agreement; exit 1 with one
"UI shared drift: <location>: expected <x>, got <y>" line per divergence.

Refreshing the contract is a deliberate two-repo act: land the new manifest in
Chudcode, copy it here byte-for-byte, update EXPECTED_MANIFEST_SHA256 and the
aligned constants, and rerun this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# SHA-256 of the frozen Chudcode assets/ui-shared-manifest.json this repo mirrors.
# The pin is what makes "byte-identical copy" checkable without a sibling checkout.
EXPECTED_MANIFEST_SHA256 = "6badb552d943981c97e852c7ecfaf885bbbfa465f2e3f3e7278cf4676f09301f"

# Chudware implementation surfaces (repo-relative).
THEME_HEADER = "host/src/ui_theme.h"
KIT_HEADER = "host/src/ui/widgets/kit.h"
ANIM_HEADER = "host/src/ui/anim.h"
ICONS_HEADER = "host/src/ui/icons.h"
ICON_ATLAS = "assets/icons/atlas.json"
ICON_TEXTURES = ("assets/icons/atlas_dark.png", "assets/icons/atlas_light.png")
ICON_GENERATORS = ("assets/make_icons.py", "assets/pack_icons.py")
FONTS_DIR = "assets/fonts"
FONT_LICENSE = "assets/fonts/OFL.txt"

# Manifest semanticRoles that Chudware's Palette (host/src/ui_theme.h) implements under the
# shared name verbatim, as `float <name>[4]` fields. The rest of the manifest vocabulary is
# either Chudcode-only surface (dag*, attachment*, scrollPill*, tab*, usage*, …) or carried
# by Chudware under product-owned names (accent2 = secondary, frameBgHovered = frameHovered,
# selectionHighlight = accentSelection, chipBg = accentChip, treeHover = accentTreeHover, …).
# Growing this list is welcome; shrinking or renaming it is contract drift.
SHARED_PALETTE_FIELDS = (
    "windowBg", "panelBg", "headerBg", "popupBg", "statusBg", "border",
    "text", "textDim", "textDisabled",
    "button", "buttonHovered", "buttonActive", "frameBg",
    "accent", "accentDim", "accentSoft", "accentMid",
    "codeBg", "proseBg", "proseText",
    "statusOk", "statusWarn", "statusErr",
    "glassBg", "glassBorder", "glassHighlight", "glassShadow",
)

# manifest.animationSeconds key -> host/src/ui/anim.h token. maxDeltaTime is deliberately
# absent: Chudware's frame pacing has no delta-time clamp constant (ImGui's io.DeltaTime is
# consumed as-is), so the concept is not shared and is not checked.
ANIMATION_TOKENS = {
    "instant": "kInstant",
    "fast": "kFast",
    "base": "kBase",
    "slow": "kSlow",
    "stagger": "kStagger",
    "breathe": "kBreathe",
    "march": "kMarch",
}

# Role-name prefixes that would smuggle CAD-domain vocabulary into the shared contract.
CAD_DOMAIN_PREFIXES = (
    "viewport", "plane", "sketch", "sim", "flow", "cam", "fit", "plan", "parts", "ribbon",
)

LICENSE_MARKERS = (
    "SIL OPEN FONT LICENSE Version 1.1",
    "The Rubik Project Authors",
    "The JetBrains Mono Project Authors",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _strip_line_comments(source: str) -> str:
    return re.sub(r"//[^\n]*", "", source)


def _cpp_enum(source: str, name: str) -> list[str]:
    match = re.search(rf"enum\s+class\s+{name}\s*(?::\s*\w+\s*)?\{{([^}}]*)\}}",
                      _strip_line_comments(source))
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def _cpp_number(source: str, name: str) -> float | None:
    match = re.search(rf"\b{name}\s*=\s*(-?\d+(?:\.\d+)?)f?\b", source)
    return float(match.group(1)) if match else None


def _cpp_string(source: str, name: str) -> str | None:
    match = re.search(rf'\b{name}\s*=\s*"([^"]*)"', source)
    return match.group(1) if match else None


class Verifier:
    def __init__(self, root: Path, manifest_path: Path) -> None:
        self.root = root
        self.manifest_path = manifest_path
        self.failures: list[str] = []

    def drift(self, location: str, expected: object, actual: object) -> None:
        self.failures.append(
            f"{location}: expected {json.dumps(expected)}, got {json.dumps(actual)}")

    def expect_equal(self, location: str, expected: object, actual: object) -> None:
        if expected != actual:
            self.drift(location, expected, actual)

    def expect_unique(self, location: str, values: list[str]) -> None:
        duplicates = sorted({v for v in values if values.count(v) > 1})
        if duplicates:
            self.drift(location, "unique values", duplicates)

    def read_required(self, rel: str) -> str:
        path = self.root / rel
        if not path.is_file():
            self.drift(rel, "existing file", "missing")
            return ""
        return path.read_text(encoding="utf-8")

    # ---- manifest ---------------------------------------------------------------------------
    def load_manifest(self) -> dict | None:
        if not self.manifest_path.is_file():
            self.drift("manifest", "existing file", "missing")
            return None
        if self.manifest_path.is_symlink():
            self.drift("manifest.storage", "regular checked-in file", "symbolic link")
        actual = _sha256(self.manifest_path)
        if actual != EXPECTED_MANIFEST_SHA256:
            self.drift("manifest.sha256 (byte-identity with the Chudcode original)",
                       EXPECTED_MANIFEST_SHA256, actual)
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            self.drift("manifest", "valid JSON", str(error))
            return None

    def verify_manifest_shape(self, manifest: dict) -> None:
        self.expect_equal("schema", "chud-ui-shared-v1", manifest.get("schema"))
        self.expect_equal("contractVersion", 1, manifest.get("contractVersion"))
        roles = manifest.get("semanticRoles", [])
        self.expect_unique("semanticRoles", roles)
        self.expect_unique("tones", manifest.get("tones", []))
        self.expect_unique("densities", manifest.get("densities", []))
        for role in roles:
            matched = next(
                (p for p in CAD_DOMAIN_PREFIXES if role.lower().startswith(p)), None)
            if matched:
                self.drift(f"semanticRoles.{role}", "general cross-product role",
                           f"CAD-domain prefix {matched}")
        thresholds = manifest.get("usageThresholds", {})
        warning = thresholds.get("warning")
        critical = thresholds.get("critical")
        if not (isinstance(warning, (int, float)) and isinstance(critical, (int, float))
                and 0 <= warning < critical <= 1):
            self.drift("usageThresholds", "0 <= warning < critical <= 1", thresholds)
        for role in manifest.get("surfaceRoleSemantics", {}):
            if role not in roles:
                self.drift(f"surfaceRoleSemantics.{role}", "declared semantic role", "missing")
        for font in manifest.get("fonts", {}).get("files", []):
            path = font.get("path", "")
            if Path(path).is_absolute() or ".." in re.split(r"[\\/]", path):
                self.drift(f"fonts.{font.get('file')}.path", "repository-relative path", path)

    # ---- theme / kit / anim / icons headers -------------------------------------------------
    def verify_theme(self, manifest: dict) -> None:
        theme = self.read_required(THEME_HEADER)
        kit = self.read_required(KIT_HEADER)
        anim = self.read_required(ANIM_HEADER)
        icons = self.read_required(ICONS_HEADER)

        self.expect_equal("theme.kUiSharedSchema", manifest.get("schema"),
                          _cpp_string(theme, "kUiSharedSchema"))
        self.expect_equal("theme.kUiSharedContractVersion", manifest.get("contractVersion"),
                          _cpp_number(theme, "kUiSharedContractVersion"))

        thresholds = manifest.get("usageThresholds", {})
        self.expect_equal("theme.kUsageWarnFrac", thresholds.get("warning"),
                          _cpp_number(theme, "kUsageWarnFrac"))
        self.expect_equal("theme.kUsageCritFrac", thresholds.get("critical"),
                          _cpp_number(theme, "kUsageCritFrac"))

        roles = manifest.get("semanticRoles", [])
        for field in SHARED_PALETTE_FIELDS:
            if field not in roles:
                self.drift(f"theme.Palette.{field}", "declared manifest semantic role",
                           "not in manifest")
            if not re.search(rf"\bfloat\s+{field}\s*\[4\]", theme):
                self.drift(f"theme.Palette.{field}", "float[4] Palette field", "missing")

        self.expect_equal("kit.Tone", manifest.get("tones"), _cpp_enum(kit, "Tone"))
        self.expect_equal("kit.Density", manifest.get("densities"), _cpp_enum(kit, "Density"))

        animation = manifest.get("animationSeconds", {})
        for manifest_name, cpp_name in ANIMATION_TOKENS.items():
            self.expect_equal(f"anim.{cpp_name}", animation.get(manifest_name),
                              _cpp_number(anim, cpp_name))

        cell_sizes = sorted(manifest.get("iconPipeline", {}).get("cellSizes", []))
        if len(cell_sizes) == 2:
            self.expect_equal("icons.kIconCellSmall", cell_sizes[0],
                              _cpp_number(icons, "kIconCellSmall"))
            self.expect_equal("icons.kIconCellLarge", cell_sizes[1],
                              _cpp_number(icons, "kIconCellLarge"))
        else:
            self.drift("iconPipeline.cellSizes", "two shared cell sizes", cell_sizes)

    # ---- assets -----------------------------------------------------------------------------
    def verify_assets(self, manifest: dict) -> None:
        fonts = manifest.get("fonts", {})
        self.expect_equal("fonts.license.spdx", "OFL-1.1",
                          fonts.get("license", {}).get("spdx"))
        license_text = self.read_required(FONT_LICENSE)
        for marker in LICENSE_MARKERS:
            if marker not in license_text:
                self.drift(f"fonts.license.text:{marker}", "present", "missing")

        for font in fonts.get("files", []):
            name = font.get("file", "")
            path = self.root / FONTS_DIR / name
            if not path.is_file():
                self.drift(f"fonts.{name}", font.get("sha256"), "missing")
                continue
            if path.is_symlink():
                self.drift(f"fonts.{name}.storage", "regular vendored file", "symbolic link")
                continue
            self.expect_equal(f"fonts.{name}.sha256", font.get("sha256"), _sha256(path))

        for rel in ICON_GENERATORS:
            if not (self.root / rel).is_file():
                self.drift(rel, "existing icon-pipeline generator", "missing")
        for rel in ICON_TEXTURES:
            if not (self.root / rel).is_file():
                self.drift(rel, "existing atlas texture", "missing")

        atlas_text = self.read_required(ICON_ATLAS)
        if not atlas_text:
            return
        try:
            atlas = json.loads(atlas_text)
        except json.JSONDecodeError as error:
            self.drift("iconPipeline.atlas", "valid JSON", str(error))
            return
        sizes = set(manifest.get("iconPipeline", {}).get("cellSizes", []))
        for key, cell in atlas.get("cell", {}).items():
            base, _, suffix = key.rpartition("@")
            if not base or not suffix.isdigit() or int(suffix) not in sizes:
                self.drift(f"iconPipeline.atlas.cell.{key}",
                           f"name@size key with size in {sorted(sizes)}", key)
                continue
            size = int(suffix)
            if not (isinstance(cell, dict) and cell.get("w") == size and cell.get("h") == size):
                self.drift(f"iconPipeline.atlas.cell.{key}", {"w": size, "h": size},
                           {"w": cell.get("w"), "h": cell.get("h")}
                           if isinstance(cell, dict) else cell)

    # ---- driver -----------------------------------------------------------------------------
    def run(self) -> list[str]:
        manifest = self.load_manifest()
        if manifest is not None:
            self.verify_manifest_shape(manifest)
            self.verify_theme(manifest)
            self.verify_assets(manifest)
        return self.failures


def collect_failures(root: Path, manifest_path: Path | None = None) -> list[str]:
    root = root.resolve()
    return Verifier(root, manifest_path or root / "assets" / "ui-shared-manifest.json").run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_root = Path(__file__).resolve().parent.parent
    parser.add_argument("--root", type=Path, default=default_root,
                        help="repository root (default: this script's repo)")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="manifest path (default: <root>/assets/ui-shared-manifest.json)")
    args = parser.parse_args(argv)
    failures = collect_failures(args.root, args.manifest)
    if failures:
        for failure in failures:
            print(f"UI shared drift: {failure}", file=sys.stderr)
        return 1
    manifest = json.loads((args.manifest or args.root / "assets" /
                           "ui-shared-manifest.json").read_text(encoding="utf-8"))
    print(f"UI shared contract verified: {manifest['schema']} "
          f"({len(manifest['semanticRoles'])} roles, {len(manifest['fonts']['files'])} fonts, "
          f"{len(SHARED_PALETTE_FIELDS)} shared palette names)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
