"""Bridge client for the LXMF conformance suite.

Each implementation (Python reference, Swift, Kotlin) ships a CLI
executable that speaks JSON-RPC over stdin/stdout. This client
manages the subprocess lifecycle and exposes a clean ``execute()``
API for tests to issue commands.

Wire protocol (one JSON object per line, both directions):

    Request:  {"id": "req-1", "command": "lxmf_init", "params": {...}}
    Response (ok):    {"id": "req-1", "success": true, "result": {...}}
    Response (error): {"id": "req-1", "success": false, "error": "..."}

The bridge MUST emit a single line ``READY`` on stdout once it is
ready to receive commands; ``BridgeClient.__init__`` blocks on that
signal so every test command sees a fully-initialized bridge.

Modeled on reticulum-conformance/bridge_client.py — kept deliberately
in sync so contributors familiar with one suite can navigate the other
without surprises.
"""

import collections
import json
import os
import subprocess
import threading
import time


class BridgeError(Exception):
    """Raised when the bridge subprocess returns ``success: false``
    or exits unexpectedly."""

    def __init__(self, message, command=None):
        super().__init__(message)
        self.command = command


class BridgeClient:
    """Long-running subprocess wrapper that speaks JSON-RPC over stdio."""

    def __init__(self, command, timeout=30, env=None, pass_fds=()):
        """Spawn a bridge subprocess.

        Args:
            command: Either a shell string (``"python3 reference/lxmf_python.py"``)
                or an argv list. Shell strings go through ``shell=True`` for
                convenience; argv lists do not.
            timeout: Seconds to wait for the bridge to print ``READY``.
                30s is generous — bridge startup is sub-second on healthy
                builds; long stalls almost always mean a missing dependency
                or import error in the bridge.
            env: Extra environment variables to overlay on top of
                ``os.environ`` for the subprocess. Used to pin
                ``PYTHON_RNS_PATH`` / ``PYTHON_LXMF_PATH`` from CI checkouts.
            pass_fds: File descriptors to keep open in the child. Required
                for the pipe-pair fixture: each bridge needs the FD of the
                anonymous pipe end it owns. Unused FDs are CLOSED in the
                child by default (``close_fds=True``).
        """
        self.command = command
        self._req_counter = 0

        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        shell = isinstance(command, str)

        # close_fds=True is the Popen default on POSIX, but we set it
        # explicitly so the contract is visible: only ``pass_fds`` survive
        # into the child. This matters for the pipe fixture because
        # leaking unrelated open FDs into the bridge would silently keep
        # the pytest-runner side of pipes alive past test teardown.
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=shell,
            env=proc_env,
            text=True,
            bufsize=1,
            close_fds=True,
            pass_fds=tuple(pass_fds),
        )

        # Drain stderr in a background thread. Without this, verbose
        # stderr output from a bridge (e.g. RNS.log() at LOG_DEBUG, or
        # any println-equivalent in the kotlin/swift bridges) fills the
        # OS pipe buffer (~64 KB) mid-test and the bridge subprocess
        # BLOCKS on its next stderr write. That stall manifests as
        # tests that hang past the per-test timeout, with the bridge
        # appearing to have "stopped responding" — but it's actually
        # the harness that's wedged. Same root cause as the test-infra
        # deadlock fixed in reticulum-conformance#22; ported here.
        # Tail of recent stderr is preserved for crash diagnostics.
        self._stderr_tail = collections.deque(maxlen=200)
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()

        # Wait for READY. Anything emitted before READY is treated as a
        # warning and skipped — RNS prints "Reticulum...starting" on
        # stdout before the bridge handler installs.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                if self._proc.poll() is not None:
                    raise BridgeError(
                        f"Bridge process exited before READY (code "
                        f"{self._proc.returncode}). Stderr:\n"
                        f"{self._stderr_snapshot()}"
                    )
                continue
            if line.strip() == "READY":
                return
        raise BridgeError(f"Bridge did not send READY within {timeout}s")

    def _drain_stderr(self):
        """Background drainer for the bridge's stderr pipe.

        Reads line-by-line and stashes the last N lines for diagnostic
        use. Critical for any bridge that logs verbosely on stderr —
        without it the kernel pipe buffer fills and the bridge blocks
        mid-operation. Same pattern as the equivalent fix in
        reticulum-conformance/bridge_client.py.
        """
        try:
            for line in iter(self._proc.stderr.readline, ""):
                self._stderr_tail.append(line)
        except Exception:
            pass

    def _stderr_snapshot(self):
        """Return the last few hundred lines of bridge stderr for error
        context. The drain thread keeps this current in real time."""
        return "".join(self._stderr_tail)

    def execute(self, command, **params):
        """Send a command and block until the response arrives.

        Args:
            command: Command name (e.g. ``"lxmf_init"``).
            **params: Parameters for the command. Hex-encode any byte
                values before passing.

        Returns:
            ``result`` dict from the bridge response (may be ``{}``).

        Raises:
            BridgeError: If the bridge returns ``success: false`` or
                closes stdout unexpectedly.
        """
        self._req_counter += 1
        req_id = f"req-{self._req_counter}"

        request = {"id": req_id, "command": command, "params": params}
        line = json.dumps(request) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

        # The bridge is allowed to emit non-JSON log lines (RNS prints
        # warnings during identity load, the swift bridge prints
        # debug). Skip anything that doesn't look like a JSON object.
        while True:
            response_line = self._proc.stdout.readline()
            if not response_line:
                raise BridgeError(
                    f"Bridge closed stdout (stderr: {self._stderr_snapshot()})",
                    command=command,
                )
            if response_line.strip().startswith("{"):
                break

        response = json.loads(response_line)
        if not response.get("success"):
            raise BridgeError(
                response.get("error", "Unknown bridge error"), command=command
            )
        return response.get("result", {})

    def close(self):
        """Best-effort termination. Idempotent — safe to call from
        multiple cleanup paths."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        # Drain any final stderr lines into _stderr_tail before callers
        # (typically test teardown catching a BridgeError) inspect the
        # snapshot. The thread exits naturally as soon as stderr hits
        # EOF post-wait; bound the join so a misbehaving bridge can't
        # wedge teardown.
        if hasattr(self, "_stderr_thread"):
            self._stderr_thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()
