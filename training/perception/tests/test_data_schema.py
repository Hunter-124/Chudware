import json
from pathlib import Path

import pytest

from services.perception.config import ModelSpec
from training.perception.data import WINDOW_SCHEMA
from training.perception.synthetic import generate_synthetic_corpus


def test_checked_in_window_schema_matches_runtime_contract():
    fixture = json.loads(Path("fixtures/mfg/perception/window_schema.json").read_text(encoding="utf-8"))
    assert fixture == WINDOW_SCHEMA


def test_window_batch_rejects_shape_and_mask_contract_violations():
    spec = ModelSpec()
    batch = generate_synthetic_corpus(spec=spec, windows=8, seed=88).windows
    invalid_mask = batch.cam_mask.copy()
    invalid_mask[0, 0, 0] = 2
    broken = type(batch)(
        cam_feat=batch.cam_feat, cam_mask=invalid_mask, cam_age=batch.cam_age,
        proc_feat=batch.proc_feat, proc_mask=batch.proc_mask, proc_age=batch.proc_age,
        cmd_future=batch.cmd_future, cmd_mask=batch.cmd_mask,
        target_latent=batch.target_latent, event_label=batch.event_label,
        window_ids=batch.window_ids,
    )
    with pytest.raises(ValueError, match="cam_mask must be binary"):
        broken.validate(spec)
