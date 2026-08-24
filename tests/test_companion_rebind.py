"""Rebinding the companion port after a restart, which the tunnel depends on.

A paired phone pins the companion port: `_companion_port_candidates` returns
that one port and no alternative, on purpose, because the phone's stored
endpoint contains it. The phone also long-polls `/events` continuously, so when
the app quits there is always a just-closed connection on that port sitting in
TIME_WAIT.

Without SO_REUSEADDR that is enough to make the next launch fail to bind with
"Address already in use" -- while nothing is listening. `start_paired_companion`
then raises, `main()` skips `_restore_tunnel()`, and the computer comes up
unreachable and stays that way for about a minute.

Found in a real user's log:

    OSError: [Errno 48] Address already in use
    RuntimeError: no companion port is available
    paired companion listener failed to restart
"""

from __future__ import annotations

import os
import socket
import threading
import unittest

import server


class CompanionRebindTests(unittest.TestCase):
    def _free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    @unittest.skipIf(os.name == "nt", "TIME_WAIT rebinding is a POSIX problem")
    def test_the_port_rebinds_while_a_closed_connection_lingers(self):
        """The exact shape of quit-and-relaunch with a phone paired."""
        port = self._free_port()

        # A listener, and a client that talked to it -- the long-poll.
        listener = server._bind_companion_socket("127.0.0.1", port)
        listener.listen(8)
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        accepted, _ = listener.accept()
        client.sendall(b"GET /events HTTP/1.1\r\n\r\n")

        # The app quits: the connection and the listener go away together, and
        # the closed connection is left in TIME_WAIT.
        client.close()
        accepted.close()
        listener.close()

        # The app relaunches, and must have this exact port or nothing.
        try:
            again = server._bind_companion_socket("127.0.0.1", port)
        except OSError as exc:
            self.fail(
                f"the companion port could not be rebound after a restart: {exc}. "
                f"A paired phone pins this port and there is no fallback, so the "
                f"listener never starts and the tunnel is never restored."
            )
        again.close()

    @unittest.skipIf(os.name == "nt", "SO_EXCLUSIVEADDRUSE is the Windows path")
    def test_a_genuine_conflict_still_fails(self):
        """SO_REUSEADDR must not weaken the pinned-port guarantee.

        The fail-closed policy exists so a busy port is reported rather than
        silently moved somewhere the phone cannot find. A *live* listener must
        still be a conflict; only the TIME_WAIT ghost is forgiven.
        """
        port = self._free_port()
        held = server._bind_companion_socket("127.0.0.1", port)
        held.listen(8)
        try:
            with self.assertRaises(OSError):
                server._bind_companion_socket("127.0.0.1", port)
        finally:
            held.close()


if __name__ == "__main__":
    unittest.main()
