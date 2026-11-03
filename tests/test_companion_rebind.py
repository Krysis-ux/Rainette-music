"""Rebinding the companion port after a restart, which the tunnel depends on."""

from __future__ import annotations

import os
import socket
import threading
import unittest

import server


class CompanionRebindTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "TIME_WAIT rebinding is a POSIX problem")
    def test_the_port_rebinds_while_a_closed_connection_lingers(self):
        """The exact shape of quit-and-relaunch with a phone paired."""
        # Port 0 lets the kernel choose, and the socket holds it from that moment.
        listener = server._bind_companion_socket("127.0.0.1", 0)
        port = listener.getsockname()[1]
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
        """SO_REUSEADDR must not weaken the pinned-port guarantee."""
        held = server._bind_companion_socket("127.0.0.1", 0)
        port = held.getsockname()[1]
        held.listen(8)
        try:
            with self.assertRaises(OSError):
                server._bind_companion_socket("127.0.0.1", port)
        finally:
            held.close()


if __name__ == "__main__":
    unittest.main()
