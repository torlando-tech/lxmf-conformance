"""LXMF announce app_data extraction respects RNS ratchet framing.

Modern RNS (>= 0.7) prepends a 32-byte X25519 ratchet public key to
identity announces when the announcer has ratchets enabled (default
in upstream Python today). The wire format becomes:

    public_key (64) +
    name_hash (10) +
    random_hash (10) +
    ratchet (32)         ← only present when packet.context_flag == FLAG_SET
    signature (64) +
    app_data (variable)

A receiver that doesn't know about ratchets will mis-slice the packet
and return ``ratchet || app_data`` as ``app_data`` from
``Identity.recall_app_data(dest)``. Downstream parsers (eg
``LXMF.display_name_from_app_data``) then fall into the legacy
raw-UTF-8 branch and render 32 bytes of binary garbage as a prefix on
the user's display name. That was the original surfaced symptom:
``"{ 5AMUmSw2Torlando - Columba"`` instead of ``"Torlando - Columba"``.

This test pins the invariant: after a Python sender (which announces
with the ratchet bit set) and a microlxmf receiver converge, the
receiver's recalled ``app_data`` for the sender starts with an LXMF
v0.5+ msgpack array marker (``0x90``-``0x9f`` or ``0xdc``) and decodes
into a peer_data list whose first element is the announced display
name as UTF-8 bytes — NOT random ratchet bytes.

``lxmf_recall_app_data`` is implemented on both python and microlxmf
bridges, so the test runs across the full sender × receiver matrix.
The trivial python→python pair self-confirms python's reference
behavior; the interesting cross-impl pair is microlxmf→python, which
asserts python correctly parses whatever microLXMF emits on the wire.
"""

import os
import sys
import time

import pytest

# Match conftest.py's PYTHON_RNS_PATH resolution so this test sees the
# same in-tree RNS the rest of the conformance suite uses. CI checks
# out RNS into the workspace and exports PYTHON_RNS_PATH; locally we
# fall back to ~/repos/Reticulum. Without this, importing RNS in the
# pytest process raises ModuleNotFoundError on CI (RNS isn't pip-
# installed in the runner image) — see the test-collection failure
# that surfaced this gap.
sys.path.insert(
    0,
    os.environ.get("PYTHON_RNS_PATH", os.path.expanduser("~/repos/Reticulum")),
)
import RNS.vendor.umsgpack as msgpack  # noqa: E402


def _hex_first_byte(hex_str):
    if not hex_str:
        return None
    return int(hex_str[:2], 16)


def test_announce_app_data_strips_ratchet(server_impl, client_impl, pipe_pair):
    # ``lxmf_recall_app_data`` is currently implemented only on the
    # python and microlxmf bridges (the test docstring above states
    # this explicitly). Swift's bridge has no such command and kotlin
    # is a Phase-2 placeholder — both would surface as a
    # ``BridgeError("unknown command")`` rather than a clean skip when
    # they appear in the parametrized matrix. Skip non-recall-capable
    # client impls up front so the matrix stays loud about real
    # regressions instead of impl-coverage gaps.
    if client_impl not in ("python", "microlxmf"):
        pytest.skip(
            f"client impl {client_impl!r} does not implement "
            f"lxmf_recall_app_data; recall-capable impls are "
            f"python and microlxmf"
        )

    server, client = pipe_pair

    # Re-announce both sides in case the fixture's startup window
    # raced the ratchet-enabled announce.
    server.announce()
    client.announce()

    server_dest_hex = server.delivery_hash.hex()

    # Wait for the client (microlxmf) to learn the server's announce.
    # The bridge's lxmf_has_path returns true once the server's
    # destination is in the path table.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        has_path = client.bridge.execute(
            "lxmf_has_path", destination_hash=server_dest_hex
        ).get("has_path")
        if has_path:
            break
        time.sleep(0.2)
    assert has_path, (
        f"client ({client_impl}) never saw server ({server_impl})'s "
        f"announce within 10s — fixture didn't converge"
    )

    # Pull the raw app_data the client recorded for the server.
    resp = client.bridge.execute(
        "lxmf_recall_app_data", destination_hash=server_dest_hex
    )
    size = resp["size"]
    hex_str = resp["hex"]
    assert size > 0, (
        f"client ({client_impl}) recalled empty app_data for server "
        f"({server_impl}); announce was either not processed or "
        f"app_data was stripped entirely"
    )

    # The first byte MUST be a msgpack array marker (LXMF v0.5+ peer_data
    # is `[display_name, stamp_cost]` packed as msgpack). A ratchet
    # leak would put a random byte from the X25519 pubkey here.
    first_byte = _hex_first_byte(hex_str)
    is_fixarray = 0x90 <= first_byte <= 0x9f
    is_array16 = first_byte == 0xdc
    assert is_fixarray or is_array16, (
        f"client ({client_impl}) recalled app_data first byte = "
        f"0x{first_byte:02x}, expected msgpack array marker "
        f"(0x90-0x9f or 0xdc). Likely regression: ratchet bytes "
        f"leaking into app_data because validate_announce isn't "
        f"checking packet.context_flag == FLAG_SET to skip the "
        f"32-byte ratchet field. app_data hex (first 96): "
        f"{hex_str[:96]}"
    )

    # Decode the peer_data and verify the display_name field.
    raw = bytes.fromhex(hex_str)
    try:
        peer_data = msgpack.unpackb(raw)
    except Exception as e:
        raise AssertionError(
            f"client ({client_impl}) recalled app_data did not decode "
            f"as msgpack: {e}; hex={hex_str[:96]}"
        )
    assert isinstance(peer_data, list), (
        f"peer_data should be a msgpack list; got {type(peer_data)} "
        f"= {peer_data!r}"
    )
    assert len(peer_data) >= 1, (
        f"peer_data list should have >=1 element; got {peer_data!r}"
    )
    print(f"  peer_data shape: type={type(peer_data).__name__} "
          f"len={len(peer_data)} elements={[type(e).__name__ for e in peer_data]}")
    print(f"  peer_data: {peer_data!r}")

    # The shape of peer_data depends on which destination this announce
    # was for. Two known shapes in LXMF:
    #
    #   delivery destination (lxmf.delivery): [display_name, stamp_cost]
    #     display_name is utf-8 bytes (or None), stamp_cost is int (or None).
    #     This is what `LXMRouter.get_announce_app_data` produces.
    #
    #   propagation node     (lxmf.propagation): [legacy_pn_flag, timebase, node_state, ...]
    #     [bool, int, bool, ...]
    #     This is what `LXMRouter.get_propagation_node_app_data` produces.
    #
    # The pipe_pair fixture aliases `server.delivery_hash` to the
    # LXMF delivery destination, so we expect the delivery shape.
    # If we get a bool/int as element 0, the server announced its
    # propagation destination under what we thought was its delivery
    # hash — which would be a different bug.
    first = peer_data[0]
    if isinstance(first, (bool, int)) and not isinstance(first, bytes):
        raise AssertionError(
            f"peer_data[0] is {type(first).__name__} = {first!r}, "
            f"expected utf-8 bytes (delivery-announce display_name). "
            f"This usually means we recalled app_data for a "
            f"propagation-node announce instead of a delivery "
            f"announce. server_impl={server_impl} peer_data={peer_data!r}"
        )
    if first is None:
        # display_name=None is allowed by LXMF, but the fixture
        # explicitly sets f'server-{server_impl}' via lxmf_init.
        # If we got None, the impl ignored that argument.
        pytest.skip(
            f"server ({server_impl}) announced with display_name=None; "
            f"its bridge probably doesn't propagate the lxmf_init "
            f"display_name argument into get_announce_app_data — "
            f"separate concern from the ratchet test"
        )

    decoded = first.decode("utf-8")
    expected = f"server-{server_impl}"
    assert decoded == expected, (
        f"client recalled display_name={decoded!r}, expected "
        f"{expected!r}. Either the announcer used a different name "
        f"or the bytes are off — check whether ratchet bytes are "
        f"corrupting the start of app_data."
    )
