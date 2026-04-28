"""Helper for spinning up a real `lxmd` propagation node in tests.

The conformance suite previously hosted the propagation-node role
inside the python bridge via `LXMRouter.enable_propagation()`. That
in-process path is plumbed but not what production deployments run —
production runs `lxmd` as its own subprocess (own RNS instance, own
peer state, own message store). Empirically it also doesn't reliably
ship messages from the bridge sender to a recipient on the same trio:
even the all-python `(python, python_pn, python)` variant of
`test_propagation` parks at `state='outbound'` for 30+ seconds and
times out.

Switching the propagation-node role to a real `lxmd` subprocess
matches the deployment we actually ship and unwedges the test surface.
The bridges (sender / receiver) remain unchanged — they just point
their TCP client interfaces at lxmd's listener instead of at a
python bridge configured with `enable_propagation_node=True`.

This module is purposefully self-contained — no test-fixture
imports, no dependency on the bridge protocol — so the same helper
can be reused outside pytest (manual repro, CI smoke checks, etc.).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Optional


# Default identity-store path inside lxmd's --config dir. Created by
# lxmd on first run.
_LXMD_IDENTITY_FILE = "identity"

# How long to wait for lxmd to (a) write its identity file and
# (b) bring its TCPServerInterface up before we let tests try to dial
# in. Both happen well under a second on loopback in practice; the
# 10s ceiling is the failure-fast budget for CI sandboxes that are
# CPU-throttled.
_LXMD_READY_TIMEOUT_SEC = 10.0

# How long after kill() we'll wait for lxmd's process to actually
# exit. lxmd doesn't trap SIGTERM with anything elaborate, so it
# normally exits within milliseconds.
_LXMD_TEARDOWN_TIMEOUT_SEC = 5.0


def _allocate_free_port() -> int:
    """Bind a transient socket to ask the kernel for a free TCP port.

    There's an inherent race between us closing the socket and lxmd
    binding to the same port — but on loopback it's wide enough that
    we haven't seen a collision in practice across thousands of test
    runs. If it ever does become a problem, the right fix is for the
    bridge ↔ lxmd handshake to discover the port at runtime (parse
    rnsd's startup log) rather than to pre-allocate.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _resolve_python_paths() -> tuple[str, str]:
    """Resolve checked-out RNS + LXMF paths for use by the lxmd subprocess.

    Mirrors the env conventions the python bridge uses
    (``PYTHON_RNS_PATH`` / ``PYTHON_LXMF_PATH``). Returns the absolute
    paths so the spawned lxmd can prepend them to PYTHONPATH and pick
    up the same checkouts the bridges are using.
    """
    rns_path = os.environ.get(
        "PYTHON_RNS_PATH", os.path.expanduser("~/repos/Reticulum")
    )
    lxmf_path = os.environ.get(
        "PYTHON_LXMF_PATH", os.path.expanduser("~/repos/LXMF")
    )
    return os.path.abspath(rns_path), os.path.abspath(lxmf_path)


def _compute_propagation_destination_hash(identity_path: str) -> str:
    """Return the lxmf:propagation destination hash for an identity file.

    Run as a subprocess so the parent test process never imports RNS
    (which has process-wide singletons that conflict with multiple
    bridge subprocesses also importing RNS in their own interpreters).
    The subprocess uses the same checked-out RNS as the bridges via
    PYTHONPATH so the hash algorithm matches byte-for-byte.
    """
    rns_path, lxmf_path = _resolve_python_paths()
    code = (
        "import sys, RNS;"
        f"i=RNS.Identity.from_file({identity_path!r});"
        "d=RNS.Destination(i, RNS.Destination.OUT, RNS.Destination.SINGLE,"
        " 'lxmf', 'propagation');"
        "sys.stdout.write(d.hash.hex())"
    )
    env = os.environ.copy()
    extra = [p for p in (rns_path, lxmf_path) if p and os.path.isdir(p)]
    if extra:
        env["PYTHONPATH"] = os.pathsep.join(
            extra + [env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "could not compute propagation destination hash from "
            f"identity file {identity_path}: {proc.stderr.strip()}"
        )
    out = proc.stdout.strip()
    if len(out) != 32:
        raise RuntimeError(
            f"unexpected hash output from identity-hash helper: {out!r}"
        )
    return out


def _wait_for_path(path: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.05)
    raise TimeoutError(f"file did not appear within {timeout}s: {path}")


def _wait_for_listener(host: str, port: int, timeout: float) -> None:
    """Block until *something* is accepting on (host, port).

    lxmd brings its RNS interfaces up after parsing the config, which
    takes ~100ms on loopback. We poll until the kernel reports the
    port as connectable, so the test's first ``lxmf_add_tcp_client_interface``
    call doesn't race the listener.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            s.connect((host, port))
            return
        except OSError:
            time.sleep(0.1)
        finally:
            s.close()
    raise TimeoutError(
        f"lxmd TCP listener did not come up on {host}:{port} within {timeout}s"
    )


class LxmdPropagationNode:
    """Manages a real `lxmd` subprocess for use as a test propagation node.

    Lifecycle:

      1. ``__init__`` allocates a tempdir, writes RNS + lxmd config,
         and starts the daemon. Blocks until the identity file exists
         and the TCP listener accepts connections.
      2. ``destination_hash`` (lazy property) returns the lxmf
         propagation destination hash that bridges target with
         ``lxmf_set_outbound_propagation_node``.
      3. ``close()`` SIGTERMs lxmd and removes the tempdir.

    The instance is also a context manager so tests can write
    ``with LxmdPropagationNode() as pn: ...``.
    """

    def __init__(
        self,
        announce_interval_minutes: int = 1,
        loglevel: int = 4,
    ) -> None:
        # lxmd's config parser reads `announce_interval` as an int (minutes,
        # multiplied by 60). Tests get the initial announce via
        # `announce_at_start = yes`; the periodic interval is mostly a
        # don't-care since each test runs for < 30s.
        if not isinstance(announce_interval_minutes, int) or announce_interval_minutes < 1:
            raise ValueError(
                "announce_interval_minutes must be a positive int (lxmd "
                "minimum is 1)"
            )
        self._tmpdir = tempfile.mkdtemp(prefix="lxmf_conformance_pn_")
        self._rnsconfig_dir = os.path.join(self._tmpdir, "rnsconfig")
        self._lxmd_dir = os.path.join(self._tmpdir, "lxmd")
        os.makedirs(self._rnsconfig_dir, exist_ok=True)
        os.makedirs(self._lxmd_dir, exist_ok=True)

        self._listen_port = _allocate_free_port()

        # Transport mode is REQUIRED here. lxmd is the central hub of
        # the trio's star topology — sender's announce reaches receiver
        # only if lxmd forwards it. With `enable_transport = no` the
        # sender's `lxmf_send_propagated` call fails immediately with
        # "No identity known for destination ..." because the receiver's
        # delivery announce never made it across the bridge ↔ bridge
        # link. Production deployments also run lxmd with transport on,
        # so this matches what the codebase exercises in real use.
        # Disable AutoInterface (multicast doesn't traverse loopback
        # cleanly in some sandboxes); pin only the TCP server.
        rnsconfig = (
            "[reticulum]\n"
            "  enable_transport = yes\n"
            "  share_instance = no\n"
            "\n"
            f"[logging]\n"
            f"  loglevel = {loglevel}\n"
            "\n"
            "[interfaces]\n"
            "  [[Default Interface]]\n"
            "    type = AutoInterface\n"
            "    enabled = no\n"
            "\n"
            "  [[TCP Server Interface]]\n"
            "    type = TCPServerInterface\n"
            "    enabled = yes\n"
            "    listen_ip = 127.0.0.1\n"
            f"    listen_port = {self._listen_port}\n"
        )
        with open(
            os.path.join(self._rnsconfig_dir, "config"), "w"
        ) as fh:
            fh.write(rnsconfig)

        # Fast announce interval so the bridges' path tables learn
        # about the propagation destination quickly. Default is 6h —
        # untenable for tests that expect the PN to be reachable
        # within seconds. Decimal minutes are honoured by lxmd's config
        # parser (ConfigObj treats it as a float).
        lxmd_config = (
            "[propagation]\n"
            "  enable_node = yes\n"
            f"  announce_interval = {announce_interval_minutes}\n"
            "  announce_at_start = yes\n"
            "  autopeer = no\n"
            "  message_storage_limit = 100\n"
            "  propagation_message_max_accepted_size = 25000\n"
            "  propagation_sync_max_accepted_size = 102400\n"
        )
        with open(os.path.join(self._lxmd_dir, "config"), "w") as fh:
            fh.write(lxmd_config)

        rns_path, lxmf_path = _resolve_python_paths()
        env = os.environ.copy()
        extra = [p for p in (rns_path, lxmf_path) if p and os.path.isdir(p)]
        if extra:
            env["PYTHONPATH"] = os.pathsep.join(
                extra + [env.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep)

        # `-s` makes lxmd log to /root/.lxmd/logfile-style files
        # rather than stdout. We don't want that — it complicates
        # diagnostics when a test fails — so we run lxmd without -s
        # and capture stdout/stderr to files in the tempdir for
        # post-mortem.
        self._stdout_path = os.path.join(self._tmpdir, "lxmd.stdout")
        self._stderr_path = os.path.join(self._tmpdir, "lxmd.stderr")
        self._stdout_fh = open(self._stdout_path, "wb")
        self._stderr_fh = open(self._stderr_path, "wb")
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-u",  # unbuffered so log lines flush as they're produced
                "-m",
                "LXMF.Utilities.lxmd",
                "--rnsconfig",
                self._rnsconfig_dir,
                "--config",
                self._lxmd_dir,
                "--propagation-node",
                "-v",
            ],
            stdin=subprocess.DEVNULL,
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
            env=env,
        )

        # Wait for lxmd to write its identity file (created during
        # router init) and start accepting on the listener port.
        try:
            _wait_for_path(
                os.path.join(self._lxmd_dir, _LXMD_IDENTITY_FILE),
                _LXMD_READY_TIMEOUT_SEC,
            )
            _wait_for_listener(
                "127.0.0.1", self._listen_port, _LXMD_READY_TIMEOUT_SEC
            )
        except Exception:
            # Surface the daemon's stderr so failures aren't silent.
            self.close()
            stderr_excerpt = ""
            if os.path.exists(self._stderr_path):
                with open(self._stderr_path, "rb") as fh:
                    stderr_excerpt = fh.read().decode(
                        "utf-8", errors="replace"
                    )[-2000:]
            raise RuntimeError(
                f"lxmd failed to come up. stderr tail:\n{stderr_excerpt}"
            )

        # Cache the destination hash up-front so consumers can use it
        # immediately without paying the subprocess hash-computation
        # cost on the hot path.
        self._destination_hash = _compute_propagation_destination_hash(
            os.path.join(self._lxmd_dir, _LXMD_IDENTITY_FILE)
        )

    @property
    def listen_port(self) -> int:
        return self._listen_port

    @property
    def destination_hash(self) -> str:
        """Hex-encoded lxmf:propagation destination hash for this lxmd."""
        return self._destination_hash

    @property
    def tempdir(self) -> str:
        return self._tmpdir

    def stderr_tail(self, n_bytes: int = 4000) -> str:
        if not os.path.exists(self._stderr_path):
            return ""
        with open(self._stderr_path, "rb") as fh:
            try:
                fh.seek(-n_bytes, os.SEEK_END)
            except OSError:
                fh.seek(0)
            return fh.read().decode("utf-8", errors="replace")

    def close(self) -> None:
        """Stop lxmd and remove the tempdir.

        Idempotent — safe to call multiple times. Best-effort: if
        lxmd has already exited the TERM signal is a no-op, if the
        tempdir is already gone the rmtree is a no-op.
        """
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_LXMD_TEARDOWN_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        for fh_attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, fh_attr, None)
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        tmpdir = getattr(self, "_tmpdir", None)
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
            self._tmpdir = None

    def __enter__(self) -> "LxmdPropagationNode":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
