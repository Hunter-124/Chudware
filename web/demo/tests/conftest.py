"""Front-door tests: no podman, no network, no real OpenRouter.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Import the front door only as the ``demo`` package, like the gateway tests do for
# ``gateway``, so every test module shares one module object per file.
WEB_DIR = Path(__file__).resolve().parents[2]
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))
