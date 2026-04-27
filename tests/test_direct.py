"""DIRECT (link-based) LXMF delivery, cross-impl.

Where opportunistic ships the message in a single encrypted packet,
DIRECT establishes a Reticulum Link first and pushes the LXMessage
over the link. Receiver-side, the message must surface with
``method == "direct"`` so the harness can tell the two paths apart.

Parametrized over every (server_impl, client_impl) pair just like the
opportunistic test — both sides must agree on link establishment +
LXMF resource transport over a link, so any cross-impl divergence in
the link wire format or LXMF on-link framing fails this test.
"""

import secrets
import time


def test_direct_message_with_link_proof(server_impl, client_impl, pipe_pair):
    """Server -> client direct message; client receives correct payload
    with method='direct'; server's outbound state transitions to
    delivered."""
    server, client = pipe_pair

    # Random tail keeps stale receiver buffers from accidentally
    # passing the content-match assertion.
    content = f"direct-{secrets.token_hex(8)}"
    title = f"direct-title-{secrets.token_hex(4)}"

    message_hash = server.send_direct(
        recipient_hash=client.delivery_hash,
        content=content,
        title=title,
    )
    assert message_hash, (
        f"server.send_direct ({server_impl}) returned empty "
        f"message_hash — sender did not finish packing the LXMessage."
    )

    # Direct delivery establishes a link, so allow a longer settle
    # window than opportunistic. 20s is comfortable on loopback while
    # still failing fast if establishment is broken.
    deadline = time.time() + 20.0
    received = []
    while time.time() < deadline:
        received += client.drain_received()
        if len(received) >= 1:
            time.sleep(0.5)
            received += client.drain_received()
            break
        time.sleep(0.2)

    assert len(received) == 1, (
        f"Client ({client_impl}) received {len(received)} messages, "
        f"expected exactly 1 within 20s. Inbox: {received!r}"
    )

    msg = received[0]
    assert msg["content"] == content, (
        f"content mismatch: got {msg['content']!r}, expected {content!r}"
    )
    assert msg["title"] == title, (
        f"title mismatch: got {msg['title']!r}, expected {title!r}"
    )
    assert msg["method"] == "direct", (
        f"method mismatch: got {msg['method']!r}, expected 'direct'. "
        f"Either the sender silently downgraded to opportunistic or "
        f"the receiver classified the inbound link delivery wrong."
    )
    assert msg["source_hash"] == server.delivery_hash.hex(), (
        f"source_hash mismatch: got {msg['source_hash']!r}, "
        f"expected {server.delivery_hash.hex()!r}"
    )
    assert msg["destination_hash"] == client.delivery_hash.hex(), (
        f"destination_hash mismatch: got {msg['destination_hash']!r}, "
        f"expected {client.delivery_hash.hex()!r}"
    )

    # ---- Outbound state: server should observe `delivered` --------
    deadline = time.time() + 20.0
    final_state = None
    while time.time() < deadline:
        final_state = server.message_state(message_hash)
        if final_state == "delivered":
            break
        time.sleep(0.2)

    assert final_state == "delivered", (
        f"server ({server_impl}) outbound state for direct message "
        f"is {final_state!r}, expected 'delivered' within 20s"
    )
