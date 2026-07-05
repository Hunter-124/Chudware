"""Engine-oracle engineering calculator registry (Pillar 20 / T-W4).

Importing this package registers all calculators into the global registry.
Use :func:`registry.run_calc` and :func:`registry.list_calcs` to dispatch.
"""
# Import all calculator modules to trigger their ``register()`` calls.
from . import beam            # noqa: F401
from . import bolted_joint    # noqa: F401
from . import thread_engagement  # noqa: F401
from . import press_fit       # noqa: F401
from . import wall_thickness  # noqa: F401
from . import snap_fit        # noqa: F401

from .registry import run_calc, list_calcs, get_calc, calc_envelope, calc_fail  # noqa: F401
