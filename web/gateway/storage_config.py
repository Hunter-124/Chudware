"""Credential-independent canonical gateway storage selection and raw paths."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    from .deployment_config import ConfigurationError, read_environment_file
except ImportError:
    from deployment_config import ConfigurationError, read_environment_file


def expand_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def data_root(environment: str, values: Mapping[str, str] | None = None) -> Path:
    return expand_path(raw_data_root(environment, values))


def raw_data_root(environment: str, values: Mapping[str, str] | None = None) -> str:
    values = os.environ if values is None else values
    home = "chudware-web-staging" if environment == "staging" else "chudware-web-data"
    return values.get("CHUDWARE_WEB_DATA_ROOT") or f"~/{home}"


def webd_env_path(environment: str, values: Mapping[str, str]) -> Path:
    home = "chudware-web-staging" if environment == "staging" else "chudware-web"
    return Path(values.get("CHUDWARE_WEBD_ENV", f"~/{home}/webd.env")).expanduser()


def load_webd_env(environment: str, values: Mapping[str, str] | None = None) -> dict[str, str]:
    values = os.environ if values is None else values
    path = webd_env_path(environment, values)
    try:
        result, _, _ = read_environment_file(path, private=False)
    except FileNotFoundError:
        return {}
    return result


@dataclass(frozen=True)
class StorageRoots:
    roots: dict[str, Path]
    raw_paths: dict[str, Path]


def configured_storage(environment: str, values: Mapping[str, str] | None = None, *,
                       data: Path | None = None, workspace: Path | None = None,
                       bucket: Path | None = None, cells: Path | None = None,
                       university: str | None = None,
                       discovery: Mapping[str, str] | None = None) -> StorageRoots:
    """Select the existing defaults once; retain pre-resolution backup authority."""
    if environment not in {"prod", "staging"}:
        raise ConfigurationError("unknown_deployment_environment")
    values = os.environ if values is None else values
    staging = environment == "staging"
    raw_data = raw_data_root(environment, values)
    selected_data = data if data is not None else expand_path(raw_data)
    if data is not None and selected_data != expand_path(raw_data):
        raw_data = str(selected_data)
    selected_workspace = workspace if workspace is not None and str(workspace) not in {".", ""} else None
    if selected_workspace is None:
        if values.get("CHUDWARE_WORKSPACE_ROOT"):
            raw_workspace = values["CHUDWARE_WORKSPACE_ROOT"]
        else:
            discovery = load_webd_env(environment, values) if discovery is None else discovery
            raw_workspace = discovery.get("CHUDWARE_WORKSPACE_ROOT") or (
                str(selected_data / "workspace") if staging else
                "~/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build"
            )
        selected_workspace = expand_path(raw_workspace)
    else:
        raw_workspace = str(selected_workspace)
    raw_bucket = str(bucket) if bucket is not None and str(bucket) not in {".", ""} else (
        values.get("CHUDWARE_WEB_BUCKET_ROOT") or (str(selected_data / "bucket") if staging else "~/ME489")
    )
    raw_cells = str(cells) if cells is not None and str(cells) not in {".", ""} else (
        values.get("CHUDWARE_CELLS_ROOT") or ("~/chudware-web-staging/cells" if staging else "~/chudware-web/cells")
    )
    roots = {"data": selected_data, "workspace": selected_workspace,
             "bucket": expand_path(raw_bucket), "cells": Path(raw_cells).expanduser().absolute()}
    raw_paths = {"data": Path(raw_data).expanduser(), "workspace": Path(raw_workspace).expanduser(),
                 "bucket": Path(raw_bucket).expanduser(), "cells": Path(raw_cells).expanduser()}
    raw_university = values.get("CHUDWARE_WEB_UNIVERSITY_WORKSPACE_ROOT", "") if university is None else university
    if raw_university:
        # Existing institutional roots remain raw until the consuming boundary checks them.
        roots["university"] = Path(raw_university).expanduser()
        raw_paths["university"] = roots["university"]
    if staging:
        resolved_data = selected_data.resolve()
        for label in ("workspace", "bucket", "university"):
            if label in roots and not roots[label].resolve().is_relative_to(resolved_data):
                raise ConfigurationError("staging_" + label + "_outside_data_root")
    return StorageRoots(roots, raw_paths)
