"""DIRECT delivery with text + attachment payload over Resource transfer.

Stacks two Phase 2 axes simultaneously: a multi-KB content body
(forces the sender to fall back from single-packet to Resource
transfer) AND a non-empty FIELD_FILE_ATTACHMENTS payload (forces field
encoding to round-trip through the same Resource fragmentation path).

Catches divergences neither single-axis test would: e.g. msgpack
encoding of fields-with-bytes inside a packed LXMessage that's then
fragmented across resource parts. Either side mis-encoding the inner
bytes, the resource segmenter mis-aligning on a payload boundary, or
the receiver mis-classifying the resource-delivered method would all
fail this test even when each individual axis passes.
"""

import secrets
import time

FIELD_FILE_ATTACHMENTS = 5


def test_combined_text_and_attachment_over_resource(server_impl, client_impl, pipe_pair):
    server, client = pipe_pair

    # 4 KB text body — comfortably above LINK_PACKET_MAX_CONTENT.
    content = "C" * 4000 + "-tail-" + secrets.token_hex(16)
    title = f"combined-{secrets.token_hex(4)}"

    # 1 KB attachment payload alongside the body.
    filename = f"both-{secrets.token_hex(4)}.bin"
    payload = secrets.token_bytes(1024)

    fields = {
        str(FIELD_FILE_ATTACHMENTS): [
            [
                {"str": filename},
                {"bytes": payload.hex()},
            ]
        ]
    }

    message_hash = server.send_direct(
        recipient_hash=client.delivery_hash,
        content=content,
        title=title,
        fields=fields,
    )
    assert message_hash, (
        f"server.send_direct ({server_impl}) returned empty "
        f"message_hash for the combined payload"
    )

    deadline = time.time() + 45.0
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
        f"expected exactly 1 within 45s"
    )

    msg = received[0]
    assert msg["content"] == content, "content body mismatch"
    assert msg["title"] == title
    assert msg["method"] == "direct", (
        f"method mismatch: got {msg['method']!r}, expected 'direct' — "
        f"a combined large+attachment payload arriving as opportunistic "
        f"means the sender silently downgraded the path."
    )

    msg_fields = msg.get("fields") or {}
    key = str(FIELD_FILE_ATTACHMENTS)
    assert key in msg_fields, (
        f"FIELD_FILE_ATTACHMENTS missing on the inbox entry. "
        f"Got fields keys: {list(msg_fields.keys())!r}"
    )
    attachments = msg_fields[key]
    assert isinstance(attachments, list) and len(attachments) == 1
    got_filename, got_hex = attachments[0]
    assert got_filename == filename
    assert got_hex == payload.hex(), "attachment payload bytes mismatch"

    # ---- Outbound state -----------------------------------------
    deadline = time.time() + 45.0
    final_state = None
    while time.time() < deadline:
        final_state = server.message_state(message_hash)
        if final_state == "delivered":
            break
        time.sleep(0.2)
    assert final_state == "delivered", (
        f"server ({server_impl}) outbound state for combined direct "
        f"is {final_state!r}, expected 'delivered' within 45s"
    )
