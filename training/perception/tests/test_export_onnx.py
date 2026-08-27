import numpy as np
import pytest

from services.perception.reference import NumpyMLDP
from training.perception.export_onnx import export_onnx, run_onnx
from training.perception.synthetic import generate_synthetic_corpus


def test_onnx_matches_numpy_reference_when_optional_stack_is_installed(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    # torch's ONNX exporter imports onnxscript (torch/onnx/_internal/exporter);
    # without it export_onnx raises ModuleNotFoundError. The optional stack is
    # only "installed" when every piece the export path uses is present, so
    # guard it here rather than reporting a missing optional dependency as a
    # product failure.
    pytest.importorskip("onnxscript")
    corpus = generate_synthetic_corpus(windows=8, seed=29)
    one = corpus.windows.subset(np.array([0]))
    inputs = one.model_inputs()
    reference = NumpyMLDP(corpus.spec, seed=31)
    expected = reference.forward(inputs)
    graph = export_onnx(reference, inputs, tmp_path / "mldp_core.onnx")
    actual = run_onnx(graph, inputs)
    for name in expected:
        np.testing.assert_allclose(actual[name], expected[name], rtol=1e-5, atol=1e-5)
