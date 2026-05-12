"""Receiver's announced ``stamp_cost`` must auto-configure the sender's outbound stamp.

Production failure this pins:

  Sideband (python LXMF) is configured with
  ``lxmf_require_stamps=True`` and ``lxmf_inbound_stamp_cost = N``.
  The receiver's destination announces with ``stamp_cost=N`` in the
  v0.5+ msgpack app_data array. A peer (Columba, built on LXMF-kt)
  sends a DIRECT message back. The peer is *expected* to:

    1. Receive the announce.
    2. Process it through its lxmf.delivery announce handler, which
       calls ``LXMRouter.update_stamp_cost(destination_hash, N)`` and
       populates ``outbound_stamp_costs``.
    3. On the next ``handle_outbound`` for that destination, copy the
       cached cost onto the LXMessage and call ``getStamp()`` /
       ``get_stamp()`` to produce a stamped wire.

  If any of those three steps is broken, the message goes out with no
  stamp slot. Sideband's ``LXMRouter.lxmf_delivery`` runs
  ``validate_stamp(N)`` against ``message.stamp == None``, returns
  False, and drops with "Dropping {message} with invalid stamp" before
  the application delivery callback fires. The sender sees a
  Reticulum-level delivery proof and assumes success; the receiver
  never surfaces the message.

LXMF-kt #33 was exactly this — the kotlin port had registered an
announce handler for ``lxmf.propagation`` but not for
``lxmf.delivery``, so step (2) silently no-op'd and every Columba
message to a stamp-enforcing Sideband was dropped. Conformance suite
had no test for this end-to-end path; this test closes that gap.

Test strategy:

  * Receiver (server-role) registers with ``inbound_stamp_cost=4`` and
    ``enforce_stamps()``, then announces — appearing on the wire
    exactly like a Sideband install with stamps required.
  * Sender (client-role, parametrized across detected impls) sees the
    announce during the fixture's settle window, then sends DIRECT.
  * Receiver inbox MUST contain the message. If the sender impl skips
    the auto-stamp step, enforce_stamps drops the message and the
    inbox stays empty.

Cost = 4 is the lowest cost that produces a meaningful PoW search
(below that any random stamp passes on the first try). Anything higher
just slows the test without strengthening the contract.
"""

import time

import pytest


def test_receiver_announced_stamp_cost_auto_stamps_sender_outbound(
    server_impl, client_impl, pipe_pair_enforce_stamps
):
    """End-to-end: receiver enforces stamps; sender sees announce; sender's
    DIRECT message arrives at receiver's inbox (proves auto-stamp wiring)."""
    server, client = pipe_pair_enforce_stamps

    content = "stamp-enforced direct from " + client_impl
    msg_hash = client.send_direct(server.delivery_hash, content=content)
    assert msg_hash, (
        f"client.send_direct ({client_impl}) returned empty message_hash — "
        "sender did not finish packing the LXMessage."
    )

    # Poll the receiver's inbox. 15s is comfortably above loopback DIRECT
    # timing (Link establish + transfer + delivery callback fires in
    # well under 5s on healthy CI runners), generous enough to absorb
    # GC pauses or scheduling jitter without flaking. If the sender
    # didn't auto-stamp from the announce, the receiver's enforce_stamps
    # path drops the message before the delivery callback ever fires —
    # the inbox stays empty for the entire window and the assertion below
    # explains the production-failure connection.
    deadline = time.time() + 15.0
    received = []
    while time.time() < deadline:
        received = server.drain_received()
        if received:
            break
        time.sleep(0.1)

    assert len(received) == 1, (
        f"server (python with enforce_stamps + inbound_stamp_cost=4) did "
        f"not receive a DIRECT message from {client_impl} sender within 15s. "
        f"Inbox: {received}\n\n"
        f"This is the LXMF-kt#33 production failure mode: the sender did "
        f"not auto-configure its outbound stampCost from the receiver's "
        f"announced cost. Check that the sender impl's LXMF router "
        f"registers an announce handler for the 'lxmf.delivery' aspect, "
        f"calls update_stamp_cost(destination_hash, N) from the announce "
        f"app_data, and consults that cache in handle_outbound."
    )

    msg = received[0]
    assert msg["content"] == content, (
        f"content round-trip corrupted: sent {content!r}, "
        f"received {msg['content']!r}"
    )
    assert msg["source_hash"] == client.delivery_hash.hex(), (
        f"source_hash mismatch: expected {client.delivery_hash.hex()}, "
        f"received {msg['source_hash']}"
    )
    assert msg["destination_hash"] == server.delivery_hash.hex(), (
        f"destination_hash mismatch: expected {server.delivery_hash.hex()}, "
        f"received {msg['destination_hash']}"
    )
