"""LXMF FIELD_FILE_ATTACHMENTS roundtrip across (server, client) impls.

LXMF's `fields` dict uses integer keys (0..255) and msgpack-able values
that range from primitives to nested lists/dicts. FIELD_FILE_ATTACHMENTS
(0x05) is the canonical interop case: each attachment is the pair
``[filename_str, data_bytes]``. The cross-impl risk is encoding —
both sides must serialize the same dict to the same msgpack bytes
inside the LXMF wire format, and on the receive side both must
expose the identical tagged shape on the bridge wire.

Test sends a small attachment via DIRECT (the path large attachments
will eventually flow through), asserts:
  - the inbox entry's ``fields`` dict surfaces FIELD_FILE_ATTACHMENTS
    keyed as the string ``"5"`` (decimal field id),
  - the attachment list contains exactly one entry ``[filename, hex]``,
  - filename + hex bytes match what the sender supplied.
"""

import secrets
import time

# Mirrors LXMF/LXMF.py FIELD_FILE_ATTACHMENTS = 0x05.
FIELD_FILE_ATTACHMENTS = 5


def test_file_attachment_roundtrip(server_impl, client_impl, pipe_pair):
    server, client = pipe_pair

    filename = f"attach-{secrets.token_hex(4)}.bin"
    payload = secrets.token_bytes(256)
    content = "see attached"
    title = "with attachment"

    # Bridge wire format: each attachment element is a list of two
    # tagged values — a string (filename) and bytes (payload). The
    # bridge will rebuild the canonical Python-LXMF
    # `[[filename_str, data_bytes], ...]` shape on the send side.
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
        f"message_hash with attachment payload"
    )

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
        f"expected exactly 1 within 20s"
    )

    msg = received[0]
    assert msg["content"] == content
    assert msg["title"] == title
    assert msg["method"] == "direct"

    # Field surface assertions ------------------------------------
    msg_fields = msg.get("fields") or {}
    key = str(FIELD_FILE_ATTACHMENTS)
    assert key in msg_fields, (
        f"missing FIELD_FILE_ATTACHMENTS ({key}) in inbox fields. "
        f"Got keys: {list(msg_fields.keys())!r}"
    )

    attachments = msg_fields[key]
    assert isinstance(attachments, list) and len(attachments) == 1, (
        f"FIELD_FILE_ATTACHMENTS shape wrong: {attachments!r}"
    )
    entry = attachments[0]
    assert isinstance(entry, list) and len(entry) == 2, (
        f"attachment entry shape wrong (expected [filename, hex]): {entry!r}"
    )
    got_filename, got_hex = entry
    assert got_filename == filename, (
        f"filename mismatch: got {got_filename!r}, expected {filename!r}"
    )
    assert got_hex == payload.hex(), (
        f"attachment bytes mismatch: got len(hex)={len(got_hex)}, "
        f"expected len(hex)={len(payload.hex())}"
    )
