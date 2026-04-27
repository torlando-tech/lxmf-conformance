# lxmf-conformance roadmap

## Phase 1 (shipped)

- Python reference bridge (`reference/lxmf_python.py`) — full implementation.
- Swift bridge — lives in [LXMF-swift](https://github.com/torlando-tech/LXMF-swift) as the `LXMFConformanceBridge` SPM executable target.
- Kotlin bridge — stub only (`reference/lxmf_kotlin.kt`).
- Cross-impl pytest fixtures with `(server_impl, client_impl)` parametrization.
- Direct two-node connectivity via **TCP loopback** (deferred from PipeInterface — see Phase 2 below).
- Two foundational tests:
  - `tests/test_announce_discovery.py` — pipe-connected pair, both nodes announce, both observe each other.
  - `tests/test_opportunistic.py` — A sends opportunistic to B; B receives correct payload; A's outbound state reaches `delivered`.
- CI workflow that builds the Swift bridge, runs the cross-impl tests against `(python, python)` and `(python, swift)` and `(swift, python)` and `(swift, swift)`.

## Phase 1.1 — Swift outbound fix (shipped)

Root cause: the swift bridge created `TCPServerInterface` but never set
`onClientConnected`, so accepted connections produced
`TCPSpawnedPeerInterface` children that never entered
`Transport.interfaces`. Inbound packets on those children (announce,
proof, link request) reached `receive(packet:from:)` with an unknown
interfaceId and were dropped before path-table updates ran. The
visible failure was "No identity known for destination …" because
swift's path table never observed the python client's announce.

Fix: wire `onClientConnected` to register every spawned peer with the
transport, mirroring the existing reticulum-swift bridge
(`ConformanceBridge/WireTcp.swift:319-343`). All 8 Phase 1 trios now
pass — `python→python`, `python→swift`, `swift→python`, `swift→swift`
across both `test_announce_discovery` and `test_opportunistic`.

Landed in [LXMF-swift PR #5](https://github.com/torlando-tech/LXMF-swift/pull/5).

## Phase 2 (deferred)

These are the test categories that need to land before this suite covers the LXMF protocol meaningfully. Listed in roughly the order they should be tackled — earlier items are prerequisites for later ones.

### Real PipeInterface-backed pair fixture
- Phase 1 ships TCP loopback as the direct-pair transport. The original design called for a Reticulum `PipeInterface` (or fd-pair equivalent) so the test pair has *zero* networking layer between them.
- The Python `RNS.Interfaces.PipeInterface` doesn't fit cleanly because it spawns a subprocess via `command =` config; we'd need to subclass it for fd-pair use. macOS's `posix_spawn` + `pass_fds` interaction also surfaced subtle EOF issues during initial Phase 1 work that took longer to debug than the actual cross-impl tests warranted.
- Phase 2 task: write an `FdPipeInterface` for both Python and Swift bridges (HDLC framing matching the upstream `PipeInterface.HDLC`), wire them via `os.pipe()` pairs and `subprocess.pass_fds`, debug the EOF behaviour, and switch the test fixture back. Test surface stays unchanged — only the bridge command surface and the conftest fixture shift.

### Direct (link-based) delivery
- New bridge command: `lxmf_send_direct { destination_hash, content, title? }`.
- New test: `tests/test_direct.py::test_direct_message_with_link_proof`.
- Same pipe-pair topology; receiver must surface the message with `method == "direct"`.

### Propagation
- Three-bridge topology: sender → propagation node → receiver.
- Propagation node: a real `lxmd` subprocess attached via shared instance (mirrors `reticulum-conformance/reference/lxmf_bridge.py::cmd_lxmf_spawn_daemon_propagation_node`).
- New bridge commands: `lxmf_set_outbound_propagation_node`, `lxmf_send_propagated`, `lxmf_sync_inbound`.
- Tests:
  - Sender submits to PN; PN stores message; receiver pulls via sync and decodes.
  - Stamp generation interop (Python sender ↔ Swift receiver must agree on stamp validation).
  - 3-node topology requires more than two bridges per test — extend `pipe_pair` into `pipe_trio` / `tcp_trio` fixtures.

### Sync with propagation node
- Receiver-side: `lxmf_sync_inbound` blocks until the propagation transfer completes.
- Tests for partial sync, retransmission, and link drop during sync.

### Attachments
- New tests for `FIELD_FILE_ATTACHMENTS` (key 5), `FIELD_IMAGE` (key 6), `FIELD_AUDIO` (key 7).
- Field encoding crosses the largest cross-impl divergence risk: Python's `[filename, bytes]` pairs vs. Swift's tagged-dict shape. Mirror the `_decode_field_value_from_params` / `_encode_field_value_for_inbox` pattern from reticulum-conformance.

### Large messages
- Direct + propagation paths support multi-packet messages via Resource transfer.
- Tests for messages exceeding `LINK_PACKET_MAX_CONTENT` (~316 bytes for direct, propagation buckets are larger).
- Resource transfer interop is a high-risk surface — bytes must reassemble identically across impls.

### Combined attachment + text + large
- `text + image + file` matrix per delivery method.

### Kotlin bridge implementation
- Land the seven Phase 1 commands against reticulum-kt's LXMF library.
- Once present, add `kotlin` to `BRIDGE_COMMANDS` parametrization (it's already wired; just needs the binary).
- Cross-language permutations including kotlin pairs activate automatically.

## Test scope expansion

- **Pipe-pair fixture**: `(A, B)` connected by anonymous pipes — Phase 1.
- **Pipe-trio fixture**: `(A, P, B)` with `A` and `B` linked through a transport-mode middle peer `P` — Phase 2.
- **TCP fixture**: optional — when we want to test against real RNS TCP transport. May be redundant with reticulum-conformance.

## Open questions

- Should `lxmf-conformance` and `reticulum-conformance` share a `bridge_client.py`? They diverge today only in cosmetic naming. Leaving them separate avoids cross-repo coupling for Phase 1; if Phase 2 surfaces real maintenance friction, we can extract a shared `conformance-bridge-protocol` package.

- Does the Python LXMF library need API surface changes to fit a clean bridge protocol? So far no — `LXMRouter` exposes everything we need (`register_delivery_callback`, `register_delivery_identity`, `handle_outbound`, `request_messages_from_propagation_node`).

- Does LXMF-swift need new public API for the bridge? Phase 1 implementation will surface this; expect to add helper methods like `currentPropagationStateString()` for the bridge-side state translation.
