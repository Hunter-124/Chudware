"""Fixed cell/actor loopback egress transport; only the cell's own Unix socket is dialed."""
import socket
import threading
from cell_runtime import relay


def _start_lane(port, path):
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
            relay(client, upstream, byte_limit=67108864, idle=120)
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


def start():
    """Bind the fixed egress lane before launching any child; no caller endpoint input."""
    return [_start_lane(18080, "/run/egress.sock")]
