# services/installer — provisioning + packaging

Runtime provisioning, MCP client registration, release packaging.

## Entry points
- `python -m services.installer.provision --with-app-code --wheelhouse <dir>` provisions
  the managed runtime from the reviewed `vendor/python/RUNTIME_WHEELS.lock` and matching
  offline wheelhouse. For source development use `scripts/provision_venv.sh`.
- Follow the [two-phase Windows signing procedure](../../docs/reference/PACKAGING_WINDOWS.md#build)
  for `services.installer.build_release`; `--zip` is an optional signed-finalization output.
- `mcp_setup.bat` → `services/installer/setup_mcp_clients.py`
