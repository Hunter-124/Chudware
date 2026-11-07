"""Environment the web gateway reads ONCE, at import, set before any web test module can import it.

``web/deploy/tests`` imports ``gateway`` while pytest collects, which can happen before
``web/gateway/tests/conftest.py`` is loaded (the ``platform`` suite runs demo, deploy and gateway
tests in one session). The gateway's settings would then freeze without the test host name and
every gateway request would answer 400. Loading this file first fixes the order.
"""

from __future__ import annotations

import os
import tempfile

if "CHUDWARE_WEB_TEST_ENV" not in os.environ:
    os.environ["CHUDWARE_WEB_TEST_ENV"] = "1"
    os.environ["CHUDWARE_WEB_HOME"] = tempfile.mkdtemp(prefix="cw-gw-import-")
    os.environ["CHUDWARE_WEB_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
    for _name in ("CHUDWARE_WORKSPACE_ROOT", "CHUDWARE_WEB_APP", "CHUDWARE_WEB_OPERATOR", "CHUDWARE_WEB_DEMO"):
        os.environ.pop(_name, None)
