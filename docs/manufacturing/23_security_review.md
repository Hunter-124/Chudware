# Manufacturing security review — 2026-08-08

Read-only audit of the manufacturing/perception attack surface. Nothing was executed;
every claim is code-reading evidence with `file:line`. All 16 findings were assigned and
are being fixed; this file is the coverage baseline and the record of what was NOT audited.

```json
{
  "coverage_summary": "Read-only security audit of the new manufacturing/perception attack surface. No source file was modified and nothing was executed. This session has no write tool, so `local://manufacturing-security-review.md` could not be created here; the full ranked report was delivered to Main over hub in four plain-text messages (F1-F16, the hardened vs not-hardened split, and the limits), and the same content is reproduced structurally in `findings` below. Main confirmed receipt and has assigned all 16 findings to follow-up slices.\n\nScope reviewed: core/hostd/mfg_lane.{h,cpp}; services/manufacturing/gateway/**; services/manufacturing/printers/**; services/manufacturing/notify/**; services/manufacturing/monitor/**; services/mcp/mfg_tools.py; services/perception/capture/**. Read for evidence only: core/hostd/server.cpp (dispatch order), core/device/grbl_adapter.cpp (the parser every gateway transport feeds), core/manufacturing/lease.h, services/worker_main.py, services/mcp/server.py, services/mcp/test_mfg_tools.py.\n\nTop three by exploitability: (F1) the commander lease is an unbound bearer token that mfg_device_list publishes to any unauthenticated reader while requireLease compares only the id string, so an MCP agent can hijack a human seat's lease and reach hold/abort/release; (F2) mfg_artifact_register accepts an arbitrary filesystem path, giving a daemon-privilege file-read oracle plus exfiltration through the printer upload path; (F3) GrblAdapter::processBytes accumulates inbound bytes with no maximum line length, so any serial/TCP/WebSocket peer can exhaust daemon memory and write attacker-chosen content into the durable journal.\n\nHARDENED (no finding): MTConnect XML decoding (4 MiB byte cap, DTD/ENTITY rejected before parse, 100k element cap, namespace+version allowlist, transport-free and pinned by test_mtconnect.py:127-130); C++ WebSocket framing (mask/RSV/opcode/canonical-length/control-size checks, per-frame and per-message ceilings with a provably non-wrapping fragment subtraction, UTF-8 and close-code validation, 16 KiB handshake cap, strict accept-key check, extensions and subprotocols refused); allowlist file parsing and atomic 0600 persistence; allowlist matching semantics (deny-by-default holds, transports re-authorize at open, and every spoofing spelling tried — case, trailing dot, unicode, 127.1, 0x7f.0.0.1, userinfo, bracketed IPv6 — fails closed); worker authority stripping (a worker or printer cannot assert seq, approval, lease or capability; the arbiter stays the only approval validator; the motion-class deny and default-off manufacture family are pinned by tests, so an agent cannot reach a motion tool with the family disabled); printer-path secret handling (redactor over payloads, error excerpts and exceptions, no credentials in base_url, no redirects, no secret in any log, event, journal row, webhook body or capture artifact); capture storage path handling (hash filenames, no traversal).\n\nNOT HARDENED: inbound Grbl line assembly (F3); the raw TCP drain loop (F9); printer HTTP body size and its eightfold journal amplification (F4); POSIX serial device identity (F6); mfg_artifact_register file input (F2); the lane's authorization model — lease bearer token (F1) and the entirely unauthenticated attention/journal surface (F13); outbound notify egress — SSRF, redirects, macOS script interpolation (F7, F8, F14), latent only because DeliveryManager is wired nowhere outside tests and must not be wired as-is; printer-supplied remote_path used as a URL path segment (F10); capture clip integrity on replay (F12).\n\nLIMITS: nothing was executed, so F1/F2/F3 exploitability is reasoned from the dispatch path (server.cpp:496-529 vs :805-812) rather than demonstrated; I could not determine whether the manufacture MCP family is enabled in any deployment (it is off by default); slicer/**, robot/**, pnp/**, metrology/** were outside the assignment and were not audited, and slicer/sidecar.py:322-325 spawns subprocesses and deserves the same treatment.",
  "findings": [
    {
      "rule_id": "MFG-LEASE-BEARER",
      "title": "Commander lease is an unbound bearer token published to every reader",
      "summary": "requireLease compares only the lease_id string and never the caller's LeaseOwner, while mfg_device_list returns that lease_id to any unauthenticated caller and lease grants are broadcast to all clients. An MCP agent can read a human seat's lease_id and then satisfy every lease-gated write, including hold, abort and release.",
      "severity": "high",
      "confidence": "high",
      "category": "authorization",
      "locations": [
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 497,
          "end_line": 514,
          "role": "broken control"
        },
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 975,
          "end_line": 1004,
          "role": "disclosure of the bearer token"
        },
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 1062,
          "end_line": 1064,
          "role": "broadcast to all clients"
        },
        {
          "path": "services/mcp/mfg_tools.py",
          "start_line": 33,
          "end_line": 40,
          "role": "agent-reachable source"
        },
        {
          "path": "core/manufacturing/lease.h",
          "start_line": 52,
          "end_line": 59,
          "role": "unused owner-checked API"
        }
      ],
      "cwe": [
        "CWE-639",
        "CWE-863",
        "CWE-200"
      ],
      "evidence": [
        {
          "label": "lease check ignores owner",
          "explanation": "Only the id is compared; no owner or caller identity is consulted, and hostd never passes client identity into the lane.",
          "excerpt": "const auto active = leases_.describe(device_id_.str(), clock_.mono_ns());\nif (!active || active->lease_id != lease_id) { refusal = refuse(\"lease.commander_held_by_caller\", ...); return false; }"
        },
        {
          "label": "token is published",
          "explanation": "leaseJson(*lease) includes lease_id and is returned in every mfg_device_list row for the selected device, a call that requires no lease.",
          "excerpt": "{\"lease\", lease ? leaseJson(*lease) : json::object()}"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Pass the caller's LeaseOwner into the lane and use leases_.held_by(device_id, owner, now); omit lease_id from mfg_device_list rows for non-owners."
      }
    },
    {
      "rule_id": "MFG-ARTIFACT-ARBITRARY-READ",
      "title": "mfg_artifact_register reads an arbitrary filesystem path at daemon privilege",
      "summary": "The artifact object accepts a `path` opened with no root confinement, traversal check or symlink handling. The bytes are hashed and returned as a size/hash oracle, and for printer machine classes are base64-uploaded to the commissioned printer, giving an exfiltration channel. The MCP tool passes the dict through verbatim.",
      "severity": "high",
      "confidence": "high",
      "category": "path traversal / information disclosure",
      "locations": [
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 1238,
          "end_line": 1250,
          "role": "dangerous sink"
        },
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 1479,
          "end_line": 1495,
          "role": "exfiltration path"
        },
        {
          "path": "services/mcp/mfg_tools.py",
          "start_line": 92,
          "end_line": 103,
          "role": "agent-reachable source"
        }
      ],
      "cwe": [
        "CWE-22",
        "CWE-73",
        "CWE-200"
      ],
      "evidence": [
        {
          "label": "unconstrained open",
          "explanation": "Any path the daemon user can read is accepted; the whole file is slurped and hashed.",
          "excerpt": "} else if (input.contains(\"path\")) {\n    std::ifstream stream(input.at(\"path\").get<std::string>(), std::ios::binary);"
        },
        {
          "label": "content leaves the host",
          "explanation": "On run start for a printer, the registered program lines are base64-encoded and uploaded to the printer endpoint.",
          "excerpt": "{{\"adapter_id\", device_id_.str()}, {\"artifact_b64\", base64Encode(content)}, ..."
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Remove the `path` source (content/lines already cover the use case) or confine it to one configured artifact root with weakly_canonical, a size cap and symlink rejection."
      }
    },
    {
      "rule_id": "MFG-GRBL-UNBOUNDED-LINE",
      "title": "GrblAdapter accumulates inbound bytes with no maximum line length",
      "summary": "processBytes appends every received byte to input_bytes_ and only trims on a CR/LF, so a peer that never sends a terminator grows the buffer without bound and exhausts daemon memory. The rx_capacity limit is applied only to outbound program lines. Reachable from serial, TCP and WebSocket transports.",
      "severity": "high",
      "confidence": "high",
      "category": "denial of service / parser hardening",
      "locations": [
        {
          "path": "core/device/grbl_adapter.cpp",
          "start_line": 308,
          "end_line": 319,
          "role": "unbounded allocation"
        },
        {
          "path": "core/device/grbl_adapter.cpp",
          "start_line": 118,
          "end_line": 122,
          "role": "outbound-only limit"
        },
        {
          "path": "services/manufacturing/gateway/transport_tcp.cpp",
          "start_line": 411,
          "end_line": 440,
          "role": "hostile byte source"
        },
        {
          "path": "services/manufacturing/gateway/transport_serial.cpp",
          "start_line": 431,
          "end_line": 441,
          "role": "hostile byte source"
        }
      ],
      "cwe": [
        "CWE-400",
        "CWE-770"
      ],
      "evidence": [
        {
          "label": "no receive-side cap",
          "explanation": "The buffer only shrinks when a terminator is found; there is no ceiling on its size.",
          "excerpt": "input_bytes_ += bytesToString(bytes);\nwhile (true) {\n    const auto end = input_bytes_.find_first_of(\"\\r\\n\");\n    if (end == std::string::npos) break;"
        },
        {
          "label": "limit exists only outbound",
          "explanation": "startProgram refuses lines above rx_capacity, proving the protocol has a line ceiling the receive path ignores.",
          "excerpt": "if (line.size() + 1 > config_.rx_capacity) return {false, \"encoded program line exceeds configured Grbl RX capacity\"};"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Bound input_bytes_ by config_.rx_capacity and markTerminal with an rx_overflow reason on overflow.",
        "scope_note": "core/device is outside the roots this task may write to; no file was modified."
      }
    },
    {
      "rule_id": "MFG-PRINTER-BODY-UNBOUNDED",
      "title": "Printer HTTP responses are unbounded and re-journaled eight times per snapshot",
      "summary": "HttpClient reads the entire response with no size cap, every telemetry row embeds the whole decoded payload as `raw`, and the lane journals all eight rows durably and broadcasts them on each mfg_telemetry_snapshot, so a hostile printer amplifies one poll into an attacker-sized durable write.",
      "severity": "medium",
      "confidence": "high",
      "category": "denial of service",
      "locations": [
        {
          "path": "services/manufacturing/printers/http_client.py",
          "start_line": 65,
          "end_line": 93,
          "role": "unbounded read"
        },
        {
          "path": "services/manufacturing/printers/base.py",
          "start_line": 87,
          "end_line": 109,
          "role": "per-row raw duplication"
        },
        {
          "path": "services/manufacturing/printers/model.py",
          "start_line": 266,
          "end_line": 305,
          "role": "raw embedded in each row"
        },
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 1161,
          "end_line": 1206,
          "role": "durable amplification"
        }
      ],
      "cwe": [
        "CWE-400",
        "CWE-770"
      ],
      "evidence": [
        {
          "label": "no body ceiling",
          "explanation": "Only the error excerpt is capped (512 bytes); the success path reads everything.",
          "excerpt": "with self._opener.open(request, timeout=self.timeout_s) as response:\n    result = HttpResponse(response.status, dict(response.headers.items()), response.read())"
        },
        {
          "label": "eightfold duplication then journaled",
          "explanation": "Each of the eight rows carries the full raw payload, and the lane appends the row array to the journal on every snapshot call.",
          "excerpt": "appendEvent(\"mfg.device.snapshot\", device_id_.str(), {{\"device_id\", device_id_.str()}, {\"snapshot\", snapshot}});"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Cap the read (e.g. 1 MiB) and attach raw once per snapshot instead of once per row; cap the journaled raw."
      }
    },
    {
      "rule_id": "MFG-WORKER-NO-ALLOWLIST",
      "title": "Printer worker RPC performs no allowlist check and is shielded only by the mfg_ prefix",
      "summary": "worker_rpc._open constructs an HTTP client from caller-supplied base_url and auth with no DeviceAllowlist consultation and no SSRF policy. hostd forwards every engine-unknown method to the worker; the only thing preventing arbitrary-URL requests from a local client is that ManufacturingLane::handle claims all mfg_-prefixed methods and refuses unknown ones. No test pins that naming invariant.",
      "severity": "medium",
      "confidence": "high",
      "category": "SSRF / allowlist bypass (defense in depth)",
      "locations": [
        {
          "path": "services/manufacturing/printers/worker_rpc.py",
          "start_line": 47,
          "end_line": 76,
          "role": "unchecked sink"
        },
        {
          "path": "core/hostd/server.cpp",
          "start_line": 805,
          "end_line": 812,
          "role": "generic worker fallback"
        },
        {
          "path": "core/hostd/server.cpp",
          "start_line": 526,
          "end_line": 529,
          "role": "sole shielding control"
        },
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 227,
          "end_line": 237,
          "role": "prefix interception"
        }
      ],
      "cwe": [
        "CWE-918",
        "CWE-183"
      ],
      "evidence": [
        {
          "label": "no allowlist in the worker",
          "explanation": "base_url comes straight from params; EndpointConfig only requires an absolute http(s) URL without credentials, so loopback and link-local pass.",
          "excerpt": "config = EndpointConfig(base_url=endpoint.get(\"base_url\"), auth=auth_config, endpoint_is_physical=endpoint.get(\"endpoint_is_physical\"), ...)"
        },
        {
          "label": "fallback would reach it",
          "explanation": "Any method the engine does not know is forwarded to the Python worker verbatim.",
          "excerpt": "if (unknown) { json wresult; std::string werr; if (WorkerBridge::instance().call(method, p, wresult, werr)) return wresult;"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Assert in a test that every WORKER_METHODS key starts with mfg_, and refuse a base_url host the lane did not pass as expected inside _open."
      }
    },
    {
      "rule_id": "MFG-SERIAL-IDENTITY-PATH-ONLY",
      "title": "POSIX serial commissioning binds to a path only; USB identity is enforced on Windows alone",
      "summary": "SerialTransport::open builds the endpoint identity from the address with empty USB fields on POSIX (the enumeration match is #ifdef _WIN32), so authorization is path equality. Enumerated /dev/serial/by-id names derive from device-supplied USB descriptors, and open follows symlinks with no O_NOFOLLOW and no fstat character-device or st_rdev check.",
      "severity": "medium",
      "confidence": "high",
      "category": "authentication of device identity",
      "locations": [
        {
          "path": "services/manufacturing/gateway/transport_serial.cpp",
          "start_line": 120,
          "end_line": 141,
          "role": "identity construction"
        },
        {
          "path": "services/manufacturing/gateway/transport_serial.cpp",
          "start_line": 156,
          "end_line": 157,
          "role": "symlink-following open"
        },
        {
          "path": "services/manufacturing/gateway/transport_serial.cpp",
          "start_line": 575,
          "end_line": 587,
          "role": "descriptor-derived enumeration"
        },
        {
          "path": "services/manufacturing/gateway/device_allowlist.cpp",
          "start_line": 287,
          "end_line": 296,
          "role": "identity key"
        }
      ],
      "cwe": [
        "CWE-59",
        "CWE-290",
        "CWE-346"
      ],
      "evidence": [
        {
          "label": "USB tuple only consulted on Windows",
          "explanation": "On POSIX the identity has empty vid/pid/serial, so key() reduces to the path, and an entry that records the USB tuple can never match.",
          "excerpt": "EndpointIdentity identity = EndpointIdentity::serial(endpoint.address);\n#ifdef _WIN32\n    for (const auto& candidate : enumerate()) { ... }\n#endif"
        },
        {
          "label": "no post-open verification",
          "explanation": "O_EXCL without O_CREAT is a no-op for character devices; there is no O_NOFOLLOW and no fstat check that the fd is the commissioned device.",
          "excerpt": "const int fd = ::open(identity.address.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC | O_EXCL);"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Verify sysfs USB attributes (or the resolved by-id target) against the commissioned tuple and fstat the fd for S_ISCHR before configuring."
      }
    },
    {
      "rule_id": "MFG-WEBHOOK-SSRF",
      "title": "WebhookChannel has no egress policy and follows redirects",
      "summary": "Webhook URLs are validated only as http(s) with a hostname; loopback, link-local and private ranges are accepted, and urlopen uses the default opener so a 30x redirects the POST to a metadata or loopback service. Latent: the class is constructed nowhere outside tests today.",
      "severity": "medium",
      "confidence": "medium",
      "category": "SSRF / egress",
      "locations": [
        {
          "path": "services/manufacturing/notify/channels.py",
          "start_line": 135,
          "end_line": 155,
          "role": "unrestricted egress"
        }
      ],
      "cwe": [
        "CWE-918"
      ],
      "evidence": [
        {
          "label": "validation is scheme-only",
          "explanation": "No address-class check and no redirect suppression, unlike the printer HTTP client which installs _NoRedirect.",
          "excerpt": "parsed = urlsplit(url)\nif parsed.scheme not in {\"http\", \"https\"} or not parsed.hostname:\n    raise ValueError(\"webhook URL must be http(s)\")"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Build the opener with the existing _NoRedirect handler and refuse resolved addresses that are loopback, link-local, private or reserved unless explicitly opted in.",
        "status": "latent until notify is wired"
      }
    },
    {
      "rule_id": "MFG-OSASCRIPT-INJECTION",
      "title": "AppleScript injection in the macOS desktop notification channel",
      "summary": "The osascript command is built by string interpolation that escapes double quotes but not backslashes, so text ending in a backslash-quote terminates the AppleScript string and the remainder is parsed as code. The text originates from attention reason/why, which mfg_attention_raise accepts with no lease and no authentication.",
      "severity": "medium",
      "confidence": "medium",
      "category": "command/script injection",
      "locations": [
        {
          "path": "services/manufacturing/notify/channels.py",
          "start_line": 124,
          "end_line": 131,
          "role": "injection sink"
        },
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 1640,
          "end_line": 1665,
          "role": "unauthenticated text source"
        }
      ],
      "cwe": [
        "CWE-78",
        "CWE-116"
      ],
      "evidence": [
        {
          "label": "backslash is not escaped",
          "explanation": "Input ending in backslash-quote becomes escaped-backslash followed by a closing quote in the generated script.",
          "excerpt": "script = 'display notification \"' + body.replace('\"', '\\\\\"') + '\" with title \"' + title.replace('\"', '\\\\\"') + '\"'"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Escape backslashes before quotes, or pass the strings through argv with osascript -e 'on run argv' ... -- body title.",
        "status": "latent until notify is wired"
      }
    },
    {
      "rule_id": "MFG-TCP-DRAIN-UNBOUNDED",
      "title": "TcpTransport::poll drains without a byte budget or deadline recheck",
      "summary": "The recv loop exits only on EAGAIN, EOF or error, never rechecks the deadline, and imposes no cap on accumulated events; every received byte is also stored twice. A peer that outpaces the reader — trivial over loopback — grows one poll's result without bound.",
      "severity": "medium",
      "confidence": "medium",
      "category": "denial of service",
      "locations": [
        {
          "path": "services/manufacturing/gateway/transport_tcp.cpp",
          "start_line": 411,
          "end_line": 440,
          "role": "unbounded drain"
        }
      ],
      "cwe": [
        "CWE-400",
        "CWE-770"
      ],
      "evidence": [
        {
          "label": "no budget, no deadline",
          "explanation": "The for(;;) loop appends a 16 KiB event per recv with no ceiling and no time check.",
          "excerpt": "for (;;) {\n    std::array<std::byte, 16 * 1024> buffer{};\n    const ssize_t count = ::recv(impl_->socket, buffer.data(), buffer.size(), 0);\n    if (count > 0) { ... result.events.push_back(std::move(event)); continue; }"
        },
        {
          "label": "double storage",
          "explanation": "Each event keeps the payload both as bytes and as a std::string copy.",
          "excerpt": "event.data.assign(buffer.begin(), buffer.begin() + count);\nevent.raw.assign(reinterpret_cast<const char*>(event.data.data()), event.data.size());"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Cap bytes per poll and break when deadline_timeout_ms(deadline) reaches zero; stop duplicating the payload into raw."
      }
    },
    {
      "rule_id": "MFG-REMOTE-PATH-TRAVERSAL",
      "title": "Printer-supplied remote_path becomes a URL path segment",
      "summary": "remote_path comes from the printer's own upload response and is inserted into the OctoPrint select URL with quote(safe=\"/\"); urljoin then normalizes .. segments, so a hostile response retargets the POST to an arbitrary path on the printer host. Host takeover is prevented by lstrip(\"/\").",
      "severity": "low",
      "confidence": "medium",
      "category": "request forgery / path traversal",
      "locations": [
        {
          "path": "services/manufacturing/printers/octoprint.py",
          "start_line": 148,
          "end_line": 160,
          "role": "tainted path use"
        },
        {
          "path": "services/manufacturing/printers/octoprint.py",
          "start_line": 134,
          "end_line": 139,
          "role": "taint source"
        },
        {
          "path": "services/manufacturing/printers/http_client.py",
          "start_line": 74,
          "end_line": 75,
          "role": "urljoin normalization"
        }
      ],
      "cwe": [
        "CWE-22",
        "CWE-441"
      ],
      "evidence": [
        {
          "label": "slashes preserved from remote data",
          "explanation": "quote(..., safe=\"/\") keeps path separators, and urljoin resolves .. against the base URL.",
          "excerpt": "\"/api/files/local/\" + quote(remote_path, safe=\"/\")"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Validate remote_path against a conservative character class with no .. segment, or quote it with safe=\"\"."
      }
    },
    {
      "rule_id": "MFG-DNS-POST-AUTH",
      "title": "DNS resolves after authorization with no post-resolution address check",
      "summary": "Authorization compares hostname strings; getaddrinfo then runs and the transport connects to whatever it returns, with no pinning and no refusal of loopback or link-local results. All alternate spellings of an address fail closed at the allowlist, so the residual risk is limited to hostname entries under hostile DNS.",
      "severity": "low",
      "confidence": "high",
      "category": "SSRF / TOCTOU",
      "locations": [
        {
          "path": "services/manufacturing/gateway/transport_tcp.cpp",
          "start_line": 236,
          "end_line": 263,
          "role": "resolve-after-authorize"
        },
        {
          "path": "services/manufacturing/gateway/device_allowlist.h",
          "start_line": 16,
          "end_line": 18,
          "role": "documented design choice"
        }
      ],
      "cwe": [
        "CWE-350",
        "CWE-918"
      ],
      "evidence": [
        {
          "label": "authorize then resolve",
          "explanation": "The allowlist decision is made on the literal host string; the socket then connects to the resolved address without further checks.",
          "excerpt": "const Authorization authorization = impl_->allowlist.authorize(candidate);\nif (!authorization.allowed) return {false, ...};\n...\nconst int lookup = getaddrinfo(endpoint_address.host.c_str(), endpoint_address.port_text.c_str(), &hints, &addresses);"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Refuse resolved addresses outside the commissioned class, or pin the address observed at commissioning time."
      }
    },
    {
      "rule_id": "CAPTURE-CLIP-INTEGRITY",
      "title": "Capture replay never verifies clip hashes although the manifest records them",
      "summary": "_load_clip only checks that the clip_id appears in the manifest; neither the manifest sha256 nor the content-derived clip_id is recomputed, so a tampered clip on disk replays with intact provenance.",
      "severity": "low",
      "confidence": "high",
      "category": "integrity",
      "locations": [
        {
          "path": "services/perception/capture/replay.py",
          "start_line": 39,
          "end_line": 58,
          "role": "unverified load"
        },
        {
          "path": "services/perception/capture/replay.py",
          "start_line": 100,
          "end_line": 107,
          "role": "membership-only check"
        },
        {
          "path": "services/perception/capture/storage.py",
          "start_line": 207,
          "end_line": 215,
          "role": "hash is recorded"
        }
      ],
      "cwe": [
        "CWE-345",
        "CWE-354"
      ],
      "evidence": [
        {
          "label": "membership is not integrity",
          "explanation": "The clip id is looked up in the manifest but nothing is hashed on read, even though clip_id is by construction the sha256 of the canonical body.",
          "excerpt": "clip, samples = _load_clip(clip_path, extractor)\nif clip[\"clip_id\"] not in manifest_by_clip:\n    raise ValueError(f\"clip {clip['clip_id']} is absent from the landed manifest\")"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Recompute sha256 of the file bytes against the manifest record and re-derive clip_id from the canonical body before accepting a clip."
      }
    },
    {
      "rule_id": "MFG-ATTENTION-UNAUTHENTICATED",
      "title": "Attention and journal surfaces require no lease and no identity",
      "summary": "mfg_attention_raise/ack/delivery/resolve, mfg_journal_query, mfg_result_invalidations, mfg_run_list/describe, mfg_gate_report and mfg_approval_inspect are reachable by any daemon client without a lease, permitting unbounded durable journal growth with attacker-chosen text, forged acknowledgement attribution, forged delivery receipts and full journal disclosure.",
      "severity": "low",
      "confidence": "high",
      "category": "authorization / resource exhaustion",
      "locations": [
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 1640,
          "end_line": 1733,
          "role": "unauthenticated writes"
        },
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 1734,
          "end_line": 1748,
          "role": "unauthenticated full journal read"
        }
      ],
      "cwe": [
        "CWE-862",
        "CWE-770"
      ],
      "evidence": [
        {
          "label": "no lease gate",
          "explanation": "Unlike every write path above it, the attention family calls neither requireLease nor validDevice, and `by` is accepted verbatim as the acknowledging identity.",
          "excerpt": "if (method == \"mfg_attention_ack\") {\n    const std::string id = params.value(\"id\", std::string{});\n    const std::string by = params.value(\"by\", std::string{});"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Require the commander lease for attention mutations, bound the attention map and per-item deliveries, and cap reason/why length."
      }
    },
    {
      "rule_id": "MFG-NOTIFY-SEND-OPTIONS",
      "title": "notify-send invoked without an option terminator",
      "summary": "Attention text beginning with a dash is parsed as a notify-send option because no `--` separator precedes the positional title and body. No code-execution primitive, but delivery can be subverted or broken by attacker-chosen text.",
      "severity": "low",
      "confidence": "medium",
      "category": "argument injection",
      "locations": [
        {
          "path": "services/manufacturing/notify/channels.py",
          "start_line": 108,
          "end_line": 127,
          "role": "argument construction"
        }
      ],
      "cwe": [
        "CWE-88"
      ],
      "evidence": [
        {
          "label": "no -- terminator",
          "explanation": "Title and body are appended as bare positionals after the option list.",
          "excerpt": "return [\"notify-send\", \"--app-name=Chudware\", title, body]"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Insert \"--\" before the positional arguments.",
        "status": "latent until notify is wired"
      }
    },
    {
      "rule_id": "MFG-PYWS-FRAGMENT-LOOP",
      "title": "Python WebSocket client can be pinned by empty continuation frames",
      "summary": "recv_json loops until a final frame arrives; a peer streaming zero-length non-final continuation frames never trips the message size limit, so the loop continues indefinitely. An interleaved text frame silently resets the accumulator instead of failing the connection.",
      "severity": "informational",
      "confidence": "medium",
      "category": "denial of service / parser hardening",
      "locations": [
        {
          "path": "services/manufacturing/printers/websocket.py",
          "start_line": 80,
          "end_line": 107,
          "role": "unbounded loop"
        }
      ],
      "cwe": [
        "CWE-835"
      ],
      "evidence": [
        {
          "label": "size limit cannot fire on empty frames",
          "explanation": "Only the accumulated payload length is bounded; the number of frames per message is not.",
          "excerpt": "if len(fragments) > _MAX_MESSAGE:\n    raise ProtocolError(\"Moonraker websocket message exceeded size limit\")\nif final:"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Cap frames per message and treat a data frame arriving mid-fragmentation as a protocol error."
      }
    },
    {
      "rule_id": "MFG-CAPABILITY-LEVELS-DROPPED",
      "title": "Printer-probed capability levels are silently discarded by the lane",
      "summary": "The adapter emits levels as a JSON array while capabilityJson tests is_object(), so probed per-level truths are always replaced by the lane's emulator-derived levels. Safety is not weakened, but operators and agents see synthesized rather than probed truth.",
      "severity": "informational",
      "confidence": "high",
      "category": "evidence provenance",
      "locations": [
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 610,
          "end_line": 642,
          "role": "shape mismatch"
        },
        {
          "path": "services/manufacturing/printers/model.py",
          "start_line": 204,
          "end_line": 250,
          "role": "array-shaped levels"
        },
        {
          "path": "core/hostd/mfg_lane.cpp",
          "start_line": 273,
          "end_line": 283,
          "role": "allowlisted candidates are always physical"
        }
      ],
      "cwe": [
        "CWE-1076"
      ],
      "evidence": [
        {
          "label": "array fails the object test",
          "explanation": "capability_report returns levels as a list, so the is_object() branch always substitutes the lane's own levels; the lane still re-asserts safety_integration unsupported and execute assumed for physical endpoints.",
          "excerpt": "if (!capability.contains(\"levels\") || !capability[\"levels\"].is_object())\n    capability[\"levels\"] = base[\"levels\"];"
        }
      ],
      "optionalProperties": {
        "minimal_fix": "Normalize the adapter's level array into the object shape before merging so probed truths survive."
      }
    }
  ],
  "reviewed_paths": [
    "core/hostd/mfg_lane.h",
    "core/hostd/mfg_lane.cpp",
    "services/manufacturing/gateway/device_allowlist.h",
    "services/manufacturing/gateway/device_allowlist.cpp",
    "services/manufacturing/gateway/transport_serial.h",
    "services/manufacturing/gateway/transport_serial.cpp",
    "services/manufacturing/gateway/transport_tcp.h",
    "services/manufacturing/gateway/transport_tcp.cpp",
    "services/manufacturing/gateway/transport_ws.h",
    "services/manufacturing/gateway/transport_ws.cpp",
    "services/manufacturing/printers/http_client.py",
    "services/manufacturing/printers/websocket.py",
    "services/manufacturing/printers/model.py",
    "services/manufacturing/printers/base.py",
    "services/manufacturing/printers/moonraker.py",
    "services/manufacturing/printers/octoprint.py",
    "services/manufacturing/printers/prusalink.py",
    "services/manufacturing/printers/worker_rpc.py",
    "services/manufacturing/notify/channels.py",
    "services/manufacturing/notify/service.py",
    "services/manufacturing/notify/store.py",
    "services/manufacturing/notify/journal.py",
    "services/manufacturing/monitor/mtconnect.py",
    "services/manufacturing/monitor/replay.py",
    "services/mcp/mfg_tools.py",
    "services/mcp/server.py",
    "services/mcp/test_mfg_tools.py",
    "services/perception/capture/storage.py",
    "services/perception/capture/replay.py",
    "services/perception/capture/buffer.py",
    "services/perception/capture/session.py",
    "services/perception/capture/redaction.py",
    "services/worker_main.py",
    "core/hostd/server.cpp",
    "core/hostd/worker_bridge.cpp",
    "core/device/grbl_adapter.cpp",
    "core/manufacturing/lease.h"
  ],
  "deferred": [
    {
      "reason": "Outside the assigned target list; slicer/sidecar.py spawns subprocesses (services/manufacturing/slicer/sidecar.py:322-325) and deserves the same treatment. Main has assigned this to a follow-up agent, slicer first.",
      "paths": [
        "services/manufacturing/slicer/**",
        "services/manufacturing/robot/**",
        "services/manufacturing/pnp/**",
        "services/manufacturing/metrology/**"
      ]
    },
    {
      "reason": "Read for evidence only; the finding that lands in core/device (F3) is reported but no file was modified and it is outside the roots this task may write to.",
      "paths": [
        "core/device/grbl_adapter.cpp",
        "core/hostd/server.cpp"
      ]
    },
    {
      "reason": "No execution was performed: no daemon started, no payload run, no network call made. Exploitability rankings are reasoned from the dispatch path, not demonstrated. The artifact local://manufacturing-security-review.md could not be created because this session has no write tool; the full report was delivered to Main over hub in four parts and is reproduced in coverage_summary and findings.",
      "paths": [
        "local://manufacturing-security-review.md"
      ]
    }
  ]
}
```
