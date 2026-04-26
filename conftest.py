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


@pytest.fixture
def server_impl(request):
    """Server-role impl name. Set by parametrization."""
    return request.param if hasattr(request, "param") else request.node.callspec.params["server_impl"]


@pytest.fixture
def client_impl(request):
    """Client-role impl name. Set by parametrization."""
    return request.param if hasattr(request, "param") else request.node.callspec.params["client_impl"]


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

    def send_opportunistic(self, recipient_hash, content, title=""):
        """Send opportunistic LXMF; returns the message hash."""
        result = self.bridge.execute(
            "lxmf_send_opportunistic",
            destination_hash=recipient_hash.hex(),
            content=content,
            title=title,
        )
        return bytes.fromhex(result["message_hash"])

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
