"""LXMF payload-format conformance tests (byte-level decoder).

Targets the LXMessage msgpack payload-shape contract that EVERY LXMF
implementation MUST handle uniformly. Crafts raw wire bytes directly
(no live RNS / LXMF stack) and feeds them to each impl's
``lxmf_decode_bytes`` bridge command, comparing decoded structure
and computed hash.

Why a separate test file instead of folding into existing
``test_opportunistic.py`` etc.: those tests exercise the wire-E2E
path, which means each impl's encoder produces the bytes — so they
only cover encodings the SUT itself emits. A whole class of
interop bugs lives in payload shapes that ONE impl emits but ANOTHER
never does. iOS LXMF emits ``fields = msgpack Nil (0xc0)`` for
empty fields; Python and Kotlin senders both emit ``empty Map (0x80)``.
Without a byte-level decoder test, the iOS-Nil shape never enters
the suite from a Python or Kotlin sender, and a parser that rejects
Nil silently passes every wire-E2E test while quietly dropping
every iOS-originated message in the wild. (Real bug: see Columba
investigation 2026-04-30 — kotlin's ``LXMessage.unpackFields`` threw
``MessageTypeException: Expected Map, but got Nil (c0)`` for two
weeks before this test caught it.)

Reference for canonical semantics:
  python LXMF/LXMF/LXMessage.py:734-779 (unpack_from_bytes)
  python LXMF/LXMF/LXMessage.py:220-224 (set_fields tolerates None)
  python LXMF/LXMF/LXMessage.py:742-747 (only re-pack when stamp present)

Cases covered (all six combinations of array-size × fields-shape):

  +-----------+----------------+-------------------+-----------------+
  | array     | fields shape   | wire byte (3rd    | who emits this  |
  | length    |                | element of payld) | naturally?      |
  +-----------+----------------+-------------------+-----------------+
  | 4 (no     | empty Map      | 0x80              | python, kotlin  |
  |   stamp)  | empty Nil      | 0xc0              | iOS LXMF        |
  |           | non-empty Map  | 0x81+             | any (with       |
  |           |                |                   | attachments)    |
  +-----------+----------------+-------------------+-----------------+
  | 5 (with   | empty Map      | 0x80              | python (rare)   |
  |   stamp)  | empty Nil      | 0xc0              | iOS w/ stamp    |
  |           | non-empty Map  | 0x81+             | any with stamp  |
  +-----------+----------------+-------------------+-----------------+

The hash invariant we assert: ``message_hash`` returned by the
bridge MUST equal what the sender would have computed
(``SHA256(dest + source + 4-element-packed-payload)``). For the
no-stamp cases the bridge must use the original packed bytes
verbatim — re-packing is a footgun that breaks hash matching for
any wire encoding the impl's encoder doesn't emit identically
(Nil vs empty Map being the original landmine).
"""

import hashlib
import os
import sys

import pytest

# Use the same RNS/LXMF checkout the bridges use, so we craft bytes
# with the SAME msgpack flavor (umsgpack) the reference uses.
_RNS_PATH = os.environ.get(
    "PYTHON_RNS_PATH", os.path.expanduser("~/repos/Reticulum")
)
if _RNS_PATH and _RNS_PATH not in sys.path:
    sys.path.insert(0, os.path.abspath(_RNS_PATH))

import RNS.vendor.umsgpack as umsgpack  # noqa: E402

# 16-byte destination/source hashes and a 64-byte signature. The
# decoder under test does not validate the signature (it can't,
# without identity material), so we use a stable dummy.
DEST_HASH = bytes.fromhex("00112233445566778899aabbccddeeff")
SOURCE_HASH = bytes.fromhex("ffeeddccbbaa99887766554433221100")
DUMMY_SIG = b"\x00" * 64
TIMESTAMP = 1700000000.0
TITLE_BYTES = b"hello"
CONTENT_BYTES = b"world payload bytes"


def _build_lxmf_bytes(payload_list):
    """Pack the LXMF payload list and prepend dest/source/sig.

    Crucially: msgpack.packb(payload_list) preserves Python-side type
    distinctions — None -> 0xc0 (Nil), {} -> 0x80 (Map). This is what
    lets us produce the iOS-style Nil-fields encoding without owning
    an iOS device.
    """
    packed_payload = umsgpack.packb(payload_list)
    return DEST_HASH + SOURCE_HASH + DUMMY_SIG + packed_payload, packed_payload


def _expected_hash(payload_list_4elem):
    """Compute SHA256(dest + source + msgpack.packb(payload_list_4elem)).

    Mirrors LXMessage.py:367-373. The hash is over the 4-element form
    even when the wire transmits 5 elements (stamp appended), so callers
    constructing 5-element payloads should pass payload_list[:4] here.
    """
    packed = umsgpack.packb(payload_list_4elem)
    return hashlib.sha256(DEST_HASH + SOURCE_HASH + packed).hexdigest()


# Synthetic stamp bytes for the 5-element cases. Decoder under test
# extracts these as opaque bytes — we don't need a valid PoW stamp.
SYNTHETIC_STAMP = bytes.fromhex("deadbeef" * 8)  # 32 bytes


# --------------------------------------------------------------------------- #
# Decode tests — six payload shapes, asserting decode + hash for each impl.
# --------------------------------------------------------------------------- #


def _decode_and_assert_basic(bridge, lxmf_bytes_hex, expected_hash, *,
                              expected_fields_was_nil, expected_fields_count,
                              expected_stamp_hex=None):
    """Common assertion block: decode succeeded + hash matched + shape matched."""
    resp = bridge.execute("lxmf_decode_bytes", lxmf_bytes=lxmf_bytes_hex)

    assert "decode_error" not in resp, (
        f"decode_bytes failed: {resp.get('decode_error')!r}\n"
        f"This means the impl rejects a valid LXMF wire encoding. The "
        f"most common cause is the decoder demanding fields=Map and "
        f"refusing fields=Nil — see test docstring for context."
    )

    # Hash MUST match — this is the load-bearing invariant. A mismatch
    # means the impl's repack-after-decode produces different bytes than
    # the original wire form, which would make signature validation fail
    # downstream even when the message decodes structurally.
    assert resp["message_hash"] == expected_hash, (
        f"message_hash mismatch:\n"
        f"  expected (sender-side):  {expected_hash}\n"
        f"  actual   (decoder-side): {resp['message_hash']}\n"
        f"This means the decoder's hash computation diverges from the "
        f"sender's. Likely cause: the decoder re-packs the payload via "
        f"its own encoder, and the encoder produces different bytes for "
        f"this fields shape (Nil vs empty Map is the canonical landmine). "
        f"Mirror python LXMessage.py:742-749 — only re-pack when stamp "
        f"is present, and preserve the original Nil-vs-Map encoding."
    )

    assert resp["destination_hash"] == DEST_HASH.hex()
    assert resp["source_hash"] == SOURCE_HASH.hex()
    assert resp["signature"] == DUMMY_SIG.hex()
    assert resp["title_hex"] == TITLE_BYTES.hex()
    assert resp["content_hex"] == CONTENT_BYTES.hex()
    assert resp["fields_was_nil"] is expected_fields_was_nil, (
        f"fields_was_nil flag wrong: expected {expected_fields_was_nil}, "
        f"got {resp['fields_was_nil']}. The decoder must distinguish "
        f"Nil from empty Map on the wire; collapsing them loses the "
        f"information needed to round-trip the hash for stamped messages."
    )
    assert resp["fields_count"] == expected_fields_count
    if expected_stamp_hex is None:
        assert resp["stamp"] in (None,), f"expected null stamp, got {resp['stamp']!r}"
    else:
        assert resp["stamp"] == expected_stamp_hex


# --- 4-element (no stamp) cases ---


def test_decode_4elem_empty_map(single_bridge):
    """Most common: a tiny opportunistic message with no fields (kotlin/python sender)."""
    payload = [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, {}]
    lxmf_bytes, _ = _build_lxmf_bytes(payload)
    expected = _expected_hash(payload)
    _decode_and_assert_basic(
        single_bridge, lxmf_bytes.hex(), expected,
        expected_fields_was_nil=False, expected_fields_count=0,
    )


def test_decode_4elem_nil_fields(single_bridge):
    """The iOS bug case: fields slot encoded as msgpack Nil (0xc0) instead of empty Map.

    This is what every iOS LXMF client emits for empty fields. A decoder
    that demands a Map header here will silently drop every iOS-originated
    message — exactly the production failure mode we caught on 2026-04-30.
    """
    payload = [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, None]
    lxmf_bytes, packed = _build_lxmf_bytes(payload)
    # Sanity: confirm we actually produced a Nil byte at the fields position.
    # The exact offset depends on title/content sizes; just check Nil exists
    # somewhere reasonable in the packed payload.
    assert b"\xc0" in packed, "test setup error: expected Nil byte in packed payload"
    expected = _expected_hash(payload)
    _decode_and_assert_basic(
        single_bridge, lxmf_bytes.hex(), expected,
        expected_fields_was_nil=True, expected_fields_count=0,
    )


def test_decode_4elem_with_fields(single_bridge):
    """Non-empty fields: e.g. a reply_to (field 16) referencing a message hash."""
    parent_hash = bytes.fromhex("11" * 32)
    payload = [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, {16: parent_hash}]
    lxmf_bytes, _ = _build_lxmf_bytes(payload)
    expected = _expected_hash(payload)
    _decode_and_assert_basic(
        single_bridge, lxmf_bytes.hex(), expected,
        expected_fields_was_nil=False, expected_fields_count=1,
    )


# --- 5-element (with stamp) cases ---


def test_decode_5elem_empty_map_with_stamp(single_bridge):
    """Stamp present + empty Map fields. Hash must be on the 4-element form."""
    payload_5 = [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, {}, SYNTHETIC_STAMP]
    lxmf_bytes, _ = _build_lxmf_bytes(payload_5)
    expected = _expected_hash(payload_5[:4])  # hash on 4-element form
    _decode_and_assert_basic(
        single_bridge, lxmf_bytes.hex(), expected,
        expected_fields_was_nil=False, expected_fields_count=0,
        expected_stamp_hex=SYNTHETIC_STAMP.hex(),
    )


def test_decode_5elem_nil_fields_with_stamp(single_bridge):
    """The hardest case: iOS-style Nil fields PLUS a stamp.

    Decoder must (a) accept Nil for fields, (b) re-pack the 4-element
    form for hash with the original Nil encoding preserved, NOT
    substituted with empty Map. A Map-substitution here makes the hash
    mismatch and breaks signature validation downstream.
    """
    payload_5 = [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, None, SYNTHETIC_STAMP]
    lxmf_bytes, _ = _build_lxmf_bytes(payload_5)
    expected = _expected_hash(payload_5[:4])
    _decode_and_assert_basic(
        single_bridge, lxmf_bytes.hex(), expected,
        expected_fields_was_nil=True, expected_fields_count=0,
        expected_stamp_hex=SYNTHETIC_STAMP.hex(),
    )


def test_decode_5elem_with_fields_and_stamp(single_bridge):
    """Stamp + non-empty fields. Common shape for proof-of-work messages with attachments."""
    parent_hash = bytes.fromhex("22" * 32)
    payload_5 = [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, {16: parent_hash}, SYNTHETIC_STAMP]
    lxmf_bytes, _ = _build_lxmf_bytes(payload_5)
    expected = _expected_hash(payload_5[:4])
    _decode_and_assert_basic(
        single_bridge, lxmf_bytes.hex(), expected,
        expected_fields_was_nil=False, expected_fields_count=1,
        expected_stamp_hex=SYNTHETIC_STAMP.hex(),
    )


# --------------------------------------------------------------------------- #
# Negative tests — decoder must report error gracefully, not crash bridge.
# --------------------------------------------------------------------------- #


def test_decode_truncated_bytes(single_bridge):
    """Less than dest+source+sig in the input — must return decode_error, not crash."""
    truncated = (DEST_HASH + SOURCE_HASH).hex()  # missing signature + payload
    resp = single_bridge.execute("lxmf_decode_bytes", lxmf_bytes=truncated)
    assert "decode_error" in resp, (
        f"expected decode_error for truncated input, got: {resp!r}. "
        f"A bridge that crashes here would also crash on a malformed wire "
        f"packet from a buggy or malicious peer — the decoder MUST be "
        f"hardened against partial input."
    )


def test_decode_too_few_payload_elements(single_bridge):
    """Payload array with <4 elements is malformed — must report decode_error."""
    # Pack a 3-element array — missing fields slot.
    short_payload = umsgpack.packb([TIMESTAMP, TITLE_BYTES, CONTENT_BYTES])
    lxmf_bytes = (DEST_HASH + SOURCE_HASH + DUMMY_SIG + short_payload).hex()
    resp = single_bridge.execute("lxmf_decode_bytes", lxmf_bytes=lxmf_bytes)
    assert "decode_error" in resp, (
        f"expected decode_error for <4-element payload, got: {resp!r}"
    )


def test_decode_garbage_msgpack(single_bridge):
    """Random bytes after the prefix — msgpack unpack must fail gracefully."""
    garbage = bytes.fromhex("ff" * 32)  # not valid msgpack
    lxmf_bytes = (DEST_HASH + SOURCE_HASH + DUMMY_SIG + garbage).hex()
    resp = single_bridge.execute("lxmf_decode_bytes", lxmf_bytes=lxmf_bytes)
    assert "decode_error" in resp, (
        f"expected decode_error for garbage msgpack, got: {resp!r}"
    )


# --------------------------------------------------------------------------- #
# Cross-impl consistency — every impl must produce the SAME hash for the
# same input. If a 7th impl ever joins the suite (Swift, Rust, etc.), this
# test catches drift on day one without any per-impl edits.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "case_name,payload",
    [
        ("4elem_empty_map", [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, {}]),
        ("4elem_nil_fields", [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, None]),
        ("4elem_with_fields", [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, {16: b"\x33" * 32}]),
        ("5elem_empty_map_stamp", [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, {}, SYNTHETIC_STAMP]),
        ("5elem_nil_fields_stamp", [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, None, SYNTHETIC_STAMP]),
        ("5elem_with_fields_stamp", [TIMESTAMP, TITLE_BYTES, CONTENT_BYTES, {16: b"\x44" * 32}, SYNTHETIC_STAMP]),
    ],
    ids=lambda x: x if isinstance(x, str) else "",
)
def test_decode_hash_matches_sender_expected(single_bridge, case_name, payload):
    """Every impl must produce the sender-expected hash for every payload shape.

    This is functionally a tighter version of the per-shape tests above:
    if the 4-element case fails, it fails here too with the case_name in
    the test ID, making it trivial to see which shape regressed at a
    glance in CI.
    """
    lxmf_bytes, _ = _build_lxmf_bytes(payload)
    expected = _expected_hash(payload[:4])  # hash always on 4-element form
    resp = single_bridge.execute("lxmf_decode_bytes", lxmf_bytes=lxmf_bytes.hex())
    assert "decode_error" not in resp, f"[{case_name}] decode failed: {resp.get('decode_error')!r}"
    assert resp["message_hash"] == expected, (
        f"[{case_name}] hash mismatch: expected {expected}, got {resp['message_hash']}"
    )
