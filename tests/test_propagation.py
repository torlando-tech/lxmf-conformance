"""LXMF PROPAGATED delivery via a propagation node.

Three-bridge topology (`tcp_trio`):

    sender → propagation_node (python) → receiver

Sender submits a PROPAGATED LXMF; the message is uploaded to the PN
and stored there. Receiver later runs `lxmf_sync_inbound` to pull
queued messages from the PN. The receiver's regular delivery callback
fires for the synced message, so it lands in the inbox alongside any
opportunistic / direct messages.

Because swift LXMF doesn't host propagation nodes, the PN is always
python — sender and receiver vary. Two impls (python, swift) yields
4 trios.
"""

import secrets
import time

import pytest


# Override the 60s global pytest-timeout for propagation. Worst-case
# budget = ~15s tcp_trio fixture + 60s upload deadline + 1s settle +
# 3×30s sync_inbound retries + 2×2s noPath backoff + ~15s drain ≈ 185s.
# The retry path and a slow upload are unlikely to coincide, but 240s
# leaves visible headroom against both at once while still catching a
# real hang.
@pytest.mark.timeout(240)
def test_propagated_message_via_pn(sender_impl, receiver_impl, tcp_trio):
    sender, pn, receiver = tcp_trio

    content = f"prop-{secrets.token_hex(8)}"
    title = f"prop-title-{secrets.token_hex(4)}"

    message_hash = sender.send_propagated(
        recipient_hash=receiver.delivery_hash,
        content=content,
        title=title,
    )
    assert message_hash, (
        f"sender.send_propagated ({sender_impl}) returned empty "
        f"message_hash"
    )

    # ---- Sender uploads to the PN -------------------------------
    # The sender's outbound state needs to reach `sent` (uploaded to
    # the PN). A `delivered` state requires the recipient to also
    # sync, but PROPAGATED on the sender side is "done" once the PN
    # accepts the upload.
    #
    # Deadline was 30s and held locally (~7-11s per kotlin->kotlin
    # propagation) but on GitHub-hosted ubuntu runners (2 cores,
    # ~7GB RAM, multiple JVM bridges + lxmd contending for the same
    # CPU) the resource transfer routinely needs 30-50s. The transfer
    # itself completes — the 30s window just clipped it. 60s gives
    # ~2x local headroom against the ~3x observed CI slowdown.
    upload_deadline = time.time() + 60.0
    upload_state = None
    while time.time() < upload_deadline:
        upload_state = sender.message_state(message_hash)
        if upload_state in {"sent", "delivered"}:
            break
        time.sleep(0.2)
    assert upload_state in {"sent", "delivered"}, (
        f"sender ({sender_impl}) outbound state for propagated "
        f"message is {upload_state!r}, expected `sent` or `delivered` "
        f"within 60s — sender failed to upload to PN"
    )

    # ---- Receiver syncs from PN --------------------------------
    # Allow a brief settle so the PN has the message persisted before
    # the receiver dials in for sync.
    time.sleep(1.0)
    # Retry sync with re-announce on noPath: in 3-bridge transport
    # topology the PN's announce can race the receiver's dial-in,
    # leaving the receiver with a missing path entry on the first
    # sync attempt. A re-announce by the PN nudges the table.
    sync_result = None
    for attempt in range(3):
        sync_result = receiver.sync_inbound(timeout_sec=30.0)
        assert sync_result is not None, "sync_inbound returned None"
        # Python bridge returns int final_state; swift returns string.
        # Either side reports trouble if it couldn't even establish the
        # link; bail on retry only for that early-failure case.
        is_no_path = (
            sync_result.get("error") == "noPath"
            or "noPath" in str(sync_result.get("final_state", ""))
        )
        if not is_no_path:
            break
        # Nudge the path table by re-announcing on all three sides
        # and waiting briefly before the next attempt.
        pn.bridge.execute("lxmf_announce")
        sender.bridge.execute("lxmf_announce")
        receiver.bridge.execute("lxmf_announce")
        time.sleep(2.0)

    # The python bridge returns numeric `final_state` (0=idle/done);
    # the swift bridge returns a string (`complete`, `idle`, `done`).
    # Either path is acceptable as long as the message lands.
    deadline = time.time() + 15.0
    received = []
    while time.time() < deadline:
        received += receiver.drain_received()
        if len(received) >= 1:
            time.sleep(0.5)
            received += receiver.drain_received()
            break
        time.sleep(0.2)

    assert len(received) == 1, (
        f"receiver ({receiver_impl}) received {len(received)} messages "
        f"after sync, expected exactly 1. sync_result={sync_result!r}"
    )

    msg = received[0]
    assert msg["content"] == content, (
        f"content mismatch: got {msg['content']!r}, expected {content!r}"
    )
    assert msg["title"] == title, (
        f"title mismatch: got {msg['title']!r}, expected {title!r}"
    )
    assert msg["method"] == "propagated", (
        f"method mismatch: got {msg['method']!r}, expected 'propagated'"
    )
    assert msg["source_hash"] == sender.delivery_hash.hex(), (
        f"source_hash mismatch: got {msg['source_hash']!r}, "
        f"expected {sender.delivery_hash.hex()!r}"
    )
    assert msg["destination_hash"] == receiver.delivery_hash.hex(), (
        f"destination_hash mismatch: got {msg['destination_hash']!r}"
    )
