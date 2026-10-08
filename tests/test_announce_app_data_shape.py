"""Announce app_data must be a 3-element LXMF v0.5+ peer_data array.

Reference contract (python LXMF, LXMRouter.py:1055-1058):

    supported_functionality = [SF_COMPRESSION]      # [0x00]  (LXMF.py:143)
    peer_data = [display_name, stamp_cost, supported_functionality]
    return msgpack.packb(peer_data)

Every LXMF delivery announce carries a v0.5+ app_data that is a 3-element
msgpack array: the display name, the advertised stamp cost, and the
supported-functionality flags list. A peer reads the third element to infer
capabilities (e.g. ``compression_support_from_app_data`` checks whether
``SF_COMPRESSION`` is in ``peer[2]``).

The LXMF-kt #38 divergence (LXMRouter.kt packAnnounceAppData, line 1808):
``packer.packArrayHeader(2)`` emits only ``[display_name, stamp_cost]`` -- the
``supported_functionality`` element is missing. A strict v0.5+ reader that
expects ``peer[2]`` gets an IndexError / short-array, and
``compression_support_from_app_data`` over-infers capability from the short
array. The wire bytes differ from the reference.

This test pins the 3-element shape. The SERVER impl is the announcer (the
impl whose app_data wire bytes are under test); the CLIENT impl recalls the
raw app_data bytes the server emitted and the test decodes them. The
interesting pair is server=kotlin (the buggy 2-element emitter) with a
recall-capable client; the python server row is the green control.

The recall is done on the client's local RNS (``lxmf_recall_app_data`` reads
the recalling side's path-table app_data), so the CLIENT must be a
recall-capable impl (python or microlxmf) - kotlin and swift skip, matching
the existing test_announce_app_data coverage note.
"""

import os
import sys
import time

import pytest

# Match conftest.py's PYTHON_RNS_PATH resolution so this test imports the same
# in-tree RNS (and its msgpack) the rest of the suite uses.
sys.path.insert(
    0,
    os.environ.get("PYTHON_RNS_PATH", os.path.expanduser("~/repos/Reticulum")),
)
import RNS.vendor.umsgpack as msgpack  # noqa: E402

SF_COMPRESSION = 0x00  # LXMF.py:143


def test_announce_app_data_is_three_element_peer_data(server_impl, client_impl, pipe_pair):
    """The server's announce app_data must decode to a 3-element msgpack
    list ``[display_name, stamp_cost, supported_functionality]`` with
    ``supported_functionality == [SF_COMPRESSION]``, matching the reference
    python LXMF wire shape."""
    # The recall reads the client's local RNS path table, so the client must
    # be a recall-capable impl. kotlin/swift bridges have no
    # lxmf_recall_app_data command; skip them as the recall side.
    if client_impl not in ("python", "microlxmf"):
        pytest.skip(
            f"client impl {client_impl!r} does not implement "
            f"lxmf_recall_app_data; recall-capable impls are "
            f"python and microlxmf"
        )

    server, client = pipe_pair

    server.announce()
    client.announce()

    server_dest_hex = server.delivery_hash.hex()

    # Wait for the client to learn the server's announce (path table populated).
    deadline = time.time() + 10.0
    has_path = False
    while time.time() < deadline:
        has_path = client.bridge.execute(
            "lxmf_has_path", destination_hash=server_dest_hex
        ).get("has_path")
        if has_path:
            break
        time.sleep(0.2)
    assert has_path, (
        f"client ({client_impl}) never saw server ({server_impl})'s announce "
        f"within 10s - fixture did not converge"
    )

    # Pull the raw app_data the client recorded for the server.
    resp = client.bridge.execute(
        "lxmf_recall_app_data", destination_hash=server_dest_hex
    )
    size = resp["size"]
    hex_str = resp["hex"]
    assert size > 0, (
        f"client ({client_impl}) recalled empty app_data for server "
        f"({server_impl}); announce was not processed or app_data was "
        f"stripped entirely"
    )

    raw = bytes.fromhex(hex_str)
    try:
        peer_data = msgpack.unpackb(raw)
    except Exception as e:
        pytest.fail(
            f"server ({server_impl}) emitted app_data that is not valid "
            f"msgpack: {type(e).__name__}: {e}; first 96 bytes hex: "
            f"{hex_str[:96]}"
        )

    assert isinstance(peer_data, list), (
        f"server ({server_impl}) app_data is not a msgpack list: "
        f"{type(peer_data).__name__} = {peer_data!r}"
    )

    # THE contract: 3 elements [display_name, stamp_cost,
    # supported_functionality]. The LXMF-kt #38 bug emits 2 elements
    # (packArrayHeader(2)), omitting supported_functionality.
    assert len(peer_data) == 3, (
        f"server ({server_impl}) announce app_data has {len(peer_data)} "
        f"element(s); the reference python LXMF (LXMRouter.py:1055-1058) "
        f"emits a 3-element peer_data "
        f"[display_name, stamp_cost, supported_functionality]. A 2-element "
        f"array is the LXMF-kt packAnnounceAppData divergence "
        f"(packArrayHeader(2)) and breaks v0.5+ readers that index "
        f"peer[2] for supported_functionality. decoded: {peer_data!r}"
    )

    # The third element must be the supported-functionality list, and must
    # include SF_COMPRESSION (0x00) - python always advertises it.
    supported_functionality = peer_data[2]
    assert isinstance(supported_functionality, list), (
        f"server ({server_impl}) peer_data[2] (supported_functionality) is "
        f"not a list: {type(supported_functionality).__name__} = "
        f"{supported_functionality!r}; expected a msgpack list"
    )
    assert SF_COMPRESSION in supported_functionality, (
        f"server ({server_impl}) peer_data[2] does not include "
        f"SF_COMPRESSION ({SF_COMPRESSION:#x}): {supported_functionality!r}. "
        f"Reference python always advertises [SF_COMPRESSION]. "
        f"decoded peer_data: {peer_data!r}"
    )
