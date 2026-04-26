"""LXMF OPPORTUNISTIC delivery over a pipe-connected pair.

Phase 1 cross-impl interop: A sends an opportunistic LXMF message to
B; B receives the message with EXACT content + title + method; A's
outbound state transitions to ``delivered`` after B's RNS replies
with the proof packet.

Opportunistic delivery is single-packet, so the entire end-to-end
encode → encrypt → wire-frame → unframe → decrypt → decode cycle is
exercised. Any bit-level disagreement between Python LXMF, LXMF-swift,
or (Phase 2) LXMF-kt surfaces here.

Parametrized over every (server_impl, client_impl) pair detected by
the conftest.
"""

import secrets
import time


def test_opportunistic_message_with_ack(server_impl, client_impl, pipe_pair):
    """Server -> client opportunistic message; client receives correct
    payload; server's outbound state transitions to delivered."""
    server, client = pipe_pair

    # Random content tail so a stale receiver buffer can't accidentally
    # pass a content-match assertion. Stay under
    # LXMessage.ENCRYPTED_PACKET_MAX_CONTENT (~295 bytes) — the bridge
    # rejects oversized opportunistic up-front, so this is bounded.
    content = f"opp-{secrets.token_hex(8)}"
    title = f"opp-title-{secrets.token_hex(4)}"

    message_hash = server.send_opportunistic(
        recipient_hash=client.delivery_hash,
        content=content,
        title=title,
    )
    assert message_hash, (
        f"server.send_opportunistic ({server_impl}) returned empty "
        f"message_hash — sender did not finish packing the LXMessage. "
        f"This is a bridge/sender-side bug, not interop."
    )

    # ---- Receive side: poll until exactly one message lands ----------
    # Linger after first sighting so a duplicate landing on the next
    # tick still flips the count and fails the equality check (the
    # tight-assertions discipline matched in reticulum-conformance).
    deadline = time.time() + 15.0
    received = []
    while time.time() < deadline:
        received += client.drain_received()
        if len(received) >= 1:
            # Linger >= one poll cycle so a delayed duplicate would
            # still be observed before we stop draining.
            time.sleep(0.5)
            received += client.drain_received()
            break
        time.sleep(0.2)

    assert len(received) == 1, (
        f"Client ({client_impl}) received {len(received)} messages, "
        f"expected exactly 1 within 15s. Inbox: {received!r}"
    )

    msg = received[0]
    assert msg["content"] == content, (
        f"content mismatch: client got {msg['content']!r}, sender sent "
        f"{content!r}. Cross-impl ({server_impl}->{client_impl}) "
        f"opportunistic decode failed."
    )
    assert msg["title"] == title, (
        f"title mismatch: client got {msg['title']!r}, sender sent "
        f"{title!r}. Cross-impl ({server_impl}->{client_impl}) "
        f"opportunistic decode failed."
    )
    assert msg["source_hash"] == server.delivery_hash.hex(), (
        f"source_hash mismatch: client got {msg['source_hash']}, "
        f"expected {server.delivery_hash.hex()}."
    )
    assert msg["destination_hash"] == client.delivery_hash.hex(), (
        f"destination_hash mismatch: client got "
        f"{msg['destination_hash']}, expected {client.delivery_hash.hex()}."
    )
    assert msg["method"] == "opportunistic", (
        f"method mismatch: client got method={msg['method']!r}, expected "
        f"'opportunistic'. Sender ({server_impl}) may have silently "
        f"upgraded to a different delivery method."
    )
    assert msg["message_hash"] == message_hash.hex(), (
        f"message_hash mismatch: client decoded "
        f"{msg['message_hash']}, sender computed {message_hash.hex()}. "
        f"This is a hash-derivation interop bug — receiver hash != "
        f"sender hash means the cross-impl LXMessage hash function "
        f"disagrees on input bytes."
    )

    # ---- Sender ack: poll until the proof packet arrives -------------
    # The receiver's RNS auto-emits a proof packet on receipt of an
    # opportunistic delivery; the sender's RNS dispatches its
    # registered proof callback, which transitions the LXMessage state
    # to DELIVERED. This is the cross-impl interop proof for the
    # opportunistic path: receiver decoded correctly AND sender
    # processed the proof correctly.
    deadline = time.time() + 15.0
    final_state = "unknown"
    while time.time() < deadline:
        final_state = server.message_state(message_hash)
        if final_state == "delivered":
            break
        if final_state == "failed":
            break
        time.sleep(0.2)

    assert final_state == "delivered", (
        f"Server ({server_impl}) outbound message state did not reach "
        f"'delivered' within 15s — last observed: {final_state!r}. "
        f"Either the receiver ({client_impl}) did not emit a proof, "
        f"or the sender did not process it."
    )
