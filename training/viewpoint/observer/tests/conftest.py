import pytest

# Torch is a training-only dependency: the runtime venv the gate sweeps with has none, and the
# torch-free tests here (queries, cross-fit bookkeeping) must still collect there. Test modules
# that need torch skip themselves with pytest.importorskip.
try:
    import torch
except ImportError:  # pragma: no cover - depends on the interpreter running the suite
    torch = None


@pytest.fixture(autouse=True, scope="session")
def _single_threaded_cpu_torch():
    # This torch build (2.12, CPU kernels) aborts with heap corruption ("double free or
    # corruption") in multi-threaded CPU backward through ObserverNet; one intra-op thread is
    # exact and the test models are tiny. CUDA training is unaffected.
    if torch is None:
        yield
        return
    prev = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(prev)
