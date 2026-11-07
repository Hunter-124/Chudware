"""Chudware public demo front door: queue, per-session containers, AI broker.

See README.md in this directory. The front door is a host-side process (not part of
the container image): it admits at most a few visitors at a time, runs each one's CAD
daemon in a throwaway rootless podman container, reverse-proxies the browser to it,
and brokers free-tier OpenRouter access over a per-session unix socket.
"""
