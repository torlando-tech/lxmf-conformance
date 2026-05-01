#!/usr/bin/env python3
"""Python reference bridge for the LXMF conformance suite.

Long-running subprocess that reads JSON-RPC commands from stdin and
writes JSON responses to stdout. Each request is one line; each
response is one line. The bridge prints ``READY`` once on stdout
before entering the command loop so the test harness can wait for
startup deterministically.

For the bridge protocol shape see ``bridge_client.py``. For the
command surface see the README and the ``COMMANDS`` table at the
bottom of this file.

Phase 1 commands (all bridges must implement):

  - ``lxmf_init``: spin up RNS + LXMF router, return identity hash
  - ``lxmf_add_tcp_server_interface``: attach a TCP server interface,
     return the bound port
  - ``lxmf_add_tcp_client_interface``: attach a TCP client interface
     pointing at a peer bridge's listener
  - ``lxmf_announce``: emit a delivery announce
  - ``lxmf_send_opportunistic``: send an opportunistic LXMF message,
     return its hash
  - ``lxmf_get_received_messages``: drain inbound queue
  - ``lxmf_get_message_state``: poll outbound state
  - ``lxmf_shutdown``: clean teardown

The bridge is single-instance per process: ``lxmf_init`` may be called
once. To run two LXMF nodes, the test harness spawns two bridges and
wires them with TCP loopback (one side hosts a listener, the other
connects to it). This matches what ``reticulum-conformance`` does for
its wire-layer trios — battle-tested and avoids the subtle FD-
inheritance gotchas of OS-pipe-based topologies.

Phase 2 will add a real PipeInterface backend (FD pair across bridges,
HDLC framing). Pipes are conceptually cleaner but bring fork/spawn
quirks across macOS/Linux that aren't worth fighting in Phase 1.
"""

import json
import os
import sys
import threading
import time
import traceback

# Allow CI / local dev to point at checked-out source trees rather than
# pip-installed packages. Mirrors reticulum-conformance/reference/
# bridge_server.py — same env var names so contributors don't have to
# learn two conventions. Falls back to whatever's importable from the
# ambient Python (so a `pip install lxmf` setup also works).
_RNS_PATH = os.environ.get("PYTHON_RNS_PATH")
_LXMF_PATH = os.environ.get("PYTHON_LXMF_PATH")
if _RNS_PATH:
    sys.path.insert(0, os.path.abspath(_RNS_PATH))
if _LXMF_PATH:
    sys.path.insert(0, os.path.abspath(_LXMF_PATH))

import RNS  # noqa: E402  -- after path setup
import LXMF  # noqa: E402

# --------------------------------------------------------------------------- #
# Stamp generation: force single-process path.
#
# LXStamper.generate_stamp() dispatches by platform and on Linux uses
# multiprocessing.Process workers (job_linux). In a bridge subprocess
# launched by pytest those workers hang indefinitely — they inherit FDs
# and signal masks that confuse the manager, and the bridge never sees
# the stamp result. We force the single-process path (LXStamper.py:145
# "should work on any platform, used as a fall-back, in case of limited
# multi-processing"). Cost-12 stamps take < 1s, well under any test
# timeout, so we don't need parallelism.
#
# Two implementations of the override depending on which LXMF is loaded:
#
#   1. PREFERRED — call LXStamper.set_external_generator(callback) if it
#      exists. This is the upstream-blessed registration hook (PR
#      markqvist/LXMF#38, on torlando-tech/LXMF feature branch as of
#      2025-12). Same hook Columba uses on Android via Chaquopy.
#
#   2. FALLBACK — monkey-patch LXStamper.generate_stamp directly. Works
#      against an unmodified upstream LXMF that hasn't merged #38 yet.
#
# Either way the bridge ends up running cost-12 stamps single-process
# in <1s and propagation tests are unblocked.
# --------------------------------------------------------------------------- #
import LXMF.LXStamper as _LXStamper  # noqa: E402


def _external_stamp_generator(workblock, stamp_cost):
    """Single-process stamp generator matching LXMF#38's callback shape:
    (workblock: bytes, stamp_cost: int) -> (stamp: bytes, rounds: int).
    """
    start_time = time.time()
    # job_simple wants a message_id key for active_jobs bookkeeping; we
    # don't have one here so synthesize a stable per-call id.
    pseudo_msg_id = RNS.Identity.full_hash(workblock + stamp_cost.to_bytes(4, "big"))
    stamp, rounds = _LXStamper.job_simple(stamp_cost, workblock, pseudo_msg_id)
    duration = time.time() - start_time
    RNS.log(
        f"[bridge] Single-process stamp generated in "
        f"{RNS.prettytime(duration)}, {rounds} rounds",
        RNS.LOG_DEBUG,
    )
    return stamp, rounds


def _generate_stamp_single_process(message_id, stamp_cost,
                                   expand_rounds=_LXStamper.WORKBLOCK_EXPAND_ROUNDS):
    """Monkey-patch fallback for LXMF without set_external_generator."""
    workblock = _LXStamper.stamp_workblock(message_id, expand_rounds=expand_rounds)
    start_time = time.time()
    stamp, rounds = _LXStamper.job_simple(stamp_cost, workblock, message_id)
    duration = time.time() - start_time
    value = _LXStamper.stamp_value(workblock, stamp) if stamp is not None else 0
    RNS.log(
        f"[bridge] Single-process stamp value={value} in "
        f"{RNS.prettytime(duration)}, {rounds} rounds",
        RNS.LOG_DEBUG,
    )
    return stamp, value


if hasattr(_LXStamper, "set_external_generator"):
    _LXStamper.set_external_generator(_external_stamp_generator)
else:
    _LXStamper.generate_stamp = _generate_stamp_single_process

# --------------------------------------------------------------------------- #
# Bridge state
# --------------------------------------------------------------------------- #


class _BridgeState:
    """Per-process LXMF bridge state.

    A single bridge process hosts at most one RNS + LXMF router pair —
    same constraint reticulum-conformance's wire bridge enforces. Tests
    that need two LXMF nodes spawn two bridges.
    """

    def __init__(self):
        self.reticulum: RNS.Reticulum | None = None
        self.config_dir: str | None = None
        self.identity: RNS.Identity | None = None
        self.router: LXMF.LXMRouter | None = None
        self.delivery_destination = None
        self.storage_path: str | None = None

        # Inbound message queue. Append-only; ``cmd_lxmf_get_received_
        # messages`` returns entries with ``seq > since_seq`` so a slow
        # consumer doesn't lose messages. Tight assertions in tests
        # require monotonic delivery ordering.
        self._inbox: list[dict] = []
        self._inbox_lock = threading.Lock()
        self._inbox_seq = 0

        # Outbound state tracker. Maps message hash -> state name.
        # Updated synchronously from the LXMessage delivery callback
        # (passed when constructing each LXMessage). Tests poll
        # ``cmd_lxmf_get_message_state`` for delivery confirmation.
        self._outbound_state: dict[bytes, str] = {}
        self._outbound_lock = threading.Lock()

        # Live LXMessage references keyed by message hash. Used by
        # ``cmd_lxmf_get_message_state`` to read the current
        # ``message.state`` directly. Necessary because LXMF's
        # ``register_delivery_callback`` only fires on terminal-for-
        # method transitions (DELIVERED for opportunistic, SENT for
        # propagated). The intermediate transitions (OUTBOUND→SENDING,
        # SENDING→SENT-not-yet-delivered) never fire any callback, so
        # polling _outbound_state alone reports a stale 'outbound'
        # even after the message has actually advanced.
        self._outbound_messages: dict[bytes, "LXMF.LXMessage"] = {}

        self._interfaces: list[FdPipeInterface] = []


_state = _BridgeState()


# --------------------------------------------------------------------------- #
# LXMF field encoding / decoding
# --------------------------------------------------------------------------- #
#
# LXMF `fields` is a `dict[int, Any]` where leaf values can be bytes,
# strings, ints, bools, or nested lists/dicts. JSON-RPC can't express
# bytes directly, so the bridge wire format uses tagged objects:
#   - {"bytes": "<hex>"}
#   - {"str": "..."}
#   - {"int": 123}
#   - {"bool": true}
#   - JSON arrays for lists (recursive)
#   - bare JSON primitives pass through
#
# The explicit "bytes" tag is load-bearing for FIELD_FILE_ATTACHMENTS:
# attachment elements are `[filename_str, data_bytes]` and without a
# tag there's no way to distinguish hex-encoded bytes from a literal
# string. Mirrors reticulum-conformance's `lxmf_bridge.py` so the two
# suites encode attachments identically.


def _encode_field_value_for_inbox(value):
    """Recursively convert bytes -> hex so the value is JSON-safe."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_encode_field_value_for_inbox(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode_field_value_for_inbox(v) for k, v in value.items()}
    return value


def _decode_field_value_from_params(value):
    """Inverse of `_encode_field_value_for_inbox` for the send side."""
    if isinstance(value, dict):
        if "bytes" in value:
            return bytes.fromhex(value["bytes"])
        if "str" in value:
            return value["str"]
        if "int" in value:
            return int(value["int"])
        if "bool" in value:
            return bool(value["bool"])
        raise ValueError(
            f"Unsupported LXMF field object shape; expected one of "
            f"{{bytes|str|int|bool}}: {value!r}"
        )
    if isinstance(value, list):
        return [_decode_field_value_from_params(v) for v in value]
    return value


def _decode_fields_param(fields_param):
    """Convert the JSON `fields` dict (str keys -> tagged values) into
    the `dict[int, Any]` shape LXMessage expects."""
    if not fields_param:
        return {}
    decoded = {}
    for k, v in fields_param.items():
        decoded[int(k)] = _decode_field_value_from_params(v)
    return decoded


def _encode_message_fields(message):
    """Pull `message.fields` off an inbound LXMessage and JSON-ify."""
    fields = {}
    for k, v in (getattr(message, "fields", None) or {}).items():
        fields[str(k)] = _encode_field_value_for_inbox(v)
    return fields


# --------------------------------------------------------------------------- #
# LXMF bridge commands
# --------------------------------------------------------------------------- #


def cmd_lxmf_init(params):
    """Bring up RNS + an LXMF router.

    Single-fire per bridge process; calling twice raises. The bridge
    creates a temp configdir + storagedir for full isolation between
    test runs.

    params:
        storage_path (str, optional): explicit storage path for LXMF.
            Defaults to a fresh tempdir. Tests don't normally set this;
            the parameter is exposed for cross-process resume scenarios
            in Phase 2.
        identity_pem (str, optional): hex-encoded RNS Identity private
            key bytes. Phase 1 leaves this unused — every test creates
            fresh identities. Reserved so future replay tests can pin
            an identity for deterministic announce hashes.
        display_name (str, optional): announced display name. Defaults
            to a generic label so a missing param doesn't cause Python
            LXMF to emit None into the announce app_data (LXMF rejects
            None display names with a TypeError on pack).

    Returns:
        identity_hash (hex): the LXMF router's identity hash. This is
            NOT the delivery destination hash — call ``lxmf_announce``
            (or read the announce return value) to get the destination
            hash a peer will address.
        delivery_destination_hash (hex): LXMF delivery destination
            hash. Other peers send opportunistic / direct messages to
            this hash.
        config_dir (str): the temp config dir, exposed so the test
            harness can clean it up if anything escapes the bridge's
            own teardown.
    """
    if _state.router is not None:
        raise RuntimeError(
            "lxmf_init has already been called on this bridge process. "
            "Spawn a separate bridge subprocess for each LXMF node."
        )

    import tempfile

    storage_path = params.get("storage_path") or tempfile.mkdtemp(
        prefix="lxmf_conf_storage_"
    )
    config_dir = tempfile.mkdtemp(prefix="lxmf_conf_rns_")
    display_name = params.get("display_name") or "lxmf-conformance-peer"

    # Write a minimal Reticulum config BEFORE constructing RNS.Reticulum.
    # The defaults RNS auto-generates set ``share_instance = Yes`` which
    # binds a LocalServerInterface on port 37428 — running two bridges
    # in the same test would collide on that port. We disable
    # share_instance unconditionally and only flip enable_transport on
    # for propagation-node bridges so the PN can forward announces
    # between sender + receiver in a 3-bridge topology (without that,
    # the sender's `RNS.Identity.recall(receiver_dest)` returns None
    # and PROPAGATED send fails).
    config_file = os.path.join(config_dir, "config")
    enable_transport = (
        "Yes" if params.get("enable_propagation_node") else "No"
    )
    with open(config_file, "w") as f:
        f.write(
            "[reticulum]\n"
            f"  enable_transport = {enable_transport}\n"
            "  share_instance = No\n"
            "  respond_to_probes = No\n"
            "\n"
            "[interfaces]\n"
        )

    # loglevel=3 (Notice) suppresses the chatty RNS info logs while
    # keeping warnings + errors visible on stderr; tests can override
    # with LXMF_CONFORMANCE_RNS_LOGLEVEL when debugging.
    rns_loglevel = int(os.environ.get("LXMF_CONFORMANCE_RNS_LOGLEVEL", "3"))

    reticulum = RNS.Reticulum(configdir=config_dir, loglevel=rns_loglevel)

    # LXMF router uses a dedicated identity (matches Python LXMF
    # production usage and the reticulum-conformance reference bridge).
    # storage_path is the LXMF-internal path for delivery destinations,
    # ratchets, peer state, etc.
    identity = RNS.Identity()
    router = LXMF.LXMRouter(identity=identity, storagepath=storage_path)

    delivery_destination = router.register_delivery_identity(
        identity, display_name=display_name
    )

    # Hook the delivery callback so inbound messages land in our queue.
    # The hash field on the inbound LXMessage IS the message hash; we
    # surface it as ``message_hash`` for tight equality checks against
    # the sender's returned hash.
    def delivery_callback(message):
        # Method comes through as the LXDeliveryMethod int constant
        # (LXMF.LXMessage.OPPORTUNISTIC = 0, DIRECT = 1, PROPAGATED = 2).
        # We map to a stable string for cross-impl comparison — the
        # Swift bridge emits the same strings.
        method_name = _method_to_string(getattr(message, "method", None))
        ack_status = _ack_status_to_string(getattr(message, "delivery_attempts", None), method_name)
        with _state._inbox_lock:
            _state._inbox_seq += 1
            entry = {
                "seq": _state._inbox_seq,
                "message_hash": message.hash.hex() if getattr(message, "hash", None) else "",
                "source_hash": (
                    message.source_hash.hex()
                    if getattr(message, "source_hash", None)
                    else ""
                ),
                "destination_hash": (
                    message.destination_hash.hex()
                    if getattr(message, "destination_hash", None)
                    else ""
                ),
                "title": _bytes_to_str(getattr(message, "title", None)),
                "content": _bytes_to_str(getattr(message, "content", None)),
                "method": method_name,
                "ack_status": ack_status,
                "received_at_ms": int(time.time() * 1000),
                "fields": _encode_message_fields(message),
            }
            _state._inbox.append(entry)

    router.register_delivery_callback(delivery_callback)

    # Optional: bring this LXMF router up as a propagation node. Tests
    # that want a 3-bridge sender→PN→receiver topology spawn the middle
    # bridge with `enable_propagation_node=True`; the router announces
    # `lxmf:propagation` so the other bridges can discover it via path
    # convergence and target it with `lxmf_set_outbound_propagation_node`.
    propagation_destination_hash_hex = None
    if params.get("enable_propagation_node"):
        router.enable_propagation()
        # `enable_propagation` already triggers an initial announce of
        # the propagation destination, so a subsequent
        # `lxmf_announce` is unnecessary on the PN side.
        propagation_destination_hash_hex = router.propagation_destination.hash.hex()

    _state.reticulum = reticulum
    _state.config_dir = config_dir
    _state.identity = identity
    _state.router = router
    _state.delivery_destination = delivery_destination
    _state.storage_path = storage_path

    result = {
        "identity_hash": identity.hash.hex(),
        "delivery_destination_hash": delivery_destination.hash.hex(),
        "config_dir": config_dir,
        "storage_path": storage_path,
    }
    if propagation_destination_hash_hex is not None:
        result["propagation_destination_hash"] = propagation_destination_hash_hex
        # PROPAGATION_COST_MIN floor is 13 in LXMRouter; tests need this
        # to set the sender's outbound stamp cost so the PN won't reject
        # an inbound LXM as low-stamp.
        result["propagation_stamp_cost"] = int(
            getattr(_state.router, "propagation_stamp_cost", 0) or 0
        )
    return result


def _apply_default_interface_attrs(iface):
    """Mirror the attrs that `RNS.Reticulum.interface_post_init` normally sets.

    The bridge constructs interfaces directly via the TCP{Client,Server}Interface
    constructors instead of going through Reticulum's config-file parser, which
    means the standard `interface_post_init` defaults never get applied. RNS
    Transport later assumes those attrs exist (`announce_rate_target`,
    `announce_cap`, `mode`, `ifac_size`, etc.) — a missing attr fires
    `AttributeError` deep inside Transport's announce/forward path. The
    visible failure mode is a TCPInterface that keeps reconnecting (the
    AttributeError gets caught by Transport's interface error handler) and
    LXMF outbound messages parking at `state='outbound'` because their
    underlying link establishment can't traverse the broken interface.

    This function applies the same defaults `interface_post_init` does for
    a plain `[interfaces] [[Name]]` block with no extra options.
    """
    from RNS.Interfaces.Interface import Interface

    iface.mode = Interface.MODE_FULL
    iface.announce_cap = RNS.Reticulum.ANNOUNCE_CAP / 100.0
    iface.bootstrap_only = False
    iface.optimise_mtu()
    iface.ifac_size = iface.DEFAULT_IFAC_SIZE
    iface.discoverable = False
    iface.discovery_announce_interval = None
    iface.discovery_publish_ifac = False
    iface.reachable_on = None
    iface.discovery_name = None
    iface.discovery_encrypt = False
    iface.discovery_stamp_value = None
    iface.discovery_latitude = None
    iface.discovery_longitude = None
    iface.discovery_height = None
    iface.discovery_frequency = None
    iface.discovery_bandwidth = None
    iface.discovery_modulation = None
    iface.announce_rate_target = None
    iface.announce_rate_grace = None
    iface.announce_rate_penalty = None
    iface.ingress_control = True
    iface.ifac_netname = None
    iface.ifac_netkey = None
    iface.ifac_signature = None
    iface.ifac_key = None
    iface.ifac_identity = None


def cmd_lxmf_add_tcp_server_interface(params):
    """Attach a TCPServerInterface listening on loopback.

    The harness calls this on the "server" side of a pair, gets the
    bound port back, then calls ``lxmf_add_tcp_client_interface`` on
    the "client" side pointing at that port.

    Why TCP loopback in Phase 1: a real PipeInterface backed by OS
    pipes is conceptually cleaner (no networking layer between
    bridges), but bringing it up reliably across Python's
    subprocess + macOS posix_spawn quirks consumed multiple cycles
    and isn't on the critical path for proving cross-impl LXMF
    interop. TCP-on-loopback gives the same direct-pair semantics
    with battle-tested machinery — same pattern reticulum-conformance
    uses for its wire-layer trios. Phase 2 ROADMAP item: revisit
    PipeInterface.

    params:
        bind_port (int, optional): explicit port. Defaults to 0
            (OS-assigned ephemeral port).
        name (str, optional): interface name; defaults to "tcpserver".

    Returns:
        port (int): the port the listener actually bound to.
        interface_name (str)
    """
    if _state.reticulum is None:
        raise RuntimeError("lxmf_init must be called before lxmf_add_tcp_server_interface")

    bind_port = int(params.get("bind_port") or 0) or _allocate_free_port()
    name = params.get("name") or "tcpserver"

    from RNS.Interfaces.TCPInterface import TCPServerInterface

    iface_config = {
        "name": name,
        "interface_enabled": "true",
        "type": "TCPServerInterface",
        "listen_ip": "127.0.0.1",
        "listen_port": str(bind_port),
    }
    iface = TCPServerInterface(RNS.Transport, iface_config)
    iface.OUT = True
    _apply_default_interface_attrs(iface)
    _state.reticulum._add_interface(iface)
    _state._interfaces.append(iface)

    return {"port": bind_port, "interface_name": name}


def cmd_lxmf_add_tcp_client_interface(params):
    """Attach a TCPClientInterface to a peer bridge's listener.

    params:
        target_host (str, optional): defaults to ``127.0.0.1``.
        target_port (int): the port the peer's
            ``lxmf_add_tcp_server_interface`` returned.
        name (str, optional): interface name; defaults to "tcpclient".

    Returns:
        interface_name (str)
    """
    if _state.reticulum is None:
        raise RuntimeError(
            "lxmf_init must be called before lxmf_add_tcp_client_interface"
        )

    target_host = params.get("target_host") or "127.0.0.1"
    target_port = int(params["target_port"])
    name = params.get("name") or "tcpclient"

    from RNS.Interfaces.TCPInterface import TCPClientInterface

    iface_config = {
        "name": name,
        "interface_enabled": "true",
        "type": "TCPClientInterface",
        "target_host": target_host,
        "target_port": str(target_port),
    }
    iface = TCPClientInterface(RNS.Transport, iface_config)
    iface.OUT = True
    _apply_default_interface_attrs(iface)
    _state.reticulum._add_interface(iface)
    _state._interfaces.append(iface)

    return {"interface_name": name}


def _allocate_free_port():
    """Bind a loopback socket to port 0, snapshot the OS port, close.

    Same trick reticulum-conformance uses (wire_tcp.py
    _allocate_free_port). The window between close and re-bind is
    technically a race; on localhost in single-test mode it never
    fires in practice.
    """
    import socket as _socket

    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def cmd_lxmf_announce(params):
    """Emit a delivery announce.

    LXMRouter ships ``announce(destination_hash)`` — we forward the
    bridge's own delivery destination hash. Useful for tests that want
    to re-announce on demand (the initial ``register_delivery_identity``
    in ``lxmf_init`` does NOT auto-announce in the Python LXMF; the
    Swift bridge follows suit).

    params: none

    Returns:
        delivery_destination_hash (hex)
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_announce")

    _state.router.announce(_state.delivery_destination.hash)
    # If this bridge is also a propagation node, re-announce the
    # propagation destination so peers learn the path. The initial
    # announce fired at `enable_propagation()` time goes out before
    # any test interface is attached, so without this nudge, sender
    # bridges in a 3-node `tcp_trio` topology would never learn the
    # propagation node's path.
    if getattr(_state.router, "propagation_node", False):
        _state.router.announce_propagation_node()
    return {"delivery_destination_hash": _state.delivery_destination.hash.hex()}


def cmd_lxmf_send_opportunistic(params):
    """Send an opportunistic LXMF message.

    Single-packet unicast — the recipient must already be reachable
    (an announce from the recipient must have been observed on this
    peer's RNS). The bridge surfaces an explicit error if the
    destination identity isn't recallable, instead of silently failing.

    params:
        destination_hash (hex): the recipient's LXMF delivery destination
            hash (16 bytes).
        content (str): UTF-8 message content. Keep small enough that
            packed message + msgpack overhead fits in
            LXMessage.ENCRYPTED_PACKET_MAX_CONTENT (~295 bytes); LXMF
            silently upgrades larger payloads to DIRECT, which is a
            different test path.
        title (str, optional): UTF-8 title; defaults to "".

    Returns:
        message_hash (hex): the LXMessage hash; tests track this via
            cmd_lxmf_get_message_state for delivery confirmation.
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_send_opportunistic")

    dest_hash_hex = params["destination_hash"]
    content = params["content"]
    title = params.get("title", "")
    fields = _decode_fields_param(params.get("fields"))
    # Optional explicit timestamp for tests that need byte-for-byte
    # reproducible wire bytes (notably the dedup conformance test, where
    # two consecutive sends must produce the same message_hash so the
    # receiver's dedup check actually has something to compare against).
    # When unset, LXMessage.pack() defaults to time.time() at line 362.
    forced_timestamp = params.get("timestamp")

    dest_hash = bytes.fromhex(dest_hash_hex)

    # Pre-check: the recipient identity must be recallable. RNS.Identity.
    # recall returns None when no announce has been observed. Failing
    # here surfaces a misordered fixture (test sent before announce
    # converged) instead of a confusing "message sent, never arrived".
    recipient_identity = RNS.Identity.recall(dest_hash)
    if recipient_identity is None:
        raise RuntimeError(
            f"No identity known for destination {dest_hash_hex}. The "
            f"recipient must announce its delivery destination before "
            f"this peer can send to it."
        )

    recipient_destination = RNS.Destination(
        recipient_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
    )

    # Hook a per-message delivery callback so we can track state
    # transitions in the bridge's own outbound state map.
    def state_callback(msg):
        if msg.hash:
            with _state._outbound_lock:
                new_state = _state_to_string(msg.state)
                _state._outbound_state[msg.hash] = new_state
                # Drop the live-message reference once the state is
                # terminal-for-bridge — at that point _outbound_state
                # holds the authoritative value and nothing else needs
                # the LXMessage object. Without this, the map grows
                # for the lifetime of the bridge process.
                if new_state in {"delivered", "failed"}:
                    _state._outbound_messages.pop(msg.hash, None)

    message = LXMF.LXMessage(
        destination=recipient_destination,
        source=_state.delivery_destination,
        content=content,
        title=title,
        fields=fields,
        desired_method=LXMF.LXMessage.OPPORTUNISTIC,
    )
    message.register_delivery_callback(state_callback)
    message.register_failed_callback(state_callback)

    # Pin timestamp BEFORE pack(). LXMessage.pack() at LXMessage.py:362
    # only assigns time.time() when self.timestamp is None, so a pre-set
    # value sticks. Required for the dedup test to produce two byte-
    # identical wire forms.
    if forced_timestamp is not None:
        message.timestamp = float(forced_timestamp)

    # Pack first so we can detect silent OPPORTUNISTIC -> DIRECT upgrades
    # before they happen. ENCRYPTED_PACKET_MAX_CONTENT is checked
    # internally during pack().
    if not getattr(message, "packed", None):
        message.pack()
    if message.desired_method != LXMF.LXMessage.OPPORTUNISTIC:
        raise RuntimeError(
            "Opportunistic delivery silently upgraded to method "
            f"{message.desired_method} — content+fields exceeded "
            f"LXMessage.ENCRYPTED_PACKET_MAX_CONTENT. Phase 1 only "
            f"covers single-packet OPPORTUNISTIC; shrink the payload."
        )

    _state.router.handle_outbound(message)

    msg_hash = message.hash if message.hash else b""
    if msg_hash:
        with _state._outbound_lock:
            _state._outbound_state[msg_hash] = _state_to_string(message.state)
            # Keep a live reference so cmd_lxmf_get_message_state can
            # read the current message.state directly. The router
            # advances state through OUTBOUND → SENDING → SENT →
            # DELIVERED but only invokes the registered callback at
            # terminal-for-method (DELIVERED for opportunistic, SENT
            # for propagated). The intermediate transitions never
            # reach _outbound_state, so polling it alone misses
            # progress that already happened on the message.
            _state._outbound_messages[msg_hash] = message

    return {"message_hash": msg_hash.hex()}


def cmd_lxmf_send_direct(params):
    """Send a DIRECT (link-based) LXMF message.

    Like opportunistic, but the message is sent over a Reticulum Link
    rather than as a single encrypted packet. The recipient must be
    reachable (announce observed) so we can establish the outbound
    link. LXMRouter handles link establishment internally — we just
    set ``desired_method=DIRECT`` and let the outbound thread do its
    thing.

    Direct messages can carry payloads larger than
    ``ENCRYPTED_PACKET_MAX_CONTENT`` because the link transport
    fragments via Resource transfer when the packed message exceeds
    ``LINK_PACKET_MAX_CONTENT``.

    params:
        destination_hash (hex): the recipient's LXMF delivery destination
            hash (16 bytes).
        content (str): UTF-8 message content.
        title (str, optional): UTF-8 title; defaults to "".

    Returns:
        message_hash (hex): the LXMessage hash; tests track this via
            cmd_lxmf_get_message_state for delivery confirmation.
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_send_direct")

    dest_hash_hex = params["destination_hash"]
    content = params["content"]
    title = params.get("title", "")
    fields = _decode_fields_param(params.get("fields"))

    dest_hash = bytes.fromhex(dest_hash_hex)

    recipient_identity = RNS.Identity.recall(dest_hash)
    if recipient_identity is None:
        raise RuntimeError(
            f"No identity known for destination {dest_hash_hex}. The "
            f"recipient must announce its delivery destination before "
            f"this peer can send to it."
        )

    recipient_destination = RNS.Destination(
        recipient_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
    )

    def state_callback(msg):
        if msg.hash:
            with _state._outbound_lock:
                new_state = _state_to_string(msg.state)
                _state._outbound_state[msg.hash] = new_state
                # Drop the live-message reference once the state is
                # terminal-for-bridge — at that point _outbound_state
                # holds the authoritative value and nothing else needs
                # the LXMessage object. Without this, the map grows
                # for the lifetime of the bridge process.
                if new_state in {"delivered", "failed"}:
                    _state._outbound_messages.pop(msg.hash, None)

    message = LXMF.LXMessage(
        destination=recipient_destination,
        source=_state.delivery_destination,
        content=content,
        title=title,
        fields=fields,
        desired_method=LXMF.LXMessage.DIRECT,
    )
    message.register_delivery_callback(state_callback)
    message.register_failed_callback(state_callback)

    _state.router.handle_outbound(message)

    msg_hash = message.hash if message.hash else b""
    if msg_hash:
        with _state._outbound_lock:
            _state._outbound_state[msg_hash] = _state_to_string(message.state)
            # Keep a live reference so cmd_lxmf_get_message_state can
            # read the current message.state directly. The router
            # advances state through OUTBOUND → SENDING → SENT →
            # DELIVERED but only invokes the registered callback at
            # terminal-for-method (DELIVERED for opportunistic, SENT
            # for propagated). The intermediate transitions never
            # reach _outbound_state, so polling it alone misses
            # progress that already happened on the message.
            _state._outbound_messages[msg_hash] = message

    return {"message_hash": msg_hash.hex()}


def cmd_lxmf_has_path(params):
    """Return whether RNS has both a path entry AND a recallable identity for `destination_hash`.

    LXMF's propagation flow checks ``RNS.Transport.has_path`` (path
    table) before deciding to establish a link; the bridge's send
    commands additionally need ``RNS.Identity.recall`` (so we can
    encrypt). Both signals matter for fixture-side convergence
    polling, so this command requires both — otherwise a transient
    state where the path is registered but the identity hasn't yet
    been resolved would make a polling fixture think convergence is
    done before the next send is actually safe.
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_has_path")
    dest_hash = bytes.fromhex(params["destination_hash"])
    has_path = bool(RNS.Transport.has_path(dest_hash))
    has_identity = RNS.Identity.recall(dest_hash) is not None
    return {
        "has_path": has_path and has_identity,
        # Per-component visibility for diagnostics.
        "transport_has_path": has_path,
        "identity_recalled": has_identity,
    }


def cmd_lxmf_request_path(params):
    """Solicit an announce for ``destination_hash`` via Reticulum's path-request flow.

    Used in 3-bridge propagation fixtures: lxmd announces its
    ``lxmf:propagation`` destination once on startup, but bridges
    that connect *after* that initial announce don't observe it.
    Without an active path-request from each bridge, the receiver
    + sender path tables never learn about lxmd, and
    ``lxmf_send_propagated`` parks at ``state='outbound'`` forever
    waiting for a route that doesn't exist.

    The call is fire-and-forget at the RNS layer; it doesn't block.
    The caller should poll ``lxmf_has_path`` (or just sleep) after
    issuing it. Returns a trivial ``{"requested": true}`` so callers
    have something to assert on.

    params:
        destination_hash (hex): 16-byte destination hash to solicit.
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_request_path")
    dest_hash = bytes.fromhex(params["destination_hash"])
    RNS.Transport.request_path(dest_hash)
    return {"requested": True}


def cmd_lxmf_set_outbound_propagation_node(params):
    """Configure this peer's outbound propagation node.

    The hash is the propagation destination hash that the propagation
    node bridge returned from `lxmf_init` (when started with
    `enable_propagation_node=true`). Once set, `lxmf_send_propagated`
    will route messages through that node.

    params:
        destination_hash (hex): 16-byte propagation destination hash.
    """
    if _state.router is None:
        raise RuntimeError(
            "lxmf_init must be called before lxmf_set_outbound_propagation_node"
        )
    dest_hash = bytes.fromhex(params["destination_hash"])
    _state.router.set_outbound_propagation_node(dest_hash)
    # Optionally pin the outbound stamp cost LXMF uses when uploading to
    # this propagation node. Without this, the bridge falls back on
    # whatever the announce-driven discovery has cached, which may be 0
    # in test fixtures where the PN's announce hasn't yet round-tripped.
    if "stamp_cost" in params:
        cost = int(params["stamp_cost"])
        _state.router.outbound_stamp_costs[dest_hash] = (time.time(), cost)
    return {"ok": True}


def cmd_lxmf_send_propagated(params):
    """Submit an LXMF message via the configured propagation node.

    Same param shape as opportunistic / direct. The router queues the
    message with `desired_method=PROPAGATED`; once a path to the
    outbound propagation node is known the LXM is uploaded to the node
    and stored there until the recipient syncs.

    Returns the message hash like the other send commands. The state
    transitions through SENDING → SENT (uploaded to PN) → and stays
    there from the sender's POV — there is no ack from the recipient
    on the PROPAGATED path until they sync.
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_send_propagated")

    dest_hash_hex = params["destination_hash"]
    content = params["content"]
    title = params.get("title", "")
    fields = _decode_fields_param(params.get("fields"))

    dest_hash = bytes.fromhex(dest_hash_hex)
    recipient_identity = RNS.Identity.recall(dest_hash)
    if recipient_identity is None:
        raise RuntimeError(
            f"No identity known for destination {dest_hash_hex}. The "
            f"recipient must announce its delivery destination before "
            f"this peer can send to it."
        )

    recipient_destination = RNS.Destination(
        recipient_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
    )

    def state_callback(msg):
        if msg.hash:
            with _state._outbound_lock:
                new_state = _state_to_string(msg.state)
                _state._outbound_state[msg.hash] = new_state
                # Drop the live-message reference once the state is
                # terminal-for-bridge — at that point _outbound_state
                # holds the authoritative value and nothing else needs
                # the LXMessage object. Without this, the map grows
                # for the lifetime of the bridge process.
                if new_state in {"delivered", "failed"}:
                    _state._outbound_messages.pop(msg.hash, None)

    message = LXMF.LXMessage(
        destination=recipient_destination,
        source=_state.delivery_destination,
        content=content,
        title=title,
        fields=fields,
        desired_method=LXMF.LXMessage.PROPAGATED,
    )
    message.register_delivery_callback(state_callback)
    message.register_failed_callback(state_callback)
    _state.router.handle_outbound(message)

    msg_hash = message.hash if message.hash else b""
    if msg_hash:
        with _state._outbound_lock:
            _state._outbound_state[msg_hash] = _state_to_string(message.state)
            # Keep a live reference so cmd_lxmf_get_message_state can
            # read the current message.state directly. The router
            # advances state through OUTBOUND → SENDING → SENT →
            # DELIVERED but only invokes the registered callback at
            # terminal-for-method (DELIVERED for opportunistic, SENT
            # for propagated). The intermediate transitions never
            # reach _outbound_state, so polling it alone misses
            # progress that already happened on the message.
            _state._outbound_messages[msg_hash] = message
    return {"message_hash": msg_hash.hex()}


def cmd_lxmf_sync_inbound(params):
    """Block until a propagation-node sync finishes (or times out).

    Issues `request_messages_from_propagation_node` and polls the
    router's propagation transfer state until the transfer is
    quiescent or the timeout elapses. The returned status string is
    the final propagation transfer state — tests assert on it
    plus the inbox having received the messages.

    params:
        timeout_sec (number, optional): how long to wait before
            giving up. Defaults to 30s — propagation sync includes
            a link establishment + LXMF resource transfer round
            trip, plus the propagation node's per-peer sync window.
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_sync_inbound")
    timeout_sec = float(params.get("timeout_sec", 30.0))

    # `request_messages_from_propagation_node` takes the recipient
    # identity (we are the recipient on this side). max_messages=0
    # leaves the cap at the LXMF default; tests don't need to
    # constrain it.
    _state.router.request_messages_from_propagation_node(_state.identity)

    # Poll the router's propagation transfer state. The states we
    # treat as terminal: 0 (idle/done), -1 (failed). LXMF uses
    # PR_IDLE = 0 by default; transient states during transfer are
    # > 0. We bail on the first idle state seen after the request.
    deadline = time.time() + timeout_sec
    final_state = None
    while time.time() < deadline:
        final_state = getattr(_state.router, "propagation_transfer_state", None)
        if final_state in (None, 0, -1):
            # Sleep a bit so an in-flight transfer that hasn't yet
            # raised its state has a chance to be observed.
            time.sleep(0.5)
            final_state = getattr(_state.router, "propagation_transfer_state", None)
            break
        time.sleep(0.2)

    return {
        "final_state": final_state if final_state is not None else 0,
    }


def cmd_lxmf_get_received_messages(params):
    """Drain (or peek) inbound messages.

    Returns messages with ``seq > since_seq``; pass ``since_seq=0``
    on first call. Non-destructive — the inbox is append-only and
    tests poll incrementally. The harness emits monotonic seq numbers
    so a polling test can use ``since_seq=last_seq_seen`` to avoid
    duplicates.

    params:
        since_seq (int, optional): minimum seq to return; defaults 0.

    Returns:
        messages (list[dict]): each entry contains seq, message_hash,
            source_hash, destination_hash, title, content, method,
            ack_status, received_at_ms.
        last_seq (int): the highest seq currently in the inbox; pass
            this back as since_seq on the next poll.
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_get_received_messages")

    since_seq = int(params.get("since_seq", 0))
    with _state._inbox_lock:
        messages = [m for m in _state._inbox if m["seq"] > since_seq]
        last_seq = _state._inbox_seq
    return {"messages": messages, "last_seq": last_seq}


def cmd_lxmf_get_message_state(params):
    """Poll outbound message state.

    State transitions during a successful opportunistic send:
        generating -> outbound -> sending -> sent -> delivered

    The "delivered" transition fires when the recipient's RNS sends a
    proof packet back. Tests assert ``state == "delivered"`` to prove
    the message reached the receiver.

    params:
        message_hash (hex): hash returned by lxmf_send_opportunistic
            (or any other send command).

    Returns:
        state (str): canonical state name. Returns "unknown" if the
            hash wasn't found; tests should treat that as "not yet
            tracked" and retry with a small backoff.
    """
    if _state.router is None:
        raise RuntimeError("lxmf_init must be called before lxmf_get_message_state")

    msg_hash = bytes.fromhex(params["message_hash"])
    with _state._outbound_lock:
        state = _state._outbound_state.get(msg_hash, "unknown")
        live_message = _state._outbound_messages.get(msg_hash)
        # Read live_message.state inside the lock so the map lookup
        # and the attribute read happen atomically with respect to
        # other threads calling cmd_lxmf_get_message_state. The lock
        # does NOT synchronize with LXMF's writer thread (it doesn't
        # hold our lock), but a single attribute read is GIL-atomic
        # in CPython, which is the only python the bridge supports.
        live_state = (
            _state_to_string(live_message.state)
            if live_message is not None
            else None
        )

    # Prefer the recorded state when it's already terminal-for-bridge
    # (delivered/failed) — those values are authoritative because they
    # come from the actual delivery/failed callback firings. Otherwise
    # consider the live LXMessage.state, but never downgrade ordinally:
    # the router progresses OUTBOUND → SENDING → SENT → DELIVERED, and
    # _outbound_state can be ahead of live_message.state if the
    # callback fired for SENT while the LXMessage object briefly shows
    # SENDING in a re-read window. Use ordinal comparison so the merged
    # value is always the most-advanced state we can prove.
    if live_state is not None and _STATE_ORDER.get(live_state, 0) > _STATE_ORDER.get(state, 0):
        state = live_state

    # If we haven't heard back via the per-message callback yet, walk
    # LXMF's failed_outbound list as a fallback. Delivered state always
    # arrives via the callback (LXMessage.__mark_delivered fires it on
    # state = DELIVERED), so missing the callback for delivered would
    # be a real bug — we don't paper over that here.
    if state in {"unknown", "outbound", "sending", "sent"} and _state.router is not None:
        for m in getattr(_state.router, "failed_outbound", []) or []:
            if getattr(m, "hash", None) == msg_hash:
                state = "failed"
                with _state._outbound_lock:
                    _state._outbound_state[msg_hash] = state
                break

    return {"state": state}


def cmd_lxmf_shutdown(params):
    """Tear down RNS, LXMF, and any pipe interfaces.

    Idempotent — calling twice returns ``stopped: false`` the second
    time. The pytest fixture calls this from finalizers so a crashed
    test doesn't leak file descriptors or threads.
    """
    stopped = False
    for iface in list(_state._interfaces):
        try:
            iface.teardown()
        except Exception:
            pass
        try:
            if iface in RNS.Transport.interfaces:
                RNS.Transport.interfaces.remove(iface)
        except Exception:
            pass
    _state._interfaces = []

    if _state.router is not None:
        # LXMRouter doesn't expose a clean stop; its threads are daemons
        # and die with the process. Best effort: replace our delivery
        # callback with a no-op so spurious late inbounds don't touch
        # the soon-to-be-cleared queue.
        try:
            _state.router.register_delivery_callback(lambda _msg: None)
        except Exception:
            pass
        _state.router = None
        stopped = True

    # Storage path is a tempdir we own; nuke it so test runs don't
    # accumulate hundreds of temp message dirs.
    import shutil

    for path in (_state.storage_path, _state.config_dir):
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    _state.storage_path = None
    _state.config_dir = None
    _state.identity = None
    _state.delivery_destination = None
    _state.reticulum = None

    with _state._outbound_lock:
        _state._outbound_state.clear()
        _state._outbound_messages.clear()

    return {"stopped": stopped}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bytes_to_str(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _method_to_string(method):
    """Map LXMessage.method (int) to canonical string.

    The bridge protocol exposes string names so the swift / kotlin
    bridges don't have to agree on a private int mapping. Names match
    LXMF's own constant names.
    """
    if method == LXMF.LXMessage.OPPORTUNISTIC:
        return "opportunistic"
    if method == LXMF.LXMessage.DIRECT:
        return "direct"
    if method == LXMF.LXMessage.PROPAGATED:
        return "propagated"
    return "unknown"


def _state_to_string(state):
    """Map LXMessage.state (int) to canonical string.

    Mirrors LXMF.LXMessage state constants. The bridge maps to strings
    so the test surface stays language-agnostic; future Phase 2 tests
    that assert on intermediate states (e.g. ``sending``) can use the
    same names regardless of which impl is on the SUT side.
    """
    constants = {
        LXMF.LXMessage.GENERATING: "generating",
        LXMF.LXMessage.OUTBOUND: "outbound",
        LXMF.LXMessage.SENDING: "sending",
        LXMF.LXMessage.SENT: "sent",
        LXMF.LXMessage.DELIVERED: "delivered",
        LXMF.LXMessage.FAILED: "failed",
    }
    return constants.get(state, f"state_{state}")


# Strict monotone progression of bridge-visible state names. Used by
# cmd_lxmf_get_message_state to merge a callback-driven recorded state
# with a live LXMessage.state read without ever moving backwards. Any
# unknown / non-canonical name maps to 0 so it loses to any known state.
_STATE_ORDER = {
    "unknown": 0,
    "generating": 1,
    "outbound": 2,
    "sending": 3,
    "sent": 4,
    "delivered": 5,
    "failed": 5,
}


def _ack_status_to_string(_attempts, method_name):
    """Phase 1 ack_status placeholder.

    For OPPORTUNISTIC delivery, the absence of a proof error means the
    message reached the recipient (proof callback would have fired on
    the SENDER side, not the receiver). The receiver-side ack_status
    therefore is always ``"received"``. Phase 2 will distinguish
    ``"received_with_proof"`` vs ``"received_no_proof"`` for direct /
    propagated paths.
    """
    return "received"


# --------------------------------------------------------------------------- #
# Command dispatch
# --------------------------------------------------------------------------- #


COMMANDS = {
    "lxmf_init": cmd_lxmf_init,
    "lxmf_add_tcp_server_interface": cmd_lxmf_add_tcp_server_interface,
    "lxmf_add_tcp_client_interface": cmd_lxmf_add_tcp_client_interface,
    "lxmf_announce": cmd_lxmf_announce,
    "lxmf_send_opportunistic": cmd_lxmf_send_opportunistic,
    "lxmf_send_direct": cmd_lxmf_send_direct,
    "lxmf_has_path": cmd_lxmf_has_path,
    "lxmf_request_path": cmd_lxmf_request_path,
    "lxmf_set_outbound_propagation_node": cmd_lxmf_set_outbound_propagation_node,
    "lxmf_send_propagated": cmd_lxmf_send_propagated,
    "lxmf_sync_inbound": cmd_lxmf_sync_inbound,
    "lxmf_get_received_messages": cmd_lxmf_get_received_messages,
    "lxmf_get_message_state": cmd_lxmf_get_message_state,
    "lxmf_shutdown": cmd_lxmf_shutdown,
}


def _handle_request(line):
    """Dispatch one JSON-RPC request line. Returns a response dict."""
    try:
        request = json.loads(line)
    except json.JSONDecodeError as e:
        return {"id": "parse_error", "success": False, "error": f"JSON parse: {e}"}

    req_id = request.get("id", "")
    command = request.get("command", "")
    params = request.get("params", {}) or {}

    handler = COMMANDS.get(command)
    if handler is None:
        return {"id": req_id, "success": False, "error": f"Unknown command: {command}"}

    try:
        result = handler(params)
        return {"id": req_id, "success": True, "result": result}
    except Exception as e:
        return {
            "id": req_id,
            "success": False,
            "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        }


def _main():
    """Stdio JSON-RPC loop.

    READY is printed BEFORE the loop starts so the test harness sees
    the bridge as live before sending the first command. Stderr stays
    open for RNS / LXMF logs (the bridge_client filters non-JSON lines
    on stdout, so they're not strictly fatal there either, but stderr
    is the convention).
    """
    print("READY", flush=True)
    # After READY, route any further stdout writes to stderr. RNS.log
    # writes to sys.stdout by default and would otherwise leak onto the
    # JSON-RPC response channel, where the test harness silently drops
    # them as non-JSON. Reroute so that diagnostics from the underlying
    # RNS / LXMF stack are visible to anyone running the conformance suite.
    # The JSON-RPC writes below use a captured stdout reference so they
    # still go to the real stdout.
    _stdout_for_rpc = sys.stdout
    sys.stdout = sys.stderr
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = _handle_request(line)
        print(json.dumps(response), flush=True, file=_stdout_for_rpc)


if __name__ == "__main__":
    _main()
