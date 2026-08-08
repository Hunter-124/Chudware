import copy
import json
from pathlib import Path

import pytest

from training.perception.manifest import load_manifest, validate_manifest


FIXTURE = Path("fixtures/mfg/perception/manifest.json")


def _record():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))[0]


def test_valid_manifest_loads_with_rights_and_retention_fields():
    records = load_manifest(FIXTURE)
    assert len(records) == 1
    rights = records[0].values
    assert rights["license"] == "synthetic"
    assert rights["consent_ref"]
    assert rights["license_ref"]
    assert isinstance(rights["redaction_applied"], bool)
    assert rights["retention_class"]


@pytest.mark.parametrize("missing", ["license", "license_ref", "consent_ref", "redaction_applied", "retention_class"])
def test_manifest_rejects_missing_rights_fields(missing):
    record = _record()
    del record[missing]
    with pytest.raises(ValueError, match=missing):
        validate_manifest([record])


def test_manifest_rejects_group_leakage_and_synthetic_test_data():
    train = _record()
    leaked = copy.deepcopy(train)
    leaked["clip_id"] = "clip-2"
    leaked["split"] = "calib"
    with pytest.raises(ValueError, match="synthetic records may appear only in train"):
        validate_manifest([train, leaked])
