"""LXMF PROPAGATED delivery via a propagation node.

Three-bridge topology (`tcp_trio`):

    sender → propagation_node (python) → receiver

Sender submits a PROPAGATED LXMF; the message is uploaded to the PN
and stored there. Receiver later runs `lxmf_sync_inbound` to pull
queued messages from the PN. The receiver's regular delivery callback
fires for the synced message, so it lands in the inbox alongside any
opportunistic / direct messages.

Because swift LXMF doesn't host propagation nodes, the PN is always
python — sender and receiver vary. Two impls (python, swift) yields
4 trios.
"""

import secrets
import time

import pytest


# Override the 60s global pytest-timeout for propagation. Worst-case
# budget = ~15s tcp_trio fixture + 120s upload deadline + 1s settle +
# 3×30s sync_inbound retries + 2×2s noPath backoff + ~15s drain ≈ 245s.
# 360s leaves visible headroom against the worst case while still
# catching a real hang.
@pytest.mark.timeout(360)
def test_propagated_message_via_pn(sender_impl, receiver_impl, tcp_trio):
    sender, pn, receiver = tcp_trio

    # Pad the content to force RESOURCE representation on the wire
    # (rather than single-PACKET, which python LXMF picks for small
    # payloads that fit under LINK_PACKET_MAX_CONTENT ≈ 180-200 bytes
    # in `LXMessage.pack` at LXMessage.py:444). The dedicated
    # propagation invariant assertion below greps lxmd's per-resource
    # log line, which only fires for the RESOURCE code path
    # (`propagation_resource_concluded`); the PACKET path goes through
    # a different lxmd handler that doesn't surface the
    # identified-vs-anonymous remote distinction in the same way.
    # Forcing RESOURCE keeps the cross-impl test exercising the same
    # lxmd code path regardless of sender impl.
    padding = "x" * 512
    content = f"prop-{secrets.token_hex(8)}-{padding}"
    title = f"prop-title-{secrets.token_hex(4)}"

    message_hash = sender.send_propagated(
        recipient_hash=receiver.delivery_hash,
        content=content,
        title=title,
    )
    assert message_hash, (
        f"sender.send_propagated ({sender_impl}) returned empty "
        f"message_hash"
    )

    # ---- Sender uploads to the PN -------------------------------
    # The sender's outbound state needs to reach `sent` (uploaded to
    # the PN). A `delivered` state requires the recipient to also
    # sync, but PROPAGATED on the sender side is "done" once the PN
    # accepts the upload.
    #
    # Deadline was originally 30s, bumped to 60s, then 90s as the
    # kotlin sender's Resource transfer perf gap surfaced under
    # 2-core CI load. Bumped again to 120s: even with reticulum-kt#64
    # merged (lock-release, dedup, watchdog cleanup, identity-CAS),
    # the `kotlin->lxmd_pn->python` variant consistently exceeds 90s
    # on GitHub-hosted runners (1.5-2x slower than local 2-core).
    # `kotlin->lxmd_pn->kotlin` typically runs in ~25s on the same
    # CI; the asymmetry suggests the python receiver bridge competes
    # for CPU with the sender during the upload window in a way the
    # kotlin receiver bridge does not. Worth dedicated investigation
    # — but the underlying perf gap (kotlin Resource transfer is
    # 3-5x slower than python's equivalent) is tracked in
    # reticulum-kt#65 and this deadline is a band-aid until it lands.
    upload_deadline = time.time() + 120.0
    upload_state = None
    while time.time() < upload_deadline:
        upload_state = sender.message_state(message_hash)
        if upload_state in {"sent", "delivered"}:
            break
        time.sleep(0.2)
    assert upload_state in {"sent", "delivered"}, (
        f"sender ({sender_impl}) outbound state for propagated "
        f"message is {upload_state!r}, expected `sent` or `delivered` "
        f"within 120s — sender failed to upload to PN"
    )

    # ---- Conformance invariant: sender did NOT identify on the link
    # to the PN for the delivery transfer. Python LXMF's
    # `process_outbound` for PROPAGATED (LXMRouter.py:2700-2704)
    # establishes the link to the propagation node and goes straight
    # to sending the resource — `link.identify(...)` is reserved for
    # the *retrieval* path (LXMRouter.py:493). Identifying on the
    # delivery link causes lxmd's `propagation_resource_concluded`
    # (LXMRouter.py:2188-2214) to run its peer-discovery branch
    # (`RNS.Destination(remote_identity, OUT, SINGLE, "lxmf",
    # "propagation")` then `recall_app_data`), which serializes
    # against the proof emission and adds latency.
    #
    # We detect the invariant via lxmd's own stderr at LOG_VERBOSE
    # (LxmdPropagationNode default): the per-resource line is
    #
    #   "Received N message{s} from {remote_str}, validating stamps..."
    #
    # where `remote_str` is `"unknown client"` when the link wasn't
    # identified, or `RNS.prettyhexrep(remote_hash)` (a hex hash, with
    # an optional `peer ` prefix for known peers) when it was.
    #
    # An impl that erroneously identifies on the delivery link still
    # has its message uploaded successfully — the assertion above will
    # pass — but lxmd takes a slower path and the asymmetry is
    # observable in cross-impl conformance (kotlin->python ~2x slower
    # than kotlin->kotlin). This invariant catches the misbehavior
    # directly, regardless of the perf observable.
    # `pn.lxmd_proc` is the LxmdPropagationNode managing the lxmd
    # subprocess (set in conftest.py:519). Use `stdout_tail()` rather
    # than `stderr_tail()` — lxmd is spawned with `-v` (verbose console)
    # which routes RNS.log output to stdout. Stderr only catches
    # subprocess-level errors and python tracebacks.
    log_output = pn.lxmd_proc.stdout_tail(n_bytes=8000)

    # Find the per-resource log line(s) for this transfer. There may be
    # multiple resource transfers in flight if the test order overlaps;
    # filter to the lines emitted during this test by matching the
    # validating-stamps marker.
    received_lines = [
        ln for ln in log_output.splitlines()
        if "validating stamps" in ln and "Received" in ln and " from " in ln
    ]
    assert received_lines, (
        f"Could not find the lxmd 'Received N message... from <remote_str>' "
        f"log line in stdout. lxmd may not be running at LOG_VERBOSE — "
        f"check LxmdPropagationNode's `loglevel` default (must be >= 5) "
        f"and that the `-v` console flag is still set on the lxmd "
        f"command line in `_lxmd_pn.py`.\n"
        f"stdout tail (last 2000 chars):\n{log_output[-2000:]}"
    )
    # Take the last matching line — most recent transfer for this test.
    last_line = received_lines[-1]
    assert "from unknown client" in last_line, (
        f"sender ({sender_impl}) appears to have IDENTIFIED on the "
        f"delivery link to the PN. Python LXMF's PROPAGATED outbound "
        f"path establishes an unidentified link (LXMRouter.py:2700-"
        f"2704); identifying causes lxmd to run its peer-discovery "
        f"branch on every resource conclusion, which serializes "
        f"against proof emission and breaks cross-impl interop "
        f"performance. Fix: do NOT call `link.identify(...)` on the "
        f"propagation link when sending; identify is only for the "
        f"retrieval/sync path.\nlxmd log line: {last_line!r}"
    )

    # ---- Receiver syncs from PN --------------------------------
    # Allow a brief settle so the PN has the message persisted before
    # the receiver dials in for sync.
    time.sleep(1.0)
    # Retry sync with re-announce on noPath: in 3-bridge transport
    # topology the PN's announce can race the receiver's dial-in,
    # leaving the receiver with a missing path entry on the first
    # sync attempt. A re-announce by the PN nudges the table.
    sync_result = None
    for attempt in range(3):
        sync_result = receiver.sync_inbound(timeout_sec=30.0)
        assert sync_result is not None, "sync_inbound returned None"
        # Python bridge returns int final_state; swift returns string.
        # Either side reports trouble if it couldn't even establish the
        # link; bail on retry only for that early-failure case.
        is_no_path = (
            sync_result.get("error") == "noPath"
            or "noPath" in str(sync_result.get("final_state", ""))
        )
        if not is_no_path:
            break
        # Nudge the path table by re-announcing on all three sides
        # and waiting briefly before the next attempt.
        pn.bridge.execute("lxmf_announce")
        sender.bridge.execute("lxmf_announce")
        receiver.bridge.execute("lxmf_announce")
        time.sleep(2.0)

    # The python bridge returns numeric `final_state` (0=idle/done);
    # the swift bridge returns a string (`complete`, `idle`, `done`).
    # Either path is acceptable as long as the message lands.
    deadline = time.time() + 15.0
    received = []
    while time.time() < deadline:
        received += receiver.drain_received()
        if len(received) >= 1:
            time.sleep(0.5)
            received += receiver.drain_received()
            break
        time.sleep(0.2)

    assert len(received) == 1, (
        f"receiver ({receiver_impl}) received {len(received)} messages "
        f"after sync, expected exactly 1. sync_result={sync_result!r}"
    )

    msg = received[0]
    assert msg["content"] == content, (
        f"content mismatch: got {msg['content']!r}, expected {content!r}"
    )
    assert msg["title"] == title, (
        f"title mismatch: got {msg['title']!r}, expected {title!r}"
    )
    assert msg["method"] == "propagated", (
        f"method mismatch: got {msg['method']!r}, expected 'propagated'"
    )
    assert msg["source_hash"] == sender.delivery_hash.hex(), (
        f"source_hash mismatch: got {msg['source_hash']!r}, "
        f"expected {sender.delivery_hash.hex()!r}"
    )
    assert msg["destination_hash"] == receiver.delivery_hash.hex(), (
        f"destination_hash mismatch: got {msg['destination_hash']!r}"
    )
