import pytest
import torch


@pytest.fixture(autouse=True, scope="session")
def _single_threaded_cpu_torch():
    # This torch build (2.12, CPU kernels) aborts with heap corruption ("double free or
    # corruption") in multi-threaded CPU backward through ObserverNet; one intra-op thread is
    # exact and the test models are tiny. CUDA training is unaffected.
    prev = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(prev)
