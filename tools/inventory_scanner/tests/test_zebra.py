import os
import queue
import time
import unittest
from unittest.mock import Mock

from tools.inventory_scanner.zebra import (
    ACK, BEEP, DECODE, NAK, SCAN_ENABLE, SCANNER_EVENT, Decoder, Event, ScannerError, Session,
    ZebraScanner, packet,
)


class Port:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        return len(data)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.now = 0.0
        self.session = Session(self.events.append, timeout=2, clock=lambda: self.now)
        self.port = Port()
        self.session.command(self.port, "connect")
        self.session.receive(self.port, packet(ACK, source=0))

    def decode(self, content=b"123456", status=0, kind=0x1B):
        self.session.receive(self.port, packet(DECODE, bytes((kind,)) + content, source=0, status=status))

    def test_documented_beep_packets_checksum_and_source(self):
        self.assertEqual(packet(BEEP, b"\x00").hex(" "), "05 e6 04 00 00 ff 11")
        self.assertEqual(packet(BEEP, b"\x01").hex(" "), "05 e6 04 00 01 ff 10")
        self.assertEqual(packet(BEEP, b"\x07").hex(" "), "05 e6 04 00 07 ff 0a")
        self.assertEqual(self.port.writes[0], packet(SCAN_ENABLE))
        self.assertEqual(self.events, [Event("ready", "Zebra SSI connected; command acknowledgement verified.")])

    def test_feedback_waits_for_ack_and_never_acks_ack(self):
        self.session.command(self.port, "success")
        with self.assertRaises(ScannerError):
            self.session.command(self.port, "error")
        count = len(self.port.writes)
        self.session.receive(self.port, packet(ACK, source=0))
        self.assertIsNone(self.session.pending)
        self.assertEqual(len(self.port.writes), count)
        self.session.command(self.port, "error")
        self.assertEqual(self.port.writes[-1], packet(BEEP, b"\x07"))

    def test_fragmented_reads_coalesced_frames_and_transport_ack(self):
        one = packet(DECODE, b"\x1bfirst", source=0)
        two = packet(DECODE, b"\x1bsecond", source=0)
        self.session.receive(self.port, one[:2])
        self.assertEqual(len(self.events), 1)
        self.session.receive(self.port, one[2:] + two)
        self.assertEqual(self.events[-2:], [Event("scan", "first"), Event("scan", "second")])
        self.assertEqual(self.port.writes[-2:], [packet(ACK), packet(ACK)])

    def test_multiframe_utf8_and_retransmit_deduplicated(self):
        content = ("ci1|" + "ä" * 200).encode()
        self.decode(content[:249], status=2)
        self.decode(content[:249], status=3)
        self.assertEqual(len(self.events), 1)
        self.decode(content[249:])
        self.decode(content[249:], status=1)
        self.assertEqual(self.events[-1], Event("scan", content.decode()))
        self.assertEqual(len(self.events), 2)

    def test_real_repeated_scan_not_suppressed(self):
        self.decode()
        self.decode()
        self.assertEqual(len([event for event in self.events if event.kind == "scan"]), 2)

    def test_unknown_retransmit_never_delivered(self):
        with self.assertRaises(ScannerError):
            self.decode(b"unknown", status=1)
        self.assertEqual(self.port.writes[-1], packet(NAK, b"\x0a"))
        self.assertEqual(len(self.events), 1)

    def test_bad_checksum_invalid_length_and_wrong_source_fail(self):
        for raw in (b"\x01", packet(DECODE, b"\x1bcode", source=4),
                    packet(DECODE, b"\x1bcode", source=0)[:-1] + b"\x00"):
            with self.subTest(raw=raw), self.assertRaises(ScannerError):
                Decoder().feed(raw, 0)

    def test_invalid_text_type_and_oversize(self):
        for raw in (b"\x99structured", b"\x1b\xff", b"\x1b", b"\x1bcontains\x00nul"):
            session = Session(lambda _: None)
            session.ready = True
            with self.subTest(raw=raw), self.assertRaises(ScannerError):
                session.receive(self.port, packet(DECODE, raw, source=0))
        self.session.parts.extend(b"x" * 2048)
        with self.assertRaises(ScannerError):
            self.decode(b"x")

    def test_exact_maximum_payload_and_overlimit_packet(self):
        data = b"x" * 2048
        for offset in range(0, len(data), 250):
            part = data[offset:offset + 250]
            self.decode(part, status=2 if offset + len(part) < len(data) else 0)
        self.assertEqual(self.events[-1], Event("scan", data.decode()))
        with self.assertRaises(ScannerError):
            packet(BEEP, b"x" * 252)

    def test_command_timeout_does_not_replay(self):
        self.session.command(self.port, "success")
        before = list(self.port.writes)
        self.now = 2
        with self.assertRaisesRegex(ScannerError, "not replayed"):
            self.session.check_timeout()
        self.assertEqual(self.port.writes, before)

    def test_packet_and_barcode_timeouts(self):
        self.session.receive(self.port, b"\x05\xf3")
        self.now = 2
        with self.assertRaisesRegex(ScannerError, "Incomplete SSI packet"):
            self.session.check_timeout()
        self.session.decoder = Decoder()
        self.now = 3
        self.decode(b"partial", status=2)
        self.now = 5
        with self.assertRaisesRegex(ScannerError, "fragmented"):
            self.session.check_timeout()

    def test_nak_is_explicit_not_retried_and_not_acknowledged(self):
        self.session.command(self.port, "error")
        before = list(self.port.writes)
        with self.assertRaisesRegex(ScannerError, "cause 6"):
            self.session.receive(self.port, packet(NAK, b"\x06", source=0))
        self.assertEqual(self.port.writes, before)

    def test_decode_during_beep_transaction_is_acked_and_delivered(self):
        self.session.command(self.port, "recognized")
        self.decode(b"destination")
        self.assertEqual(self.events[-1], Event("scan", "destination"))
        self.assertEqual(self.session.pending, "recognized")
        self.session.receive(self.port, packet(ACK, source=0))
        self.assertIsNone(self.session.pending)

    def test_emit_failure_does_not_ack_unaccepted_scan(self):
        self.session.emit = Mock(side_effect=ScannerError("Queue full"))
        before = list(self.port.writes)
        with self.assertRaisesRegex(ScannerError, "Queue full"):
            self.decode()
        self.assertEqual(self.port.writes, before)

    def test_partial_serial_write_and_unknown_opcode_rejected(self):
        self.port.write = Mock(return_value=1)
        with self.assertRaises(ScannerError):
            self.session.command(self.port, "success")
        self.port = Port()
        with self.assertRaisesRegex(ScannerError, "Unsupported"):
            self.session.receive(self.port, packet(0xFE, source=0))
        self.assertEqual(self.port.writes[-1], packet(NAK, b"\x02"))

    def test_bounded_queues(self):
        scanner = ZebraScanner("/dev/unused")
        for _ in range(32):
            scanner.emit(Event("scan", "data"))
        with self.assertRaises(ScannerError):
            scanner.emit(Event("scan", "overflow"))
        for _ in range(16):
            scanner.feedback("success")
        with self.assertRaises(ScannerError):
            scanner.feedback("error")
        self.assertTrue(scanner.stop.is_set())
        self.assertIsNotNone(scanner.failure)

    def test_status_event_acknowledged_but_not_treated_as_scan(self):
        self.session.receive(self.port, packet(SCANNER_EVENT, b"\x02", source=0))
        self.assertEqual(self.port.writes[-1], packet(ACK))
        self.assertEqual(self.events[-1].kind, "status")
        with self.assertRaisesRegex(ScannerError, "rebooted"):
            self.session.receive(self.port, packet(SCANNER_EVENT, b"\x03", source=0))

    def test_symbology_change_in_fragmented_barcode_is_rejected(self):
        self.decode(b"start", status=2, kind=0x1B)
        with self.assertRaisesRegex(ScannerError, "type changed"):
            self.decode(b"end", kind=0x1C)
        self.assertEqual(len(self.events), 1)

    def test_unexpected_ack_cannot_verify_a_future_command(self):
        with self.assertRaisesRegex(ScannerError, "Unexpected SSI acknowledgement"):
            self.session.receive(self.port, packet(ACK, source=0))


@unittest.skipUnless(os.name == "posix", "Serial integration uses a Unix pseudo-terminal")
class SerialIntegrationTests(unittest.TestCase):
    def setUp(self):
        import pty
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.scanner = ZebraScanner(os.ttyname(self.slave), timeout=0.3)
        self.scanner.start()
        self.addCleanup(self.scanner.close)

    def read_packet(self):
        import select
        data = bytearray()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                data.extend(os.read(self.master, 257))
                if data and len(data) >= data[0] + 2:
                    return bytes(data)
        self.fail(f"SSI write did not arrive: {data!r}; {self.scanner.failure}")

    def event(self):
        return self.scanner.events.get(timeout=3)

    def test_real_serial_thread_decode_feedback_and_shutdown(self):
        self.assertEqual(self.read_packet(), packet(SCAN_ENABLE))
        os.write(self.master, packet(ACK, source=0))
        self.assertEqual(self.event().kind, "ready")
        content = "ci1||||Group A|Alice|".encode()
        os.write(self.master, packet(DECODE, b"\x1b" + content, source=0))
        self.assertEqual(self.read_packet(), packet(ACK))
        self.assertEqual(self.event(), Event("scan", content.decode()))
        self.scanner.feedback("success")
        self.assertEqual(self.read_packet(), packet(BEEP, b"\x01"))
        os.write(self.master, packet(ACK, source=0))
        self.scanner.feedback("error")
        self.assertEqual(self.read_packet(), packet(BEEP, b"\x07"))
        os.write(self.master, packet(ACK, source=0))
        self.scanner.close()
        self.assertFalse(self.scanner.thread.is_alive())
        self.assertIsNone(self.scanner.failure)

    def test_missing_handshake_is_visible_and_no_automatic_replay(self):
        self.assertEqual(self.read_packet(), packet(SCAN_ENABLE))
        self.scanner.thread.join(2)
        self.assertIn("No SSI acknowledgement", self.scanner.failure)
        self.assertFalse(self.scanner.thread.is_alive())
        with self.assertRaises(ScannerError):
            self.scanner.feedback("success")

    def test_command_rejection_stops_worker_with_explicit_failure(self):
        self.assertEqual(self.read_packet(), packet(SCAN_ENABLE))
        os.write(self.master, packet(ACK, source=0))
        self.assertEqual(self.event().kind, "ready")
        self.scanner.feedback("success")
        self.assertEqual(self.read_packet(), packet(BEEP, b"\x01"))
        os.write(self.master, packet(NAK, b"\x06", source=0))
        self.scanner.thread.join(2)
        self.assertFalse(self.scanner.thread.is_alive())
        self.assertIn("rejected success command", self.scanner.failure)

    def test_device_read_failure_stops_worker(self):
        from unittest.mock import patch
        import serial
        scanner = ZebraScanner("/dev/test", timeout=0.3)
        port = Mock()
        port.write.side_effect = lambda data: len(data)
        port.read.side_effect = serial.SerialException("Device disconnected")
        with patch("serial.Serial", return_value=port):
            scanner.start()
            scanner.thread.join(2)
        self.assertIn("Device disconnected", scanner.failure)
        port.close.assert_called_once()
        scanner.close()

    def test_missing_serial_device_reports_failure(self):
        scanner = ZebraScanner("/this/device/does/not/exist", timeout=0.3)
        scanner.start()
        scanner.thread.join(2)
        self.assertIsNotNone(scanner.failure)
        self.assertFalse(scanner.thread.is_alive())
        scanner.close()


if __name__ == "__main__":
    unittest.main()
