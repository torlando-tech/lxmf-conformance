"""Peering-stamp conformance tests (cross-implementation).

Covers lxmf-conformance#18: propagation-node **peering stamps** — the
low-cost proof-of-work keys peers exchange during the PN sync handshake
(python ``LXMPeer.py::generate_peering_key`` /
``LXMRouter.py`` offer-request validation via
``LXStamper.validate_peering_key``).

Why byte-level commands instead of driving the live sync flow: the
peering stamp is validated inside the PN's ``sync_offer_request``
handler, which fires mid-handshake on a real link with a seeded peer
table and matching costs on both sides. Eliciting a *rejection* through
that flow means deliberately desyncing two live routers' cost tables —
slow, flaky, and it conflates handshake plumbing with the property
under test. The stamp itself is pure cryptography over identity
material; exercising the production generate/validate entry points
directly isolates exactly what #18 asks: does each impl generate stamps
the OTHER impl accepts, and does each impl reject stamps that should
not validate?

What is asserted (mirrors the #18 recipe):

1. Generate for identity A → validate as A ⇒ accepted (positive control).
2. Generate for A → validate as B's material ⇒ rejected.
3. Generate for B → validate as A's material ⇒ rejected (reverse).
4. Cross-cost: stamp valued V → validate demanding >V ⇒ rejected.

Cases 1–4 run for every generator×validator impl pair in the
parametrized matrix, so kotlin-generated stamps must satisfy python
validation and vice versa — the actual interop property. The bridge
commands call the PRODUCTION paths (python ``LXStamper.generate_stamp``
with ``expand_rounds=WORKBLOCK_EXPAND_ROUNDS_PEERING``, kotlin
``LXStamper.generateStampWithWorkblock`` +
``LXStamper.validatePeeringKey``), not re-implementations.

Key-material note: python builds generation material as
``peer_identity_hash + node_identity_hash`` (LXMPeer.py:259) but the
node-side validation id as ``node_hash + remote_hash``
(LXMRouter.py:2300). Tests therefore pass explicit byte material to
both sides, pinning the real protocol contract (same material in,
same accept/reject out) rather than either port's internal variable
names. Cost 12 keeps PoW generation well under a second per stamp
(same rationale as the inbound_stamp_cost fixture); the threshold math
is scale-free, so a low test cost proves the same inequality the
production cost-18 path enforces.

Bridge commands added for this suite (see README):
  - ``lxmf_generate_peering_stamp``
  - ``lxmf_validate_peering_stamp``
"""

import os
import sys

import pytest

# Same RNS/LXMF checkout the bridges use, so reference-side helpers
# hash material identically to the reference bridge process.
_RNS_PATH = os.environ.get(
    "PYTHON_RNS_PATH", os.path.expanduser("~/repos/Reticulum")
)
if _RNS_PATH and _RNS_PATH not in sys.path:
    sys.path.insert(0, os.path.abspath(_RNS_PATH))

import RNS  # noqa: E402

# Generation cost for every case. Low cost = fast PoW; the rejection
# logic under test doesn't depend on absolute cost magnitude.
GEN_COST = 12

# Two distinct synthetic identity hashes. Real protocol material is a
# 32-byte RNS identity hash (full hash of the public key) — derive the
# same shape here so both impls treat the bytes identically.
A_SEED = bytes.fromhex("aabbccddeeff00112233445566778899")
B_SEED = bytes.fromhex("11223344556677889900aabbccddeeff")


def _material(seed_16: bytes) -> bytes:
    return RNS.Identity.full_hash(seed_16)


def _material_a() -> bytes:
    return _material(A_SEED)


def _material_b() -> bytes:
    return _material(B_SEED)


def _generate(bridge, key_material: bytes, cost: int = GEN_COST) -> dict:
    """Generate a peering stamp; hard-fail on bridge-level errors."""
    resp = bridge.execute(
        "lxmf_generate_peering_stamp",
        key_material_hex=key_material.hex(),
        cost=cost,
    )
    assert not resp.get("error"), f"generate failed: {resp}"
    assert resp.get("stamp_hex"), f"generate returned no stamp: {resp}"
    return resp


def _validate(bridge, peering_id: bytes, stamp: bytes, cost: int) -> bool:
    """Validate a peering stamp; returns the impl's verdict."""
    resp = bridge.execute(
        "lxmf_validate_peering_stamp",
        peering_id_hex=peering_id.hex(),
        stamp_hex=stamp.hex(),
        cost=cost,
    )
    assert not resp.get("error"), f"validate failed: {resp}"
    return bool(resp["valid"])


class TestPeeringStampConformance:
    """lxmf-conformance#18 — peering stamp cross-impl conformance."""

    def test_self_validation_accepts(
        self, generator_impl, validator_impl, gen_bridge, val_bridge
    ):
        """Generate for material A, validate against A: ACCEPTED.

        Positive control for every generator→validator pair: if this
        fails, the impls disagree on workblock derivation or value math
        (the StampInteropTest parity bug class) and every negative case
        below is meaningless.
        """
        mat_a = _material_a()
        result = _generate(gen_bridge, mat_a)
        stamp = bytes.fromhex(result["stamp_hex"])

        # Sanity on the generator's own validation first.
        assert _validate(gen_bridge, mat_a, stamp, GEN_COST) is True, (
            f"[{generator_impl}] generated stamp fails its own validation"
        )

        # THE interop assertion: validator accepts the foreign stamp.
        assert _validate(val_bridge, mat_a, stamp, GEN_COST) is True, (
            f"[{generator_impl} -> {validator_impl}] valid peering stamp "
            f"REJECTED by validator — impls disagree on PoW math"
        )

    def test_identity_mismatch_rejected(
        self, generator_impl, validator_impl, gen_bridge, val_bridge
    ):
        """Generate for A, validate as B: REJECTED (#18 step 2).

        An impl accepting another identity's stamp lets a peer
        impersonate anyone against this PN without doing its own PoW.
        """
        mat_a = _material_a()
        mat_b = _material_b()

        stamp_a = bytes.fromhex(_generate(gen_bridge, mat_a)["stamp_hex"])

        # A's stamp must fail against B's material on BOTH impls.
        assert _validate(gen_bridge, mat_b, stamp_a, GEN_COST) is False, (
            f"[{generator_impl}] accepted a stamp generated for DIFFERENT "
            f"identity material — impersonation possible"
        )
        assert _validate(val_bridge, mat_b, stamp_a, GEN_COST) is False, (
            f"[{generator_impl} -> {validator_impl}] validator accepted a "
            f"stamp generated for DIFFERENT identity material"
        )

    def test_identity_mismatch_reverse_direction(
        self, generator_impl, validator_impl, gen_bridge, val_bridge
    ):
        """Generate for B, validate as A: REJECTED (#18 step 4).

        Mirror of the forward case — catches asymmetric bugs like an
        impl hashing only one side of the material (e.g. folding the
        node hash in but ignoring the peer hash would pass forward and
        fail here).
        """
        mat_a = _material_a()
        mat_b = _material_b()

        stamp_b = bytes.fromhex(_generate(gen_bridge, mat_b)["stamp_hex"])

        assert _validate(gen_bridge, mat_a, stamp_b, GEN_COST) is False, (
            f"[{generator_impl}] accepted B-generated stamp validated as A"
        )
        assert _validate(val_bridge, mat_a, stamp_b, GEN_COST) is False, (
            f"[{generator_impl} -> {validator_impl}] validator accepted "
            f"B-generated stamp validated as A (reverse direction)"
        )

    def test_cross_cost_insufficient_value_rejected(
        self, generator_impl, validator_impl, gen_bridge, val_bridge
    ):
        """Stamp below the demanded cost: REJECTED (#18 step 3).

        The validator must compare the stamp VALUE against the TARGET
        cost, not merely accept any structurally-valid stamp. Pins the
        wrong-cost-threshold bug class (#18: "lets peers bypass cost
        gates set at the PN").

        Determinism note: PoW generation overshoots — a search that
        finds a value-18 stamp while hunting for cost 12 keeps it. So a
        fixed "+6" demand would flake whenever the achieved value meets
        it (~1/64 per +1, observed live: kotlin overshot 12→18). The
        honest invariant is therefore relative: whatever value the
        generator achieved (reported by the PRODUCTION path), demanding
        even one more bit must fail on both impls.
        """
        mat_a = _material_a()

        result = _generate(gen_bridge, mat_a, GEN_COST)
        stamp = bytes.fromhex(result["stamp_hex"])
        achieved = int(result.get("value", GEN_COST))

        # Sanity: the stamp meets its own target.
        assert _validate(val_bridge, mat_a, stamp, GEN_COST) is True

        assert _validate(val_bridge, mat_a, stamp, achieved + 1) is False, (
            f"[{generator_impl} -> {validator_impl}] validator accepted a "
            f"value-{achieved} stamp against a cost-{achieved + 1} target — "
            f"cost gate bypassable"
        )
