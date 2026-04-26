/*
 * Kotlin bridge stub for the LXMF conformance suite.
 *
 * Phase 2 deliverable. The conformance suite's pytest fixtures and
 * Python / Swift bridges are in shippable shape today; the Kotlin
 * library's LXMF surface is still in flux, so this file is a
 * placeholder that documents the command surface a future kotlin
 * implementation must implement to plug into the conformance suite.
 *
 * Required commands (see reference/lxmf_python.py for canonical
 * semantics — the Kotlin bridge MUST implement the same shape):
 *
 *   - lxmf_init { storage_path?, identity_pem?, display_name? }
 *       -> { identity_hash, delivery_destination_hash, config_dir,
 *            storage_path }
 *
 *   - lxmf_add_tcp_server_interface { bind_port?, name? }
 *       -> { port, interface_name }
 *
 *   - lxmf_add_tcp_client_interface { target_host?, target_port, name? }
 *       -> { interface_name }
 *
 *   - lxmf_announce {}
 *       -> { delivery_destination_hash }
 *
 *   - lxmf_send_opportunistic { destination_hash, content, title? }
 *       -> { message_hash }
 *
 *   - lxmf_get_received_messages { since_seq? }
 *       -> { messages: [...], last_seq }
 *
 *   - lxmf_get_message_state { message_hash }
 *       -> { state: "generating"|"outbound"|"sending"|"sent"|"delivered"|"failed"|... }
 *
 *   - lxmf_shutdown {}
 *       -> { stopped }
 *
 * Wire protocol: one JSON object per line on stdin; one response per
 * line on stdout. Print "READY" once on stdout before entering the
 * command loop. See bridge_client.py for the full protocol contract.
 *
 * Hex byte conventions: all binary fields (hashes, identity bytes)
 * are lowercase hex strings on the wire. The Kotlin bridge MUST emit
 * lowercase hex to interoperate with the test fixtures (the Python
 * and Swift bridges do).
 *
 * Phase 1 uses TCP loopback as the bridge-to-bridge transport. The
 * Kotlin bridge needs reticulum-kt's TCPServerInterface and
 * TCPClientInterface analogues — same wire protocol as the Python
 * impl. (Phase 2 reintroduces a direct PipeInterface; until then the
 * TCP path is the canonical one. See ROADMAP.md.)
 *
 * Until this stub is implemented, the conftest.py auto-detection will
 * skip kotlin pairs (no jar at the resolved path → impl is filtered
 * out before parametrization).
 */

// TODO(phase-2): implement the seven commands above against
// reticulum-kt's LXMF surface. Track progress at
// https://github.com/torlando-tech/lxmf-conformance/blob/main/ROADMAP.md
