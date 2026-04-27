"""Pytest configuration for the LXMF conformance suite.

Cross-impl parametrization: every Phase 1 test is parametrized over
``(server_impl, client_impl)`` pairs drawn from the registered impls.
Phase 1 ships with python and swift; kotlin entries are present but
xfail-skipped until the kotlin bridge lands (Phase 2).

Each impl is a long-running bridge subprocess speaking JSON-RPC over
stdin/stdout. See ``bridge_client.py`` for the wire protocol and
``reference/lxmf_python.py`` for the reference implementation.

The ``pipe_pair`` fixture sets up two anonymous OS pipes connecting two
bridges in opposite directions and yields a tuple of started
``BridgeClient`` instances. This is the workhorse for direct-
connectivity tests — no TCP, no transport node, just two LXMF nodes
talking through a kernel pipe.
"""

import os
import warnings

import pytest

from bridge_client import BridgeClient

# Bridge command templates. Each accepts a ``{root}`` placeholder for
# this repo's root path (resolved at runtime so tests work from any cwd).
#
# Per-impl env overrides (CONFORMANCE_<IMPL>_BRIDGE_CMD) take precedence,
# letting CI inject freshly-built bridges without hardcoding paths into
# the test config.
BRIDGE_COMMANDS = {
    "python": "python3 {root}/reference/lxmf_python.py",
    "swift": "{root}/../LXMF-swift/.build/release/LXMFConformanceBridge",
    # Kotlin lands in Phase 2. The placeholder lets parametrization
    # emit kotlin-paired test IDs that pytest can xfail/skip cleanly.
    "kotlin": "java -jar {root}/../LXMF-kt/conformance-bridge/build/libs/LXMFConformanceBridge.jar",
}

PER_IMPL_CMD_ENV = {
    "python": "CONFORMANCE_PYTHON_BRIDGE_CMD",
    "swift": "CONFORMANCE_SWIFT_BRIDGE_CMD",
    "kotlin": "CONFORMANCE_KOTLIN_BRIDGE_CMD",
}

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_bridge_command(impl_name):
    """Resolve the bridge command for an impl name.

    Precedence (highest first):
      1. ``CONFORMANCE_<IMPL>_BRIDGE_CMD`` env var
      2. ``BRIDGE_COMMANDS[impl]`` with ``{root}`` substitution
    """
    per_impl_var = PER_IMPL_CMD_ENV.get(impl_name)
    if per_impl_var:
        cmd = os.environ.get(per_impl_var)
        if cmd:
            return cmd
    if impl_name not in BRIDGE_COMMANDS:
        raise ValueError(f"Unknown implementation: {impl_name}")
    return BRIDGE_COMMANDS[impl_name].format(root=ROOT_DIR)


def _bridge_command_available(impl_name):
    """Return True if the bridge for ``impl_name`` is runnable.

    For ``python`` we always assume yes (the reference bridge ships
    in this repo). For ``swift`` and ``kotlin`` we check whether the
    binary at the resolved path exists — missing binaries cause the
    impl to be filtered out of parametrization rather than producing
    noisy startup failures in every test.
    """
    if impl_name == "python":
        return True
    cmd = resolve_bridge_command(impl_name)
    # Resolve the executable path — last whitespace-split token of the
    # command. For ``java -jar /path/to.jar`` that's the JAR; for the
    # swift binary it's the binary path itself.
    exe = cmd.split()[-1]
    return os.path.exists(exe)


def _env_for_impl(impl_name):
    """Per-impl env overlay for the BridgeClient.

    Python needs ``PYTHON_RNS_PATH`` and ``PYTHON_LXMF_PATH`` if the
    test runner is using checked-out repos (CI sets these from
    ``actions/checkout`` paths). Swift and Kotlin bridges link their
    crypto + protocol libs at build time so they need nothing.
    """
    if impl_name == "python":
        return {
            "PYTHON_RNS_PATH": os.environ.get(
                "PYTHON_RNS_PATH", os.path.expanduser("~/repos/Reticulum")
            ),
            "PYTHON_LXMF_PATH": os.environ.get(
                "PYTHON_LXMF_PATH", os.path.expanduser("~/repos/LXMF")
            ),
        }
    return {}


def pytest_addoption(parser):
    parser.addoption(
        "--impls",
        action="store",
        default=None,
        help=(
            "Comma-separated list of impls to parametrize over (e.g. "
            "'python,swift'). Defaults to all auto-detected impls."
        ),
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "phase2: deferred to Phase 2")


def get_active_impls(config):
    """Return the list of impls active for this pytest run.

    Default behaviour: every impl whose bridge binary is detectable
    on disk. That means a fresh checkout with no swift build only
    runs python↔python pairs — no spurious failures from missing
    binaries.
    """
    forced = config.getoption("--impls")
    if forced:
        impls = [s.strip() for s in forced.split(",") if s.strip()]
        for i in impls:
            if i not in BRIDGE_COMMANDS:
                raise ValueError(f"Unknown impl in --impls: {i}")
        return impls

    detected = [i for i in BRIDGE_COMMANDS if _bridge_command_available(i)]
    if not detected:
        warnings.warn(
            "No bridges detected; only the in-repo python bridge will run. "
            "Build the swift bridge with `swift build -c release` in the "
            "LXMF-swift checkout to enable cross-impl tests."
        )
        detected = ["python"]
    return detected


def pytest_generate_tests(metafunc):
    """Parametrize ``server_impl`` and ``client_impl`` fixtures.

    Tests opt in by listing ``server_impl`` and ``client_impl`` in
    their argument list; the cross product is generated automatically
    so a test author writes one test body and gets coverage of every
    pair.
    """
    if "server_impl" in metafunc.fixturenames and "client_impl" in metafunc.fixturenames:
        impls = get_active_impls(metafunc.config)
        pairs = [(s, c) for s in impls for c in impls]
        ids = [f"{s}->{c}" for s, c in pairs]
        metafunc.parametrize(
            ("server_impl", "client_impl"), pairs, ids=ids, scope="function"
        )

    # tcp_trio tests opt in via `sender_impl` + `receiver_impl`. The
    # propagation-node role is always python — swift LXMF doesn't host
    # propagation nodes (`router.enable_propagation()` is python-only).
    if "sender_impl" in metafunc.fixturenames and "receiver_impl" in metafunc.fixturenames:
        impls = get_active_impls(metafunc.config)
        pairs = [(s, r) for s in impls for r in impls]
        ids = [f"{s}->python_pn->{r}" for s, r in pairs]
        metafunc.parametrize(
            ("sender_impl", "receiver_impl"), pairs, ids=ids, scope="function"
        )


@pytest.fixture
def server_impl(request):
    """Server-role impl name. Set by parametrization."""
    return request.param if hasattr(request, "param") else request.node.callspec.params["server_impl"]


@pytest.fixture
def client_impl(request):
    """Client-role impl name. Set by parametrization."""
    return request.param if hasattr(request, "param") else request.node.callspec.params["client_impl"]


@pytest.fixture
def sender_impl(request):
    """Sender-role impl name in 3-bridge propagation topology."""
    return request.param if hasattr(request, "param") else request.node.callspec.params["sender_impl"]


@pytest.fixture
def receiver_impl(request):
    """Receiver-role impl name in 3-bridge propagation topology."""
    return request.param if hasattr(request, "param") else request.node.callspec.params["receiver_impl"]


@pytest.fixture
def pipe_pair(server_impl, client_impl):
    """Two LXMF bridges connected via TCP loopback.

    "pipe_pair" is the public name kept for forward compatibility with
    the Phase 2 PipeInterface fixture; today it sets up TCP instead.
    Same direct-pair semantics — no transport hops, no propagation
    node — just two LXMF nodes hearing each other on loopback.

    Topology:

        server_bridge  <─── 127.0.0.1:port ───>  client_bridge
        (TCPServer)                              (TCPClient)

    Setup:
      1. Spawn both bridges.
      2. server_bridge.lxmf_init, client_bridge.lxmf_init.
      3. server_bridge.lxmf_add_tcp_server_interface — returns the
         OS-assigned port.
      4. client_bridge.lxmf_add_tcp_client_interface (target_port = the
         server's port).
      5. Both bridges announce; we wait for announces to traverse the
         TCP link and for path discovery to converge.

    Yields:
        (server, client) tuple of ``_BridgeNode`` wrappers, each
        carrying the BridgeClient + the LXMF identity hash + the
        delivery destination hash. Tests use the wrapper's helper
        methods (send_opportunistic, get_received_messages, etc.)
        so cross-impl payloads stay consistent.

    Phase 2 will reintroduce a real PipeInterface backend; the test
    surface won't change.
    """
    import time

    server_cmd = resolve_bridge_command(server_impl)
    client_cmd = resolve_bridge_command(client_impl)

    server_bridge = None
    client_bridge = None

    try:
        server_bridge = BridgeClient(
            server_cmd, env=_env_for_impl(server_impl)
        )
        client_bridge = BridgeClient(
            client_cmd, env=_env_for_impl(client_impl)
        )

        server_init = server_bridge.execute(
            "lxmf_init", display_name=f"server-{server_impl}"
        )
        client_init = client_bridge.execute(
            "lxmf_init", display_name=f"client-{client_impl}"
        )

        # Bring up the TCP link. Server binds first so we know the port
        # before the client tries to connect. Settle briefly so the
        # client's first announce doesn't race the listener accept.
        server_iface = server_bridge.execute(
            "lxmf_add_tcp_server_interface", name="serverlistener"
        )
        listener_port = int(server_iface["port"])
        client_bridge.execute(
            "lxmf_add_tcp_client_interface",
            target_host="127.0.0.1",
            target_port=listener_port,
            name="clientconnector",
        )
        # Brief settle window for the TCP connect to complete on the
        # client side. RNS's TCPClientInterface establishes
        # asynchronously; without this gap the first announce can race
        # the connect.
        time.sleep(1.5)

        # Both peers announce. Stagger so the announces don't collide
        # on the same tick — empirically a 500ms gap is enough to keep
        # the path discovery deterministic on loopback.
        server_bridge.execute("lxmf_announce")
        time.sleep(0.5)
        client_bridge.execute("lxmf_announce")

        # Settle for path discovery. 3s is comfortably above the per-
        # hop announce-propagation budget on loopback (sub-50ms) while
        # still failing tests fast if convergence is broken.
        time.sleep(3.0)

        server = _BridgeNode(
            bridge=server_bridge,
            impl=server_impl,
            identity_hash=bytes.fromhex(server_init["identity_hash"]),
            delivery_hash=bytes.fromhex(server_init["delivery_destination_hash"]),
        )
        client = _BridgeNode(
            bridge=client_bridge,
            impl=client_impl,
            identity_hash=bytes.fromhex(client_init["identity_hash"]),
            delivery_hash=bytes.fromhex(client_init["delivery_destination_hash"]),
        )

        yield server, client

    finally:
        # Tear down in reverse order. BridgeClient.close() reaps the
        # subprocess after lxmf_shutdown does the LXMF-level cleanup.
        for bridge in (client_bridge, server_bridge):
            if bridge is None:
                continue
            try:
                bridge.execute("lxmf_shutdown")
            except Exception:
                pass
            try:
                bridge.close()
            except Exception:
                pass


@pytest.fixture
def tcp_trio(sender_impl, receiver_impl):
    """Three LXMF bridges connected via TCP loopback in a star.

    Topology:

        sender (TCPClient)  ──┐
                               ├─→ propagation_node (TCPServer, python)
        receiver (TCPClient) ──┘

    The propagation_node role is always **python** — swift LXMF does
    not implement `enable_propagation()` (the in-process propagation
    daemon is python-only; production deployments run lxmd as a
    separate subprocess but bringing that into the test surface
    requires shared-instance machinery the bridges don't currently
    expose). Sender and receiver are parametrized over every
    detected impl, so a 2-impl detector (python, swift) yields 4
    `(sender, receiver)` trios.

    Sequence:
      1. Spawn three bridges.
      2. PN: `lxmf_init(enable_propagation_node=True)` —
         publishes `lxmf:propagation` and returns the propagation
         destination hash.
      3. PN: `lxmf_add_tcp_server_interface` — returns the bound port.
      4. Sender + receiver each: `lxmf_init`, then
         `lxmf_add_tcp_client_interface(target_port=port)`.
      5. PN announces propagation; sender + receiver announce their
         delivery destinations. We settle for path convergence.
      6. Sender: `lxmf_set_outbound_propagation_node(pn_hash)`.
    """
    import time

    sender_cmd = resolve_bridge_command(sender_impl)
    receiver_cmd = resolve_bridge_command(receiver_impl)
    pn_cmd = resolve_bridge_command("python")

    sender_bridge = None
    receiver_bridge = None
    pn_bridge = None

    try:
        pn_bridge = BridgeClient(pn_cmd, env=_env_for_impl("python"))
        sender_bridge = BridgeClient(sender_cmd, env=_env_for_impl(sender_impl))
        receiver_bridge = BridgeClient(receiver_cmd, env=_env_for_impl(receiver_impl))

        pn_init = pn_bridge.execute(
            "lxmf_init",
            display_name="conformance-pn",
            enable_propagation_node=True,
        )
        if "propagation_destination_hash" not in pn_init:
            raise RuntimeError(
                "PN bridge did not return propagation_destination_hash; "
                "either enable_propagation_node failed or the python "
                "bridge is out of date."
            )

        sender_init = sender_bridge.execute(
            "lxmf_init", display_name=f"sender-{sender_impl}"
        )
        receiver_init = receiver_bridge.execute(
            "lxmf_init", display_name=f"receiver-{receiver_impl}"
        )

        # PN binds first so the clients have a port to dial.
        pn_iface = pn_bridge.execute(
            "lxmf_add_tcp_server_interface", name="pnlistener"
        )
        listener_port = int(pn_iface["port"])

        sender_bridge.execute(
            "lxmf_add_tcp_client_interface",
            target_host="127.0.0.1",
            target_port=listener_port,
            name="sender_to_pn",
        )
        receiver_bridge.execute(
            "lxmf_add_tcp_client_interface",
            target_host="127.0.0.1",
            target_port=listener_port,
            name="receiver_to_pn",
        )
        time.sleep(1.5)

        # Sender + receiver announce their delivery destinations so
        # the PN learns about them. PN's `lxmf_announce` also fires
        # `announce_propagation_node()` so the lxmf:propagation
        # destination lands in sender + receiver path tables — without
        # that, swift's outbound thread observes `hasPath=false` for
        # the PN and the message never leaves `outbound`.
        for _ in range(2):
            pn_bridge.execute("lxmf_announce")
            time.sleep(0.4)
            sender_bridge.execute("lxmf_announce")
            time.sleep(0.4)
            receiver_bridge.execute("lxmf_announce")
            time.sleep(0.4)

        pn_hash = bytes.fromhex(pn_init["propagation_destination_hash"])
        # 3-bridge transport-mode topologies converge slower than the
        # 2-bridge direct pair: announces have to traverse spawned
        # peers, transport-mode forwarding has its own bandwidth caps,
        # and stamp generation work on the sender side adds even more
        # latency to the first propagation send. A fixed 8s settle is
        # generous enough that running multiple `tcp_trio` fixtures in
        # sequence stays deterministic.
        time.sleep(8.0)

        # Pin the stamp cost the PN actually requires (PROPAGATION_COST
        # in LXMF, with PROPAGATION_COST_MIN=13 floor). Without this the
        # sender's outbound stamp generation falls back to "no work"
        # (32 random bytes) and the PN rejects the upload as low-stamp.
        pn_stamp_cost = int(pn_init.get("propagation_stamp_cost", 0))
        sender_bridge.execute(
            "lxmf_set_outbound_propagation_node",
            destination_hash=pn_hash.hex(),
            stamp_cost=pn_stamp_cost,
        )
        receiver_bridge.execute(
            "lxmf_set_outbound_propagation_node",
            destination_hash=pn_hash.hex(),
            stamp_cost=pn_stamp_cost,
        )

        sender = _BridgeNode(
            bridge=sender_bridge,
            impl=sender_impl,
            identity_hash=bytes.fromhex(sender_init["identity_hash"]),
            delivery_hash=bytes.fromhex(sender_init["delivery_destination_hash"]),
        )
        pn = _BridgeNode(
            bridge=pn_bridge,
            impl="python",
            identity_hash=bytes.fromhex(pn_init["identity_hash"]),
            delivery_hash=bytes.fromhex(pn_init["delivery_destination_hash"]),
        )
        pn.propagation_hash = pn_hash
        receiver = _BridgeNode(
            bridge=receiver_bridge,
            impl=receiver_impl,
            identity_hash=bytes.fromhex(receiver_init["identity_hash"]),
            delivery_hash=bytes.fromhex(receiver_init["delivery_destination_hash"]),
        )

        yield sender, pn, receiver

    finally:
        for bridge in (receiver_bridge, sender_bridge, pn_bridge):
            if bridge is None:
                continue
            try:
                bridge.execute("lxmf_shutdown")
            except Exception:
                pass
            try:
                bridge.close()
            except Exception:
                pass


class _BridgeNode:
    """Test-side wrapper around a BridgeClient + LXMF metadata.

    Adds typed helper methods so tests don't have to repeat
    hex-encoding / decoding boilerplate. Mirrors the
    ``_LxmfPeer`` wrapper in reticulum-conformance for symmetry.
    """

    def __init__(self, bridge, impl, identity_hash, delivery_hash):
        self.bridge = bridge
        self.impl = impl
        self.identity_hash = identity_hash
        self.delivery_hash = delivery_hash
        # Tracks the highest received-message seq each side has seen
        # so polling tests can resume incrementally without manual
        # bookkeeping.
        self._last_seq = 0

    def announce(self):
        """Emit an LXMF delivery announce."""
        self.bridge.execute("lxmf_announce")

    def send_opportunistic(self, recipient_hash, content, title="", fields=None):
        """Send opportunistic LXMF; returns the message hash.

        ``fields`` is the bridge wire-format `fields` dict — keys are
        field id strings, values are tagged objects (see
        `lxmf_python.py::_decode_field_value_from_params`).
        """
        params = {
            "destination_hash": recipient_hash.hex(),
            "content": content,
            "title": title,
        }
        if fields is not None:
            params["fields"] = fields
        result = self.bridge.execute("lxmf_send_opportunistic", **params)
        return bytes.fromhex(result["message_hash"])

    def send_direct(self, recipient_hash, content, title="", fields=None):
        """Send DIRECT (link-based) LXMF; returns the message hash."""
        params = {
            "destination_hash": recipient_hash.hex(),
            "content": content,
            "title": title,
        }
        if fields is not None:
            params["fields"] = fields
        result = self.bridge.execute("lxmf_send_direct", **params)
        return bytes.fromhex(result["message_hash"])

    def send_propagated(self, recipient_hash, content, title="", fields=None):
        """Send PROPAGATED LXMF via the configured outbound propagation
        node; returns the message hash."""
        params = {
            "destination_hash": recipient_hash.hex(),
            "content": content,
            "title": title,
        }
        if fields is not None:
            params["fields"] = fields
        result = self.bridge.execute("lxmf_send_propagated", **params)
        return bytes.fromhex(result["message_hash"])

    def sync_inbound(self, timeout_sec=30.0):
        """Pull queued messages from the configured propagation node.

        Blocks until the transfer completes (or times out). Returns
        the bridge's reported `final_state` string so tests can assert
        on it; the actual messages land in the regular inbox via the
        delivery callback and are observable via `drain_received`.
        """
        return self.bridge.execute("lxmf_sync_inbound", timeout_sec=timeout_sec)

    def drain_received(self):
        """Return all received messages since last drain."""
        result = self.bridge.execute(
            "lxmf_get_received_messages", since_seq=self._last_seq
        )
        self._last_seq = int(result.get("last_seq", self._last_seq))
        return list(result.get("messages", []))

    def message_state(self, message_hash):
        result = self.bridge.execute(
            "lxmf_get_message_state", message_hash=message_hash.hex()
        )
        return result["state"]
