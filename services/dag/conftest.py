"""conftest.py — services/dag is importable bare as 'dag' via the repo-root symlink
(dag -> services/dag, matching agent/deliverables/engine/installer/mcp_server).

T-V3: this used to `sys.path.insert(0, services_dir)` to fake the same effect — but
putting services/ itself on sys.path makes services/mcp/ resolve as a top-level `mcp`
package, shadowing the real installed mcp SDK ("mcp.server is not a package") for
every OTHER test collected afterward in the same session. The repo-root symlink gives
the same bare-`dag`-importability without that collision; nothing left to do here.
"""
