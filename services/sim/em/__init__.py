"""Full-wave electromagnetic simulation family (eigenmode / driven / time-domain).

Pluggable solver adapters (openEMS FDTD primary; builtin FDFD + analytic always
available; Gmsh+GetDP / Meep / Elmer scaffolded) behind the em_* worker RPCs.

License note: GPL solvers (openEMS, Meep, GetDP) are only ever imported lazily
inside their adapter, in the separate Python sim-worker process — never at
package import time, never linked into the C++ core.  See
contracts/license_debt.md and docs/reference/EM_SIMULATION.md.
"""

from .adapters import EmAdapter, EmSolverError, registry, select_adapter  # noqa: F401
from .worker_rpc import WORKER_METHODS  # noqa: F401
