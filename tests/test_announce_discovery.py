"""LXMF announce reception over a pipe-connected pair.

Phase 1 sanity check that the cross-impl interop has any chance of
working: the two bridges set up a PipeInterface between them, both
emit LXMF delivery announces, and each side observes the other's
announce land in its RNS path table.

Tight assertion: the receiver's path entry MUST contain the sender's
delivery destination hash exactly. Anything looser (e.g. "some path
exists") would let an off-by-one in the announce parser slip past.

Parametrized over every (server_impl, client_impl) pair the conftest
detected — two impls produce 4 trios, three impls produce 9, etc.
"""

import time


def test_lxmf_announce_received_via_pipe(server_impl, client_impl, pipe_pair):
    """Server and client are pipe-connected. Each announces. Each sees
    the other's delivery destination via path discovery."""
    server, client = pipe_pair

    # The pipe_pair fixture already issued an initial announce on each
    # side and waited 3 seconds for path convergence. We re-announce
    # here to make the test self-contained — if the fixture's settle
    # window was somehow racy, we get a fresh chance.
    server.announce()
    client.announce()

    # Path discovery via the bridge goes through the bridge's own state
    # check. Phase 1 doesn't ship a dedicated "has_path" command; we
    # use the side-effect of trying to send: if the path is unknown,
    # send_opportunistic raises. For the announce test we instead
    # check that BOTH sides can subsequently send (proving each
    # learned the other's identity from the announce).
    deadline = time.time() + 10.0
    last_err = None
    while time.time() < deadline:
        try:
            # Server -> client: proves server learned client's announce
            sc_hash = server.send_opportunistic(
                recipient_hash=client.delivery_hash,
                content="announce probe s->c",
            )
            assert sc_hash, (
                f"server.send_opportunistic returned empty hash; "
                f"sender ({server_impl}) failed to pack the message."
            )
            # Client -> server: proves client learned server's announce
            cs_hash = client.send_opportunistic(
                recipient_hash=server.delivery_hash,
                content="announce probe c->s",
            )
            assert cs_hash, (
                f"client.send_opportunistic returned empty hash; "
                f"sender ({client_impl}) failed to pack the message."
            )
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    else:
        raise AssertionError(
            f"Bidirectional announce discovery did not converge within "
            f"10s for ({server_impl}, {client_impl}). Last error: {last_err!r}"
        )

    # Tight inverse assertion: the messages we just sent must arrive.
    # If the announce was processed but the sender's path lookup picked
    # up the wrong entry, send would have raised — but defense in depth.
    deadline = time.time() + 10.0
    server_inbox = []
    client_inbox = []
    while time.time() < deadline:
        server_inbox += server.drain_received()
        client_inbox += client.drain_received()
        if server_inbox and client_inbox:
            break
        time.sleep(0.2)

    assert client_inbox, (
        f"Client ({client_impl}) did not receive announce-probe message "
        f"from server ({server_impl}) within 10s; announce flow broke at "
        f"the receive end."
    )
    assert server_inbox, (
        f"Server ({server_impl}) did not receive announce-probe message "
        f"from client ({client_impl}) within 10s; announce flow broke at "
        f"the receive end."
    )

    # Hash equality: each receiver's source_hash for the inbound message
    # must match the SENDER's delivery hash. This is the actual
    # cross-impl conformance check — if the announce parser corrupts
    # the source identity bits, the source_hash won't match and the
    # test fails on this line rather than reporting a mysterious
    # delivery failure later.
    assert client_inbox[0]["source_hash"] == server.delivery_hash.hex(), (
        f"Client received message with source_hash="
        f"{client_inbox[0]['source_hash']}, expected "
        f"{server.delivery_hash.hex()} (server's delivery hash). "
        f"Announce flow corrupted the source identity."
    )
    assert server_inbox[0]["source_hash"] == client.delivery_hash.hex(), (
        f"Server received message with source_hash="
        f"{server_inbox[0]['source_hash']}, expected "
        f"{client.delivery_hash.hex()} (client's delivery hash). "
        f"Announce flow corrupted the source identity."
    )
