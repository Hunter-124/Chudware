#!/usr/bin/env python3
"""Generate a deterministic production-only prompt and role asset table.

The output is a Nuitka build intermediate and must never be copied into a
customer payload.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ValueError(f"unhashable YAML mapping key at line {key_node.start_mark.line + 1}") from exc
        if duplicate:
            raise ValueError(
                f"duplicate YAML mapping key {key!r} at line {key_node.start_mark.line + 1}"
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _regular_files(root: Path, suffix: str) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"asset directory is missing or is a symlink: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob(f"*{suffix}"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError(f"asset symlinks are forbidden: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid asset path: {relative}")
        files.append(path)
    return files


def _canonical_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"asset is not UTF-8: {path}") from exc
    if "\x00" in text:
        raise ValueError(f"asset contains a NUL byte: {path}")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def _canonical_yaml(path: Path) -> str:
    text = _canonical_text(path)
    try:
        value = yaml.load(text, Loader=_UniqueKeyLoader)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot canonicalize YAML asset {path}: {exc}") from exc


def _python_dict(name: str, rows: dict[str, str]) -> str:
    body = "\n".join(
        f"    {path!r}: {text!r}," for path, text in rows.items()
    )
    return f"{name} = {{\n{body}\n}}\n"


def _render_python(
    prompts: dict[str, str], cheatsheets: dict[str, str], roles: dict[str, str]
) -> str:
    return (
        '"""Generated protected assets. Build input only; never ship this source."""\n\n'
        "SCHEMA = 1\n\n"
        + _python_dict("PROMPTS", prompts)
        + "\n"
        + _python_dict("CHEATSHEETS", cheatsheets)
        + "\n"
        + _python_dict("ROLES", roles)
    )


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def generate(source_root: Path, output_dir: Path) -> Path:
    source_root = source_root.resolve(strict=True)
    prompts_root = source_root / "services" / "agent" / "prompts"
    roles_root = source_root / "services" / "agent" / "roles"

    prompts: dict[str, str] = {}
    cheatsheets: dict[str, str] = {}
    for path in _regular_files(prompts_root, ".md"):
        relative = path.relative_to(prompts_root).as_posix()
        target = cheatsheets if relative.startswith("shared/") else prompts
        target[relative] = _canonical_text(path)
    roles = {
        path.relative_to(roles_root).as_posix(): _canonical_yaml(path)
        for path in _regular_files(roles_root, ".yaml")
    }
    if not prompts or not cheatsheets or not roles:
        raise ValueError("protected asset inputs must contain prompts, cheatsheets, and roles")

    python_path = output_dir / "python" / "chudware_protected_assets.py"
    _write_atomic(python_path, _render_python(prompts, cheatsheets, roles))
    return python_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: parent of packaging/)",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    python_path = generate(args.source_root, args.output_dir)
    print(python_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
