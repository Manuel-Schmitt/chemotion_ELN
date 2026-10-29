"""Bounded, off-thread SSI transport for a DS2278 in SSI-over-USB-CDC mode."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .workflow import MAX_CODE_BYTES

ACK, NAK, DECODE, BEEP, SCAN_ENABLE, SCANNER_EVENT = 0xD0, 0xD1, 0xF3, 0xE6, 0xE9, 0xF6
SOUNDS = {"recognized": 0x00, "success": 0x01, "error": 0x07}


class ScannerError(ValueError):
    """A scanner configuration, transport, or SSI protocol failure."""


def packet(opcode: int, data: bytes = b"", *, source: int = 4, status: int = 0) -> bytes:
    if len(data) > 251:
        raise ScannerError("SSI packet payload exceeds 251 bytes.")
    body = bytes((len(data) + 4, opcode, source, status)) + data
    return body + ((-sum(body)) & 0xFFFF).to_bytes(2, "big")


@dataclass(frozen=True)
class Frame:
    opcode: int
    status: int
    data: bytes


@dataclass(frozen=True)
class Event:
    kind: str
    message: str
    timestamp: float = field(default_factory=time.monotonic, compare=False)


class SerialPort(Protocol):
    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


class Decoder:
    """Incremental framing; corrupt streams are rejected rather than guessed."""

    def __init__(self):
        self.buffer = bytearray()
        self.started: float | None = None

    def feed(self, data: bytes, now: float) -> list[Frame]:
        if data and not self.buffer:
            self.started = now
        self.buffer.extend(data)
        result = []
        while self.buffer:
            length = self.buffer[0]
            if length < 4:
                raise ScannerError("Invalid SSI length. Select SSI over USB CDC, not keyboard/plain CDC.")
            if len(self.buffer) < length + 2:
                break
            raw = bytes(self.buffer[:length + 2])
            del self.buffer[:length + 2]
            if ((sum(raw[:-2]) + int.from_bytes(raw[-2:], "big")) & 0xFFFF) != 0:
                raise ScannerError("SSI checksum failed; connection stopped to avoid accepting corrupt scans.")
            if raw[2] != 0 or raw[3] & ~0x03:
                raise ScannerError("Unexpected SSI source/status. Check scanner interface and configuration.")
            result.append(Frame(raw[1], raw[3], raw[4:-2]))
            self.started = now if self.buffer else None
        return result


class Session:
    """One serial owner; barcode ACKs are transport receipt, never save success."""

    def __init__(self, emit: Callable[[Event], None], timeout: float = 2,
                 clock: Callable[[], float] = time.monotonic):
        self.emit, self.timeout, self.clock = emit, timeout, clock
        self.decoder = Decoder()
        self.pending: str | None = None
        self.sent_at = 0.0
        self.ready = False
        self.parts = bytearray()
        self.symbology: int | None = None
        self.last_fragment: tuple[int, bytes] | None = None
        self.fragment_at: float | None = None

    @staticmethod
    def send(port: SerialPort, data: bytes) -> None:
        if port.write(data) != len(data):
            raise ScannerError("Incomplete SSI serial write. Reconnect before continuing.")

    def command(self, port: SerialPort, name: str) -> None:
        if self.pending is not None:
            raise ScannerError("An SSI command is already awaiting acknowledgement.")
        opcode, data = (SCAN_ENABLE, b"") if name == "connect" else (BEEP, bytes((SOUNDS[name],)))
        self.send(port, packet(opcode, data))
        self.pending, self.sent_at = name, self.clock()

    def receive(self, port: SerialPort, data: bytes) -> None:
        for frame in self.decoder.feed(data, self.clock()):
            if frame.opcode == ACK:
                if frame.data or frame.status & 2 or self.pending is None:
                    raise ScannerError("Unexpected SSI acknowledgement; reconnect to resynchronize.")
                name, self.pending = self.pending, None
                if name == "connect":
                    self.ready = True
                    self.emit(Event("ready", "Zebra SSI connected; command acknowledgement verified."))
            elif frame.opcode == NAK:
                if len(frame.data) != 1 or self.pending is None:
                    raise ScannerError("Unexpected SSI rejection.")
                raise ScannerError(f"Scanner rejected {self.pending} command (SSI cause {frame.data[0]}).")
            elif frame.opcode == DECODE:
                self.decode(port, frame)
            elif frame.opcode == SCANNER_EVENT:
                if len(frame.data) != 1 or frame.status & 2:
                    raise ScannerError("Malformed SSI scanner event.")
                self.send(port, packet(ACK))
                if frame.data[0] in (0x03, 0x0A):
                    raise ScannerError("Scanner rebooted or reset parameters. Verify configuration and reconnect.")
                self.emit(Event("status", f"SSI scanner event 0x{frame.data[0]:02X}."))
            else:
                self.send(port, packet(NAK, b"\x02"))
                raise ScannerError(f"Unsupported SSI message 0x{frame.opcode:02X}; no barcode accepted.")

    def decode(self, port: SerialPort, frame: Frame) -> None:
        if not self.ready:
            self.send(port, packet(NAK, b"\x0a"))
            raise ScannerError("Barcode arrived before the scanner connection was verified. Reconnect and rescan.")
        fragment = (frame.status & 2, frame.data)
        if frame.status & 1:
            if self.last_fragment == fragment:
                self.send(port, packet(ACK))
                return
            self.send(port, packet(NAK, b"\x0a"))
            raise ScannerError("Unknown retransmitted barcode fragment. Rescan after reconnecting.")
        if not frame.data or frame.data[0] == 0x99:
            self.send(port, packet(NAK, b"\x0a"))
            raise ScannerError("Unsupported SSI structured barcode data.")
        code_type, content = frame.data[0], frame.data[1:]
        if self.symbology is not None and self.symbology != code_type:
            raise ScannerError("SSI barcode type changed during a fragmented scan.")
        if len(self.parts) + len(content) > MAX_CODE_BYTES:
            self.send(port, packet(NAK, b"\x0a"))
            raise ScannerError("Scanned SSI barcode exceeds the application input limit.")
        self.symbology = code_type
        self.parts.extend(content)
        self.fragment_at = self.clock()
        if not frame.status & 2:
            try:
                text = self.parts.decode("utf-8")
            except UnicodeDecodeError as exc:
                self.send(port, packet(NAK, b"\x0a"))
                raise ScannerError("Barcode is not UTF-8. Check the scanner character encoding.") from exc
            if not text or "\x00" in text:
                self.send(port, packet(NAK, b"\x0a"))
                raise ScannerError("Barcode is empty or contains NUL bytes.")
            # Emit into the bounded application queue before acknowledging receipt.
            self.emit(Event("scan", text))
            self.parts.clear()
            self.symbology, self.fragment_at = None, None
        self.last_fragment = fragment
        self.send(port, packet(ACK))

    def check_timeout(self) -> None:
        now = self.clock()
        if self.pending is not None and now - self.sent_at >= self.timeout:
            raise ScannerError(
                f"No SSI acknowledgement for {self.pending}. Outcome unknown; command was not replayed."
            )
        if self.decoder.started is not None and now - self.decoder.started >= self.timeout:
            raise ScannerError("Incomplete SSI packet timed out. Reconnect and rescan.")
        if self.fragment_at is not None and now - self.fragment_at >= self.timeout:
            raise ScannerError("Incomplete fragmented barcode timed out. Reconnect and rescan.")


class ZebraScanner:
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 2):
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self.events: queue.Queue[Event] = queue.Queue(maxsize=32)
        self.commands: queue.Queue[str] = queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.failure: str | None = None

    def start(self) -> None:
        if self.thread is not None:
            raise ScannerError("Scanner backend already started.")
        self.thread = threading.Thread(target=self.run, name="zebra-ssi", daemon=True)
        self.thread.start()

    def emit(self, event: Event) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full as exc:
            raise ScannerError("Scanner input queue is full. Scans were not safely accepted; reconnect and rescan.") from exc

    def feedback(self, sound: str) -> None:
        if sound not in SOUNDS:
            raise ScannerError("Unknown scanner feedback sound.")
        if self.stop.is_set() or self.failure is not None:
            raise ScannerError("Scanner disconnected; sound could not be sent.")
        try:
            self.commands.put_nowait(sound)
        except queue.Full as exc:
            self.stop.set()
            self.failure = "Scanner feedback queue overflow. Reconnect before scanning again."
            raise ScannerError(self.failure) from exc

    def run(self) -> None:
        port = None
        try:
            import serial
        except ImportError:
            self.failure = "Zebra SSI requires pyserial. Install the station requirements."
            return
        try:
            port = serial.Serial(self.port, baudrate=self.baudrate, timeout=0.05,
                                 write_timeout=self.timeout, exclusive=True)
            session = Session(self.emit, self.timeout)
            session.command(port, "connect")
            while not self.stop.is_set():
                data = port.read(257)
                if data:
                    session.receive(port, data)
                session.check_timeout()
                if session.ready and session.pending is None and not session.parts and not session.decoder.buffer:
                    try:
                        sound = self.commands.get_nowait()
                    except queue.Empty:
                        continue
                    session.command(port, sound)
        except (serial.SerialException, OSError, ScannerError, ValueError) as exc:
            self.failure = f"Zebra SSI stopped: {exc}"
        finally:
            if port is not None:
                try:
                    port.close()
                except (serial.SerialException, OSError) as exc:
                    self.failure = f"Zebra serial close failed: {exc}"

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=self.timeout + 0.5)
            if self.thread.is_alive():
                raise ScannerError("Scanner thread did not stop. Close the process before reconnecting.")
