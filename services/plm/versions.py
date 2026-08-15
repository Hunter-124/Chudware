"""Named immutable version refs for the PLM vault."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from services.plm.vault import Vault, VaultError, _user_author

_REF_PREFIX = "refs/aicad/versions/"
_NUMERIC = re.compile(r"^V(\d+)$")


def _meta_path(vault: Vault) -> Path:
    return vault._bind.vault_dir / "versions" / "meta.json"


def _read_meta(vault: Vault) -> dict[str, Any]:
    path = _meta_path(vault)
    if not path.is_file():
        return {"versions": {}}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"versions": {}}
    versions = data.get("versions") if isinstance(data, dict) else None
    return {"versions": versions if isinstance(versions, dict) else {}}


def _write_meta(vault: Vault, data: dict[str, Any]) -> None:
    path = _meta_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")


def list_versions(vault: Vault) -> list[dict[str, Any]]:
    data = _read_meta(vault)["versions"]
    refs = vault._bind.list_refs(_REF_PREFIX)
    out: list[dict[str, Any]] = []
    for full_name, target in refs.items():
        name = full_name[len(_REF_PREFIX):]
        info = data.get(name, {})
        out.append({
            "name": name,
            "commit": target,
            "created_at": str(info.get("created_at") or ""),
            "author": str(info.get("author") or ""),
            "message": str(info.get("message") or ""),
        })
    return sorted(out, key=lambda v: (v["created_at"], v["name"]), reverse=True)


def tag_version(
    vault: Vault,
    commit: str | None = None,
    name: str | None = None,
    message: str = "",
) -> dict[str, Any]:
    versions = list_versions(vault)
    existing = {v["name"] for v in versions}
    if name is None or not str(name).strip():
        highest = max((int(m.group(1)) for n in existing if (m := _NUMERIC.match(n))), default=0)
        name = f"V{highest + 1}"
    else:
        name = str(name).strip()
    if name in existing:
        raise VaultError("already_exists", f"a version named {name!r} already exists")
    if "/" in name or name in (".", ".."):
        raise VaultError("invalid_mode", "version names cannot contain slashes")
    target = commit or vault._bind.head_commit()
    if not target:
        raise VaultError("not_found", "the vault has no commit to tag")
    try:
        vault._bind.commit_meta(target)
    except KeyError as exc:
        raise VaultError("not_found", f"no commit {target!r}") from exc
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    author = _user_author()["display"]
    item = {"name": name, "commit": target, "created_at": created_at,
            "author": author, "message": str(message or "")}
    data = _read_meta(vault)
    data["versions"][name] = item
    vault._bind.set_ref(_REF_PREFIX + name, target)
    _write_meta(vault, data)
    return item
