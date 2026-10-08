"""Inbound stamp-enforcement DEFAULT must accept invalid-stamp messages.

Reference contract (python LXMF, LXMRouter.py:103):

    self._enforce_stamps = enforce_stamps   # default False

A router that advertises a stamp cost but does NOT call ``enforce_stamps()``
accepts inbound messages whose stamp is invalid or absent. ``lxmf_delivery``
validates the stamp (because the delivery destination's ``stamp_cost`` is set),
finds it invalid, and then falls into the "allowing anyway, since stamp
enforcement is disabled" branch (LXMRouter.py:1948-1951) instead of dropping
the message.

Production impact: Columba (built on LXMF-kt) and Sideband both default to
NOT hard-enforcing stamps on a delivery destination that merely advertises a
cost. A peer sending a message that carries no valid PoW stamp (e.g. because
its own outbound auto-stamp path didn't fire, or the sender is a non-stamping
implementation) must therefore still be delivered.

The LXMF-kt #38 divergence (LXMRouter.kt processInboundDelivery + lxmfDelivery):
the stamp check drops the message unconditionally when the stamp is invalid --
``enforceStampsFlag`` is set by ``enforceStamps()``/``ignoreStamps()`` but is
never consulted in the inbound decision, so the drop is unconditional. A
kotlin router silently drops every unstamped/invalid-stamp message that the
reference python router accepts.

This test pins the default. It uses the ``lxmf_inject_inbound`` bridge
command, which drives a crafted UNSTAMPED message through the production
``lxmf_delivery`` / ``lxmfDelivery`` path with the node's own delivery
destination's ``stamp_cost`` set and stamp enforcement NOT enabled. The
reference (python) must deliver the message to the inbox; any implementation
that drops it by default fails.

Runs per-impl (single bridge, no live peer): the contract is "matches the
reference accept-by-default", so the python row is the green control and a
divergent implementation (kt) fails its own row.
"""

import pytest


def test_invalid_stamp_inbound_accepted_by_default(impl, single_bridge):
    """A delivery destination that advertises a stamp cost but does not
    hard-enforce stamps must still accept an unstamped inbound message,
    matching the reference python default (enforce_stamps=False)."""
    bridge = single_bridge

    # Bring the router up. No inbound_stamp_cost here: we set the destination
    # stamp cost via the inject command so the stamp gate is engaged, but we do
    # NOT enable enforce_stamps - that is precisely the default under test.
    bridge.execute("lxmf_init", display_name=f"stamp-default-{impl}")

    # The inject command sets this node's own delivery destination's
    # stamp_cost (engaging the stamp gate) and feeds an unstamped message
    # through the production delivery path.
    result = bridge.execute(
        "lxmf_inject_inbound",
        stamp_cost=4,
        title="stamp-default",
        content="unstamped message that must be accepted by default",
    )

    # The reference python default (enforce_stamps=False) accepts the message:
    # lxmf_delivery returns True and it lands in the inbox. A divergence that
    # drops invalid-stamp messages by default returns False and the inbox is
    # empty.
    assert result.get("delivered") is True, (
        f"{impl} dropped an UNSTAMPED inbound message even though stamp "
        f"enforcement was NOT enabled. The reference python default "
        f"(enforce_stamps=False, LXMRouter.py:103) accepts such messages "
        f"(LXMRouter.py:1948-1951 'allowing anyway, since stamp enforcement "
        f"is disabled'). This is the inverted-default / dead-flag divergence: "
        f"inbound stamp validation must gate on the enforce_stamps flag, not "
        f"drop unconditionally. bridge result: {result!r}"
    )

    # The message must actually be present in the inbox (delivered=True means
    # the callback fired, but confirm the inbox records it).
    inbox = bridge.execute("lxmf_get_received_messages", since_seq=0)
    assert len(inbox.get("messages", [])) >= 1, (
        f"{impl} reported delivered=True but the inbox is empty after "
        f"injecting an unstamped message. bridge inject result: {result!r}"
    )

    bridge.execute("lxmf_shutdown")
