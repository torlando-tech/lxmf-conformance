# lxmf-conformance

Cross-implementation conformance test suite for [LXMF](https://github.com/markqvist/LXMF). Drives multiple LXMF implementations through CLI bridge programs and runs identical pytest scenarios against every pair.

The goal is to **prove that swift↔python and (eventually) kotlin↔python interop perfectly at the LXMF protocol level**. By transitive property that means swift↔kotlin works.

## Architecture

```
                   pytest fixtures
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
       BridgeClient              BridgeClient
       (subprocess)              (subprocess)
       JSON-RPC stdio            JSON-RPC stdio
              │                       │
              ▼                       ▼
      ┌──────────────┐        ┌──────────────┐
      │ python bridge│        │ swift bridge │
      │              │        │              │
      │ RNS+LXMF     │        │ ReticulumSwift│
      │ TCPServer    │◄──TCP──│ TCPClient    │
      │              │ 127.0.0.1│ +LXMFSwift │
      └──────────────┘        └──────────────┘
```

Each test spawns two bridge subprocesses, wires them together with anonymous OS pipes, and exercises an LXMF scenario (announce, opportunistic message, eventually direct + propagated). The pytest fixtures parametrize over every `(server_impl, client_impl)` pair so a single test body covers the whole interop matrix.

## Bridge protocol

JSON-RPC over stdin/stdout, one object per line. Every bridge implementation MUST:

1. Print exactly `READY` on stdout when ready to accept commands.
2. Read JSON request lines from stdin: `{"id": "req-N", "command": "...", "params": {...}}`.
3. Write JSON response lines on stdout:
   - Success: `{"id": "req-N", "success": true, "result": {...}}`
   - Failure: `{"id": "req-N", "success": false, "error": "..."}`
4. Encode all binary fields as lowercase hex strings.

### Phase 1 commands

| Command | Params | Result |
|---|---|---|
| `lxmf_init` | `{ storage_path?, identity_pem?, display_name? }` | `{ identity_hash, delivery_destination_hash, config_dir, storage_path }` |
| `lxmf_add_tcp_server_interface` | `{ bind_port?, name? }` | `{ port, interface_name }` |
| `lxmf_add_tcp_client_interface` | `{ target_host?, target_port, name? }` | `{ interface_name }` |
| `lxmf_announce` | `{}` | `{ delivery_destination_hash }` |
| `lxmf_send_opportunistic` | `{ destination_hash, content, title? }` | `{ message_hash }` |
| `lxmf_get_received_messages` | `{ since_seq? }` | `{ messages: [...], last_seq }` |
| `lxmf_get_message_state` | `{ message_hash }` | `{ state: string }` |
| `lxmf_shutdown` | `{}` | `{ stopped: bool }` |

> **Phase 1 transport: TCP loopback.** The original design called for a Reticulum `PipeInterface` shared between bridges via OS pipes. Bringing that up reliably across `subprocess.Popen` + macOS `posix_spawn` + RNS interface registration consumed multiple cycles without contributing to the actual interop assertions. TCP loopback gives the same direct-pair semantics with the proven machinery `reticulum-conformance` already uses. Phase 2 will revisit a real `PipeInterface`-backed pair (see [ROADMAP.md](ROADMAP.md)) once the cross-impl test surface has stabilised.

### Inbound message shape

Each entry in `lxmf_get_received_messages.result.messages`:

```json
{
  "seq": 1,
  "message_hash": "abc123...",
  "source_hash": "def456...",
  "destination_hash": "789abc...",
  "title": "...",
  "content": "...",
  "method": "opportunistic",
  "ack_status": "received",
  "received_at_ms": 1714028400000
}
```

`seq` is monotonic per bridge process. Pass the highest `seq` seen as the next call's `since_seq` to drain incrementally. `method` is one of `"opportunistic"`, `"direct"`, `"propagated"`. `state` for outbound polling is one of `"generating"`, `"outbound"`, `"sending"`, `"sent"`, `"delivered"`, `"failed"`.

## Implementations

| Impl | Status | Bridge location |
|---|---|---|
| python | shipping | `reference/lxmf_python.py` |
| swift | shipping | [LXMF-swift] `Sources/LXMFConformanceBridge/main.swift` |
| kotlin | stub only | `reference/lxmf_kotlin.kt` (Phase 2) |

## Running locally

### Prerequisites

- Python 3.11+
- Swift 5.9+ (for the swift bridge)
- Local checkouts of `Reticulum`, `LXMF`, and `LXMF-swift` adjacent to this repo:

```
~/repos/
  ├── lxmf-conformance/    (this repo)
  ├── LXMF-swift/
  ├── Reticulum/           (markqvist/Reticulum)
  └── LXMF/                (markqvist/LXMF)
```

### Install Python deps

```bash
pip install -r requirements.txt
```

If you're testing against checked-out source rather than the pip releases:

```bash
export PYTHON_RNS_PATH=~/repos/Reticulum
export PYTHON_LXMF_PATH=~/repos/LXMF
```

### Build the swift bridge

```bash
cd ~/repos/LXMF-swift
swift build -c release --product LXMFConformanceBridge
```

The conformance suite auto-detects the binary at `../LXMF-swift/.build/release/LXMFConformanceBridge` (relative to this repo). Override with `CONFORMANCE_SWIFT_BRIDGE_CMD` if your layout differs.

### Run the tests

```bash
# All detected impls (python ↔ python, swift ↔ swift, cross pairs)
pytest tests/

# Restrict to specific impls
pytest tests/ --impls=python,swift

# Single test, all pairs
pytest tests/test_opportunistic.py -v
```

### Phase 1 status

As of the bootstrap commit, the test matrix looks like this on macOS 14:

| Test | python→python | python→swift | swift→python | swift→swift |
|---|---|---|---|---|
| `test_announce_discovery` | ✅ | ✅ | ❌ | ❌ |
| `test_opportunistic` | ✅ | ✅ | ❌ | ❌ |

The `swift→python` and `swift→swift` failures are isolated to the swift bridge's outbound side (announce + send opportunistic when swift hosts the TCP server). Inbound works fine — python→swift passes both tests, proving swift correctly receives, decodes, and acks LXMF messages from the python reference. The send-side gap is tracked in [ROADMAP.md](ROADMAP.md) as a Phase 1.1 follow-up.

The python→swift result is the most important Phase 1 interop signal: it proves the Swift LXMF impl reads Python LXMF's wire format correctly across the announce + opportunistic path.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for Phase 2 scope (direct, propagation, attachments, large messages, kotlin bridge).

## Related

- [Reticulum](https://github.com/markqvist/Reticulum) — underlying transport protocol.
- [LXMF](https://github.com/markqvist/LXMF) — Python LXMF reference implementation.
- [LXMF-swift](https://github.com/torlando-tech/LXMF-swift) — Swift LXMF implementation; hosts the `LXMFConformanceBridge` target.
- [reticulum-conformance](https://github.com/torlando-tech/reticulum-conformance) — sister repo for the underlying RNS protocol.

[LXMF-swift]: https://github.com/torlando-tech/LXMF-swift
