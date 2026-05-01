"""Inbound message deduplication conformance tests.

Closes coverage gap from lxmf-conformance#12.

# Protocol invariant under test

`LXMRouter` deduplicates inbound messages by `message_hash`. Two
arrivals of the SAME `message_hash` MUST produce exactly one inbox
entry — the second arrival is dropped via the `locally_delivered_
transient_ids` / `locally_processed_transient_ids` check (python
`LXMRouter.py:1781`: `if not allow_duplicate and self.has_message
(message.hash): return`).

A receiver that doesn't dedup is vulnerable to:
- User-visible spam (same message appears N times in inbox)
- Stamp replay (combined with stamp-replay vector — see #11)
- False-confirmation attacks (recipient acts on duplicated delivery)

# Why this test needs forced timestamps

Two consecutive `lxmf_send_opportunistic` calls with default behavior
produce DIFFERENT `message_hash` values because each pack() picks a
fresh `time.time()` for the timestamp slot. The receiver wouldn't see
two arrivals of the same hash — it would see two distinct messages
and (correctly) deliver both.

To exercise dedup, the test pins the timestamp on both sends so the
wire bytes (and hence the SHA256 over them) are byte-identical. The
LXMessage encoder is deterministic given identical inputs (Ed25519
signatures are deterministic per RFC 8032), so two sends with pinned
timestamp produce the same `message_hash`. The receiver MUST see the
second arrival as a duplicate.

# Sender-side caveat

Some impls might dedup on the SENDER side too (don't actually
transmit a second message with a hash already in the outbound
queue). If that happens, this test surfaces it as
``second send did not transmit`` — which is itself a finding worth
filing, but distinct from a receiver-side dedup bug.
"""

import secrets
import time

import pytest


# Pin a deterministic timestamp so both sends produce identical wire
# bytes. Value is arbitrary as long as it's the same across the two
# calls. Using a far-from-now value (Nov 2023) so any clock-skew
# logic in either impl handles it the same way each time.
PINNED_TIMESTAMP = 1700000000.0


def test_duplicate_message_hash_arrives_once_in_inbox(server_impl, client_impl, pipe_pair):
    """Receiver MUST deliver only one inbox entry when the same wire bytes
    arrive twice.

    Sends an opportunistic message with a pinned timestamp, waits for
    delivery, sends again with the SAME pinned timestamp + content +
    title (producing byte-identical wire bytes / same message_hash),
    asserts the receiver's inbox still has exactly ONE entry.
    """
    server, client = pipe_pair

    # Random tail so a stale receiver buffer from a previous test can't
    # accidentally match. Stay under ENCRYPTED_PACKET_MAX_CONTENT.
    tail = secrets.token_hex(8)
    content = f"dedup-{tail}"
    title = f"dedup-title-{tail[:4]}"

    # ---- First send ---------------------------------------------------
    first_hash = server.send_opportunistic(
        recipient_hash=client.delivery_hash,
        content=content,
        title=title,
        timestamp=PINNED_TIMESTAMP,
    )
    assert first_hash, (
        f"server.send_opportunistic ({server_impl}) returned empty hash on first send."
    )

    # Wait for the first arrival to land — we want the dedup state to be
    # populated before the second send arrives. Without this barrier,
    # both arrivals could be in-flight simultaneously and the receiver's
    # behavior depends on which one is processed first (a race we don't
    # want to entangle with the dedup invariant under test).
    deadline = time.time() + 15.0
    received = []
    while time.time() < deadline:
        received += client.drain_received()
        if any(m["message_hash"] == first_hash.hex() for m in received):
            break
        time.sleep(0.2)

    assert any(m["message_hash"] == first_hash.hex() for m in received), (
        f"First send never arrived at client ({client_impl}) within 15s. "
        f"Inbox: {received!r}. Cannot test dedup without a confirmed first arrival."
    )

    # ---- Second send with identical inputs ----------------------------
    second_hash = server.send_opportunistic(
        recipient_hash=client.delivery_hash,
        content=content,
        title=title,
        timestamp=PINNED_TIMESTAMP,
    )
    assert second_hash == first_hash, (
        f"Sanity check failed: two sends with identical inputs produced different hashes:\n"
        f"  first  ({server_impl}): {first_hash.hex()}\n"
        f"  second ({server_impl}): {second_hash.hex()}\n"
        f"This means LXMessage.pack() is not deterministic given pinned inputs — "
        f"sender impl is mixing in non-deterministic state somewhere (random nonce? "
        f"non-RFC-8032 Ed25519?). The dedup test below is not meaningful until the "
        f"sender is deterministic."
    )

    # ---- Wait and verify NO duplicate ---------------------------------
    # Linger >= one poll cycle so a delayed duplicate has a chance to
    # land. Per the tight-assertions discipline (memory note
    # `feedback-tight-test-assertions`), we need to actively look for
    # the bug — short timeouts mask real duplicates.
    time.sleep(2.0)
    final = list(received)  # snapshot what we have so far
    final += client.drain_received()
    matching = [m for m in final if m["message_hash"] == first_hash.hex()]

    assert len(matching) == 1, (
        f"Receiver ({client_impl}) delivered {len(matching)} inbox entries for the "
        f"same message_hash {first_hash.hex()[:16]}. Expected EXACTLY 1.\n"
        f"This means the impl's LXMRouter does NOT deduplicate inbound messages — "
        f"the second arrival of the same wire bytes was delivered as a separate "
        f"message. Per LXMRouter.py:1781 the receiver must consult "
        f"`locally_delivered_transient_ids` and drop duplicates.\n"
        f"All matching inbox entries: {matching!r}"
    )


def test_pinned_timestamp_produces_deterministic_message_hash(server_impl, client_impl, pipe_pair):
    """Sanity: confirm the test infrastructure (forced timestamp + identical
    inputs → identical hash) actually works on the sender impl.

    A separate test from the dedup one so that if the sender's pack() is
    non-deterministic, this test fails first with a clear "your impl's
    pack is non-deterministic" message — rather than the dedup test
    failing for the wrong reason.
    """
    server, _ = pipe_pair

    tail = secrets.token_hex(8)
    content = f"determinism-{tail}"
    title = f"determinism-title-{tail[:4]}"

    h1 = server.send_opportunistic(
        recipient_hash=server.delivery_hash,  # send to self — we don't care about delivery here
        content=content, title=title, timestamp=PINNED_TIMESTAMP,
    )
    h2 = server.send_opportunistic(
        recipient_hash=server.delivery_hash,
        content=content, title=title, timestamp=PINNED_TIMESTAMP,
    )
    assert h1 == h2, (
        f"Sender ({server_impl}) pack() is not deterministic given identical inputs:\n"
        f"  first  call: {h1.hex()}\n"
        f"  second call: {h2.hex()}\n"
        f"For LXMessage to round-trip across impls, pack() must be deterministic. "
        f"Most likely cause: the impl is signing with non-deterministic Ed25519 "
        f"(should be RFC 8032 deterministic), OR mixing entropy into the payload "
        f"somewhere besides the timestamp slot."
    )
