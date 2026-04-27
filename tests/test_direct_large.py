"""DIRECT delivery of messages larger than LINK_PACKET_MAX_CONTENT.

When the packed LXMessage exceeds ~319 bytes (LXMF's
LINK_PACKET_MAX_CONTENT), the sender falls back from a single link
DATA packet to Resource transfer — a multi-packet streaming protocol
that fragments the payload across multiple link packets and
reassembles on the receiver. This is the highest-risk surface for
cross-impl divergence in DIRECT delivery: any byte-level mismatch in
fragmentation, transfer windows, hash-chains, or proof handling
breaks the trio.

The test ships with a 5 KB random payload — well above the single-
packet ceiling but small enough to keep the test bounded on
loopback. Receiver must surface `method == "direct"` (not a silent
upgrade to OPPORTUNISTIC) and the exact content bytes.
"""

import secrets
import time


def test_direct_large_message_resource_transfer(server_impl, client_impl, pipe_pair):
    """Server -> client direct message above LINK_PACKET_MAX_CONTENT.
    Client receives correct payload via Resource transfer; server's
    outbound state transitions to delivered."""
    server, client = pipe_pair

    # 5 KB payload triggers Resource transfer (well past the ~319 B
    # single-packet ceiling). Random tail keeps the assertion strict
    # — a stale buffer can't accidentally match.
    content = "L" * 5000 + secrets.token_hex(16)
    title = f"large-{secrets.token_hex(4)}"

    message_hash = server.send_direct(
        recipient_hash=client.delivery_hash,
        content=content,
        title=title,
    )
    assert message_hash, (
        f"server.send_direct ({server_impl}) returned empty "
        f"message_hash — sender did not finish packing the LXMessage."
    )

    # Resource transfer over loopback should land in a few seconds;
    # 30s is comfortably above that while still failing fast on a
    # broken transfer.
    deadline = time.time() + 30.0
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
        f"expected exactly 1 within 30s. Inbox sizes: "
        f"{[len(m.get('content', '')) for m in received]}"
    )

    msg = received[0]
    assert msg["content"] == content, (
        f"content byte mismatch: got len={len(msg['content'])}, "
        f"expected len={len(content)}"
    )
    assert msg["title"] == title, (
        f"title mismatch: got {msg['title']!r}, expected {title!r}"
    )
    assert msg["method"] == "direct", (
        f"method mismatch: got {msg['method']!r}, expected 'direct'. "
        f"A large message that surfaces as opportunistic on the "
        f"receiver indicates the sender silently downgraded the "
        f"method or the receiver mis-classified the inbound."
    )

    # ---- Outbound state: delivered ------------------------------
    deadline = time.time() + 30.0
    final_state = None
    while time.time() < deadline:
        final_state = server.message_state(message_hash)
        if final_state == "delivered":
            break
        time.sleep(0.2)

    assert final_state == "delivered", (
        f"server ({server_impl}) outbound state for large direct "
        f"message is {final_state!r}, expected 'delivered' within 30s"
    )
