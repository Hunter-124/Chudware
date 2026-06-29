# services/installer — provisioning + packaging

Runtime provisioning, MCP client registration, release packaging.

## Entry points
- `python -m installer.provision --with-app-code`
- `python -m installer.build_release --zip`
- `mcp_setup.bat` → `installer/setup_mcp_clients.py`

## Migration
- Stays as Python tooling until zero-Python closeout (N6); may become a thin
  shell script over the C++ binary at that point.
