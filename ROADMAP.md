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

## Phase 2 (mostly shipped)

Most of the meaningful Phase 2 cross-impl coverage is in place. The
suite ships 28 passing trios across 7 test files (4 trios each):

- `test_announce_discovery.py` — pipe-connected pair, both nodes
  announce, both observe each other's path entry.
- `test_opportunistic.py` — single-packet opportunistic; receiver
  surfaces correct payload and sender's outbound state reaches
  `delivered`.
- `test_direct.py` — small DIRECT (link-based); explicit-format
  proof signed with the link's signing key flows back from receiver
  so the sender's `PacketReceipt` callback fires.
- `test_direct_large.py` — multi-KB DIRECT message; exercises
  Resource transfer fragmentation + reassembly across the link.
- `test_attachments.py` — `FIELD_FILE_ATTACHMENTS` roundtrip;
  bridge wire format uses tagged objects (`{"bytes": "<hex>"}`,
  `{"str": "..."}`) to express LXMF's `[UInt8: Any]` field map.
- `test_combined.py` — multi-KB content + attachment over
  Resource transfer simultaneously.
- `test_propagation.py` — sender → python_pn → receiver. PN is
  always python because swift LXMF doesn't implement
  `enable_propagation()` (in-process propagation daemon is
  python-only).

### Real PipeInterface-backed pair fixture (deferred)
- Phase 1 ships TCP loopback as the direct-pair transport. The original design called for a Reticulum `PipeInterface` (or fd-pair equivalent) so the test pair has *zero* networking layer between them.
- The Python `RNS.Interfaces.PipeInterface` doesn't fit cleanly because it spawns a subprocess via `command =` config; we'd need to subclass it for fd-pair use. macOS's `posix_spawn` + `pass_fds` interaction also surfaced subtle EOF issues during initial Phase 1 work that took longer to debug than the actual cross-impl tests warranted.
- Refinement task: write an `FdPipeInterface` for both Python and Swift bridges (HDLC framing matching the upstream `PipeInterface.HDLC`), wire them via `os.pipe()` pairs and `subprocess.pass_fds`, debug the EOF behaviour, and switch the test fixture back. Test surface stays unchanged — only the bridge command surface and the conftest fixture shift.

### Sync with propagation node (extensions)
- Current `test_propagation.py` covers the basic happy path. Follow-ups:
  - Partial sync (PN has > 1 message; receiver pulls them all).
  - Retransmission (link drop mid-sync; receiver re-syncs).
  - Stamp generation interop edge cases.

### Kotlin bridge implementation (not started)
- Land the Phase 1 + Phase 2 command surface against reticulum-kt / a Kotlin LXMF library.
- Once present, add `kotlin` to `BRIDGE_COMMANDS` parametrization (it's already wired; just needs the binary).
- Cross-language permutations including kotlin pairs activate automatically.

### Real lxmd-subprocess propagation node (refinement)
- Current PN role uses python LXMF's `enable_propagation()` in-process daemon.
- Production deployments run `lxmd` as a separate subprocess attached via shared RNS instance — mirrors `reticulum-conformance/reference/lxmf_bridge.py::cmd_lxmf_spawn_daemon_propagation_node`.
- Refinement task: add `share_instance=Yes` mode to the python bridge's RNS startup, plus `lxmf_spawn_daemon_propagation_node`, so the PN trio runs against an actual `lxmd` rather than an in-process router.

## Test scope expansion

- **Pipe-pair fixture**: `(A, B)` connected by anonymous pipes — Phase 1.
- **Pipe-trio fixture**: `(A, P, B)` with `A` and `B` linked through a transport-mode middle peer `P` — Phase 2.
- **TCP fixture**: optional — when we want to test against real RNS TCP transport. May be redundant with reticulum-conformance.

## Open questions

- Should `lxmf-conformance` and `reticulum-conformance` share a `bridge_client.py`? They diverge today only in cosmetic naming. Leaving them separate avoids cross-repo coupling for Phase 1; if Phase 2 surfaces real maintenance friction, we can extract a shared `conformance-bridge-protocol` package.

- Does the Python LXMF library need API surface changes to fit a clean bridge protocol? So far no — `LXMRouter` exposes everything we need (`register_delivery_callback`, `register_delivery_identity`, `handle_outbound`, `request_messages_from_propagation_node`).

- Does LXMF-swift need new public API for the bridge? Phase 1 implementation will surface this; expect to add helper methods like `currentPropagationStateString()` for the bridge-side state translation.
