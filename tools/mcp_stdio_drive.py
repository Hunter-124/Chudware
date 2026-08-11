"""Drive the Chudware MCP server over real stdio like the desktop app does: initialize, then
call tools, measuring latency and asserting replies actually arrive (the -32001 repro)."""
import json, os, subprocess, sys, time, threading, queue

VENV = os.path.join(os.environ.get("APPDATA", ""), "Chudware", "runtime", "venv", "Scripts", "python.exe")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = dict(os.environ, PYTHONPATH=REPO)

p = subprocess.Popen([VENV, "-m", "services.mcp.server"], cwd=REPO,
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, env=env, bufsize=0)

q = queue.Queue()
def reader():
    for line in p.stdout:
        q.put(line)
threading.Thread(target=reader, daemon=True).start()

def send(obj):
    p.stdin.write((json.dumps(obj) + "\n").encode()); p.stdin.flush()

def wait_id(want, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            line = q.get(timeout=timeout)
        except queue.Empty:
            break
        s = line.decode(errors="replace").strip()
        if not s:
            continue
        try:
            o = json.loads(s)
        except json.JSONDecodeError:
            print(f"  !! NON-JSON on stdout (pollution): {s[:120]!r}")
            return None, time.time() - t0
        if o.get("id") == want:
            return o, time.time() - t0
    return None, time.time() - t0

ok = True
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "drive", "version": "1"}}})
r, dt = wait_id(1, 30)
print(f"initialize -> {'OK' if r else 'TIMEOUT'} ({dt:.2f}s)")
ok &= r is not None
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

for cid, name, args in [(2, "new_part", {"name": "main_wing"}),
                        (3, "list_standard_parts", {}),
                        (4, "new_part", {"name": "second"})]:
    send({"jsonrpc": "2.0", "id": cid, "method": "tools/call",
          "params": {"name": name, "arguments": args}})
    r, dt = wait_id(cid, 120)
    if r is None:
        print(f"{name} -> TIMEOUT after {dt:.1f}s"); ok = False; continue
    err = r.get("error")
    isErr = bool(err) or (r.get("result", {}).get("isError"))
    print(f"{name} -> {'ERROR' if isErr else 'OK'} ({dt:.2f}s)"
          + (f"  {err}" if err else ""))
    ok &= not isErr

send({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
r, dt = wait_id(9, 10)
print(f"tools/list -> {len(r['result']['tools']) if r else '?'} tools")
p.terminate()
print("RESULT:", "ALL_OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
