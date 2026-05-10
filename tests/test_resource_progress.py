"""LXMessage progress field ticks during Resource transfer.

When a DIRECT message exceeds the LINK_PACKET_MAX_CONTENT (~319 B)
ceiling, LXMF falls back to RNS::Resource transfer — a multi-packet
streaming protocol where the underlying ``RNS.Resource`` calls a
``progress_callback`` repeatedly as parts of the payload are
transferred. Python LXMF wires this through to a public
``LXMessage.progress`` field, blending the resource's 0–1 progress
into the message-level 0.10–1.0 band:

    self.progress = 0.10 + (resource.get_progress() * 0.90)

This test pins that contract for both directions of the
(python, microlxmf) matrix:

  - During an in-flight large transfer, ``message_progress`` returns
    a value > 0 and ≤ 1 at some point before the message is
    ``delivered``. (A single sample is enough to prove the wiring
    fires; loopback transfers are fast enough that we may not see
    every intermediate tick.)
  - Sampled progress values are monotonically non-decreasing.
  - Once the message is ``delivered``, progress is 1.0.

The test uses a 50 KB payload — large enough that the transfer takes
multiple ticks on loopback (each Resource part is HMAC'd separately),
but small enough that the test stays under the global pytest
timeout. The exact number of intermediate ticks observed is timing-
dependent, so we only require ≥1.
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
        # 10 ms poll interval gives ~30+ samples across a 50 KB
        # loopback Resource transfer (typical ~300 ms on CI),
        # which is enough margin to land at least one mid-flight
        # observation before delivery flips state to terminal.
        time.sleep(0.01)
    final_state = server.message_state(message_hash)
    final_progress = server.message_progress(message_hash)
    if final_progress >= 0.0:
        samples.append(final_progress)
    return samples, final_state


def test_resource_progress_ticks_during_transfer(server_impl, client_impl, pipe_pair):
    """Server -> client large direct message; server observes progress >0 and =1.0."""
    server, client = pipe_pair

    # 50 KB random payload: enough resource-parts to make at least
    # one mid-flight progress observation likely on loopback.
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

    # Prove the resource progress_callback fires multiple times during
    # transfer. Polling the message_progress value is racy — on fast
    # loopback (e.g. the C++ microLXMF bridge transferring 50 KB in
    # <10 ms) the worker thread can complete every part-send before any
    # polling iteration observes an intermediate value, so `samples`
    # collapses to [1.0, 1.0]. The bridge-side tick counter records
    # every callback firing deterministically, regardless of polling.
    tick_count = server.message_progress_tick_count(message_hash)
    assert tick_count >= 2, (
        f"server ({server_impl}) progress callback fired {tick_count} "
        f"time(s) during a 50 KB Resource transfer to ({client_impl}); "
        f"expected >= 2 (per-part firings). A count <= 1 means either "
        f"the callback is wired only at the terminal-resource event, "
        f"or it is not wired at all. Samples observed by polling: "
        f"{samples}"
    )

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
