"""LXMessage progress field ticks during Resource transfer.

When a DIRECT message exceeds the LINK_PACKET_MAX_CONTENT (~319 B)
ceiling, LXMF falls back to RNS::Resource transfer — a multi-packet
streaming protocol where the underlying ``RNS.Resource`` calls a
``progress_callback`` repeatedly as parts of the payload are
transferred. Python LXMF wires this through to a public
``LXMessage.progress`` field, blending the resource's 0–1 progress
into the message-level 0.10–1.0 band:

    self.progress = 0.10 + (resource.get_progress() * 0.90)

This test pins the cross-impl-portable parts of that contract:

  - The bridge records ≥1 progress sample (proves the callback path
    is wired, even if the polling cadence misses intermediate values).
  - Sampled progress values are monotonically non-decreasing.
  - Once the message is ``delivered``, progress is 1.0.

The test uses a 50 KB payload — large enough to force fallback to
Resource transfer, small enough to keep the test bounded.

What deliberately ISN'T asserted: a strict mid-flight sample
``0 < s < 1``. python ``RNS.Resource`` only fires the sender-side
progress callback once per ``request`` call (Resource.py:1071,
post-loop), which on a fast loopback transfer is once for the whole
batch at ``resource.get_progress() = 0`` — leaving
``LXMessage.progress`` at the 0.10 link-up floor for most of the
transfer and jumping straight to 1.0 on delivery. Asserting strict
mid-flight samples would diverge from python's actual behavior and
flake on fast senders.
"""

import secrets
import time


def _drain_progress(server, message_hash, deadline):
    """Sample progress until the message reaches a terminal state or deadline.

    Returns ``(samples, final_state)`` where ``samples`` is a list of
    floats observed while the message was non-terminal. The final 1.0
    sample is appended explicitly after observing 'delivered' — this
    makes the monotonicity assertion well-defined on fast loopback.
    """
    samples = []
    while time.time() < deadline:
        progress = server.message_progress(message_hash)
        # Filter out -1.0 (no-progress sentinel) — that's "no
        # tick observed yet", not an actual sample.
        if progress >= 0.0:
            samples.append(progress)
        state = server.message_state(message_hash)
        if state in ("delivered", "failed"):
            break
        # 10 ms poll interval is fine-grained enough that any
        # impl which fires the progress callback more than once
        # during a 50 KB loopback transfer will produce multiple
        # samples here; impls that fire only once (e.g. python's
        # sender-side RNS.Resource) still satisfy the ≥1-sample
        # + monotonic + final==1.0 invariants asserted below.
        time.sleep(0.01)
    final_state = server.message_state(message_hash)
    final_progress = server.message_progress(message_hash)
    if final_progress >= 0.0:
        samples.append(final_progress)
    return samples, final_state


def test_resource_progress_ticks_during_transfer(server_impl, client_impl, pipe_pair):
    """Server -> client large direct message; server observes ≥1 progress sample, monotonic, final == 1.0."""
    server, client = pipe_pair

    # 50 KB random payload: large enough to force Resource transfer
    # (exceeds LINK_PACKET_MAX_CONTENT ~319 B), small enough to keep
    # the test bounded.
    content = "P" * 50000 + secrets.token_hex(16)
    title = f"progress-{secrets.token_hex(4)}"

    message_hash = server.send_direct(
        recipient_hash=client.delivery_hash,
        content=content,
        title=title,
    )
    assert message_hash, (
        f"server.send_direct ({server_impl}) returned empty "
        f"message_hash — sender did not finish packing the LXMessage."
    )

    # 30s ceiling — well above expected loopback transfer time but
    # generous enough to absorb CI scheduling jitter. The 10 ms poll
    # interval inside _drain_progress keeps sampling fine-grained
    # enough to catch intermediate ticks.
    deadline = time.time() + 30.0
    samples, final_state = _drain_progress(server, message_hash, deadline)

    assert final_state == "delivered", (
        f"server ({server_impl}) outbound state for large direct "
        f"message is {final_state!r}, expected 'delivered' within 30s. "
        f"Progress samples: {samples}"
    )

    assert len(samples) >= 1, (
        f"server ({server_impl}) recorded zero progress samples for "
        f"a 50 KB Resource transfer to ({client_impl}) — likely the "
        f"progress_callback is not wired. Final state: {final_state!r}"
    )

    # Note: there is no cross-impl mid-flight assertion here. python
    # LXMF's `RNS.Resource` fires `__progress_callback` exactly once
    # on the sender per `request` call (Resource.py:1071, post-loop),
    # which on a fast loopback transfer is once for the whole batch
    # at `resource.get_progress() = 0`, leaving `LXMessage.progress`
    # at the 0.10 "link-up" floor for most of the transfer and then
    # jumping to 1.0 on delivery. A test that required `0 < s < 1`
    # samples would diverge from python's actual behavior. The
    # assertions kept here — final state delivered, ≥1 sample,
    # monotonic, final == 1.0 — are what's actually portable across
    # impls.

    for i in range(1, len(samples)):
        assert samples[i] >= samples[i - 1] - 1e-6, (
            f"server ({server_impl}) progress went backwards: "
            f"{samples[i - 1]} -> {samples[i]}. Full sample tail: {samples}"
        )

    assert abs(samples[-1] - 1.0) < 1e-6, (
        f"server ({server_impl}) final progress for delivered message "
        f"is {samples[-1]}, expected 1.0. Sample tail: {samples}"
    )

    # Drain the inbox so the test fixture cleanup doesn't see a
    # stale unread message in client's queue.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if client.drain_received():
            break
        time.sleep(0.05)
