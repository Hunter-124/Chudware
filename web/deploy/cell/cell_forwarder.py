"""Fixed cell/actor loopback transports; only the cell's own Unix sockets are dialed."""
import socket
import threading
from cell_runtime import DEMO_PORT, DEMO_SOCKET, demo_requested, demo_socket_present, relay


def _start_lane(port, path, *, provider=False):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(32)
    except BaseException:
        listener.close()
        raise
    slots = threading.BoundedSemaphore(32)

    def forward(client):
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.settimeout(10)
            upstream.connect(path)
            # A demo model stream may legitimately pause between tokens, so it has no byte
            # cap; the front door owns request bounds, quota and revocation. A silent socket
            # is still reclaimed so a tenant cannot pin the lane's slots forever.
            relay(client, upstream, byte_limit=None if provider else 67108864,
                  idle=600 if provider else 120)
        except (OSError, ValueError):
            pass
        finally:
            upstream.close()
            client.close()
            slots.release()

    def accept():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            if slots.acquire(blocking=False):
                threading.Thread(target=forward, args=(client,), daemon=True).start()
            else:
                client.close()
    threading.Thread(target=accept, daemon=True).start()
    return listener


def demo_lane_enabled(environ=None, socket_path=DEMO_SOCKET):
    """The AI lane exists only in a demo container whose front door mounted its socket."""
    return demo_requested(environ) and demo_socket_present(socket_path)


def start(*, provider=None):
    """Bind the fixed lanes before launching any child; no caller endpoint input.

    ``provider`` None decides from the environment (demo container AND mounted socket);
    a single-operator cell therefore never opens the provider lane.
    """
    listeners = []
    try:
        listeners.append(_start_lane(18080, "/run/egress.sock"))
        if demo_lane_enabled() if provider is None else provider:
            listeners.append(_start_lane(DEMO_PORT, DEMO_SOCKET, provider=True))
        return listeners
    except BaseException:
        for listener in listeners:
            listener.close()
        raise
