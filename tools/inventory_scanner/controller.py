"""Display-independent scanner state machine and serialized worker handoff."""

from __future__ import annotations

import logging
import queue
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from enum import Enum
from typing import Callable

from .api import ApiError, AuthenticationError, Inventory, UnverifiedWrite
from .audit import Audit
from .workflow import (
    MAX_CODE_BYTES, Conflict, FoundConfirmation, Pending, Service, WorkflowError, parse_target,
)

logger = logging.getLogger(__name__)


class Mode(str, Enum):
    ASSIGN = "Assign"
    LOOKUP = "Lookup"
    AUDIT = "Audit"

    def __str__(self) -> str:
        return self.value


class State(str, Enum):
    BLOCKED = "blocked"
    STARTUP = "startup"
    IDLE = "idle"
    LOADING = "loading"
    DESTINATION = "destination"
    SAVING = "saving"
    AUDIT_STARTING = "audit_starting"
    AUDIT_READY = "audit_ready"
    AUDIT_SAVING = "audit_saving"
    FINISHING = "finishing"


class MessageLevel(str, Enum):
    INSTRUCTION = "instruction"
    WORKING = "working"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


BUSY = {State.STARTUP, State.LOADING, State.SAVING, State.AUDIT_STARTING,
        State.AUDIT_SAVING, State.FINISHING}
WRITING = {State.SAVING, State.AUDIT_SAVING, State.FINISHING}


class Controller:
    def __init__(self, service: Service, audit: Audit, changed: Callable[[], None],
                 confirm_found: Callable[[str], None], executor=None, clock=time.monotonic,
                 feedback: Callable[[str], None] | None = None, scanner_id: str = "default"):
        self.service = service
        self.audit = audit
        self.changed = changed
        self.confirm_found = confirm_found
        self.feedback = feedback
        self.scanner_id = scanner_id
        self.owns_executor = executor is None
        self.executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="inventory")
        self.inflight: Future | None = None
        self.clock = clock
        self.results: queue.Queue[tuple[int, Future, Callable, State]] = queue.Queue()
        self.generation = 0
        self.mode = Mode.ASSIGN
        self.state = State.BLOCKED
        self.message = "Connect to chemotion to begin."
        self.details = ""
        self.level = MessageLevel.INSTRUCTION
        self.last_error = ""
        self.pending: Pending | None = None
        self.awaiting_found: str | None = None
        self.target_code: str | None = None
        self.buffer: str | None = None
        self.loading_code: str | None = None
        self.deadline: float | None = None
        self.account_name = ""
        self.closed = False

    @property
    def success(self) -> bool:
        return self.level == MessageLevel.SUCCESS

    @property
    def busy(self) -> bool:
        return self.state in BUSY or self.inflight is not None

    @property
    def can_scan(self) -> bool:
        return not self.closed and self.state != State.BLOCKED and (
            not self.busy or self.state == State.LOADING and self.mode == Mode.ASSIGN)

    @property
    def can_reset(self) -> bool:
        return not self.closed and self.state not in WRITING | {State.STARTUP, State.AUDIT_STARTING}

    @property
    def can_change_mode(self) -> bool:
        return not self.closed and not self.busy and self.state != State.BLOCKED and not self.audit.belongs_to(
            self.scanner_id)

    @property
    def can_finish_audit(self) -> bool:
        return not self.closed and not self.busy and self.state == State.AUDIT_READY and self.audit.belongs_to(
            self.scanner_id)

    @property
    def can_reconnect(self) -> bool:
        return not self.closed and not self.busy and self.state == State.BLOCKED

    @property
    def can_connect_scanner(self) -> bool:
        return not self.closed and self.state not in BUSY - {State.STARTUP}

    def notify(self, message: str, success: bool = False, *, sound: str | None = None,
               level: MessageLevel | None = None) -> None:
        self.message = message
        self.level = level or (MessageLevel.SUCCESS if success else MessageLevel.ERROR if sound == "error"
                               else MessageLevel.WORKING if self.state in BUSY else MessageLevel.INSTRUCTION)
        if self.level == MessageLevel.ERROR:
            self.last_error = message
        self.changed()
        if sound is not None and self.feedback is not None:
            self.feedback(sound)

    def submit(self, task: Callable, done: Callable, fallback: State) -> None:
        token = self.generation
        try:
            future = self.executor.submit(task)
        except Exception as exc:
            self.unexpected_error(exc, completed=True)
            return
        self.inflight = future
        future.add_done_callback(lambda result: self.results.put((token, result, done, fallback)))

    def discard_pair(self) -> None:
        self.pending, self.buffer, self.loading_code, self.deadline = None, None, None, None
        self.awaiting_found = self.target_code = None

    def scan_ready(self) -> None:
        self.discard_pair()
        self.state = State.AUDIT_READY if self.audit.belongs_to(self.scanner_id) else (
            State.IDLE if self.account_name else State.BLOCKED)

    def unexpected_error(self, exc: Exception, *, completed: bool = False,
                         operation: State | None = None) -> None:
        logger.error("Unexpected inventory error on scanner %s", self.scanner_id,
                     exc_info=(type(exc), exc, exc.__traceback__))
        operation = operation or self.state
        writing = operation in WRITING
        if completed or self.inflight is None:
            if self.mode == Mode.AUDIT and operation == State.LOADING and self.loading_code:
                self.audit.failed_scan(self.loading_code, self.scanner_id)
            self.scan_ready()
        message = f"Unexpected error: {exc}."
        if writing:
            message += " A write may have reached chemotion; inspect its current inventory."
        message += (" Wait for the in-flight operation." if self.inflight is not None else
                    " Scan again to start a fresh attempt." if self.state != State.BLOCKED else
                    " Reconnect to authenticate.")
        self.notify(message, sound="error")

    def poll(self) -> None:
        while not self.closed:
            try:
                token, future, done, fallback = self.results.get_nowait()
            except queue.Empty:
                break
            if self.inflight is future:
                self.inflight = None
            if token != self.generation:
                continue
            operation = self.state
            try:
                try:
                    result = future.result()
                except (ApiError, WorkflowError, OSError) as exc:
                    if self.mode == Mode.AUDIT and operation == State.LOADING and self.loading_code:
                        self.audit.failed_scan(self.loading_code, self.scanner_id)
                    self.state = fallback
                    self.buffer = None
                    if isinstance(exc, AuthenticationError):
                        self.state = State.BLOCKED
                        self.deadline = None
                    elif isinstance(exc, UnverifiedWrite):
                        self.scan_ready()
                        self.notify(f"{exc}. A write may have reached chemotion; inspect it. "
                                    "Scan a bottle again for a fresh attempt.", sound="error")
                        continue
                    elif isinstance(exc, Conflict) and self.pending:
                        self.pending = replace(self.pending, inventory=exc.latest)
                        self.describe(self.pending)
                    elif isinstance(exc, FoundConfirmation):
                        if self.target_code is None:
                            raise WorkflowError("Recovery confirmation has no scanned destination.") from exc
                        self.awaiting_found = self.target_code
                        self.notify(str(exc), level=MessageLevel.WARNING)
                        self.confirm_found(self.target_code)
                        continue
                    if self.state != State.DESTINATION:
                        self.loading_code = None
                    self.notify(str(exc), sound="error")
                else:
                    done(result)
            except Exception as exc:
                self.unexpected_error(exc, completed=self.inflight is None, operation=operation)

    def connect(self) -> None:
        if self.busy or self.closed:
            return
        self.state = State.STARTUP
        self.notify("Checking station API token...")

        def connected(user):
            self.account_name = f"Account {user['id']}"
            self.state = State.AUDIT_READY if self.audit.belongs_to(self.scanner_id) else State.IDLE
            self.notify(f"Connected as {self.account_name}. " + self.instruction(), True)

        self.submit(self.service.client.authenticate, connected, State.BLOCKED)

    def instruction(self) -> str:
        if self.mode == Mode.AUDIT:
            if self.audit.belongs_to(self.scanner_id):
                return "Scan bottles in the selected location."
            if self.audit.active:
                return f"An audit is active on scanner {self.audit.owner_scanner_id}."
            return "Scan a location to start its audit."
        return "Scan a bottle code." if self.mode == Mode.ASSIGN else "Lookup only: scan a bottle code."

    def set_mode(self, mode: str) -> None:
        try:
            selected = Mode(mode)
        except ValueError:
            raise WorkflowError("Unknown station mode.") from None
        if not self.can_change_mode:
            self.notify("Finish/cancel the current operation before changing mode.")
            return
        self.reset()
        self.mode = selected
        self.notify(self.instruction())

    def describe(self, pending: Pending) -> None:
        first = pending.inventory.first
        location = " / ".join(str(first.get(key) or "") for key in (
            "current_building", "current_room", "current_cabinet"))
        prefix = "Expected / last recorded" if first.get("status") == "Missing" else "Current"
        self.details = (
            f"{pending.label}\n{pending.inventory.ref.key} | Status: {first.get('status', '(unset)')}\n"
            f"{prefix}: {location}\nGroup: {first.get('current_group') or '(none)'}\n"
            f"Borrower: {first.get('borrowed_by') or '(none)'}"
        )

    def scan(self, code: str) -> None:
        try:
            valid = bool(code) and len(code.encode("utf-8")) <= MAX_CODE_BYTES
        except UnicodeEncodeError:
            valid = False
        if not valid:
            self.notify(f"Scan a code of 1 to {MAX_CODE_BYTES} UTF-8 bytes.", sound="error")
            return
        if self.closed:
            self.notify("This scanner session is closed.", sound="error")
            return
        if self.state == State.LOADING and self.mode == Mode.ASSIGN:
            if code == self.loading_code:
                self.notify("Duplicate bottle scan ignored; loading...")
                return
            try:
                parse_target(code)
            except WorkflowError as exc:
                self.notify(str(exc), sound="error")
                return
            if self.buffer is not None:
                self.notify("One destination is already buffered. Wait for the save result.", sound="error")
                return
            self.buffer = code
            self.notify("Destination recognized and buffered; not saved yet.")
            return
        if self.busy:
            self.notify("Busy. This scan was not accepted; wait and scan again.", sound="error")
            return
        if not self.can_scan:
            self.notify("Writes disabled. Reconnect to authenticate.", sound="error")
            return
        if self.mode == Mode.AUDIT and self.audit.active and not self.audit.belongs_to(self.scanner_id):
            self.notify(f"Audit is active on scanner {self.audit.owner_scanner_id}; this scan was not accepted.",
                        sound="error")
            return
        if self.mode == Mode.AUDIT and self.audit.belongs_to(self.scanner_id) and code.startswith(("ci1|", "chemotion-inventory:")):
            self.notify("A location is already selected. Scan bottles, or Finish/Cancel before selecting another location.",
                        sound="error")
            return
        if self.mode == Mode.AUDIT and not self.audit.active:
            self.start_audit(code)
        elif self.state == State.DESTINATION:
            if self.pending and code == self.pending.code:
                self.notify("Bottle already recognized. Scan a location or person.")
                return
            self.assign(code)
        else:
            self.loading_code = code
            self.state = State.LOADING
            self.deadline = self.clock() + self.service.config.pair_timeout if self.mode == Mode.ASSIGN else None
            self.notify("Bottle code received; loading identity and inventory (not saved)...")
            self.submit(lambda: self.service.load(code), self.loaded,
                        State.AUDIT_READY if self.audit.belongs_to(self.scanner_id) else State.IDLE)

    def loaded(self, pending: Pending) -> None:
        self.pending = pending
        self.describe(pending)
        if self.mode == Mode.LOOKUP:
            self.state, self.pending = State.IDLE, None
            self.notify("Lookup complete. No changes made.", True, sound="recognized")
        elif self.mode == Mode.AUDIT:
            self.state = State.AUDIT_SAVING
            self.notify("Physically seen; recording this bottle in the selected location...")
            self.submit(lambda: self.audit.scan(pending, self.scanner_id), self.audit_scanned, State.AUDIT_READY)
        else:
            self.state = State.DESTINATION
            self.deadline = self.clock() + self.service.config.pair_timeout
            self.notify("Bottle recognized. Scan a location or person (not saved yet).", sound="recognized")
            if self.buffer:
                code, self.buffer = self.buffer, None
                self.assign(code)

    def assign(self, code: str, found: bool = False) -> None:
        if self.state != State.DESTINATION or self.pending is None or self.busy:
            return
        try:
            target = parse_target(code)
        except WorkflowError as exc:
            self.notify(str(exc), sound="error")
            return
        self.target_code = code
        self.awaiting_found = None
        pending = self.pending
        self.state = State.SAVING
        self.notify("Validating, saving and reading back...")
        self.submit(lambda: self.service.assign(pending, target, found=found),
                    self.assigned, State.DESTINATION)

    def assigned(self, result: tuple[Inventory, str]) -> None:
        inventory, message = result
        if self.pending is None:
            raise WorkflowError("Save completed without a pending bottle; inspect chemotion before rescanning.")
        self.describe(replace(self.pending, inventory=inventory))
        self.pending, self.buffer, self.deadline = None, None, None
        self.state = State.IDLE
        self.notify(message + ". Scan the next bottle.", True, sound="success")

    def start_audit(self, code: str) -> None:
        try:
            target = parse_target(code)
        except WorkflowError as exc:
            self.notify(str(exc), sound="error")
            return
        self.state = State.AUDIT_STARTING
        self.notify("Reading the complete expected contents before accepting bottle scans...")

        def started(count):
            self.state = State.AUDIT_READY
            self.details = f"AUDIT: {target.scope_caption}\nExpected at start: {count}"
            self.notify("Audit started. Scan all bottles, then Finish location.", True, sound="recognized")

        self.submit(lambda: self.audit.start(target, self.scanner_id, self.service), started, State.IDLE)

    def audit_scanned(self, message: str) -> None:
        summary = self.audit.summary()
        self.pending = None
        self.state = State.AUDIT_READY
        self.notify(f"{message}. Seen here: {summary['seen']}. Continue or Finish location.", True,
                    sound="recognized" if message.startswith("Already scanned") else "success")

    def finish_audit(self) -> None:
        if not self.can_finish_audit:
            return
        self.state = State.FINISHING
        self.notify("Finishing: marking unseen expected bottles Missing and verifying each write...")

        def finished(summary):
            self.state = State.AUDIT_READY
            self.details = "\n".join(f"{key}: {value}" for key, value in summary.items())
            if summary["unresolved"]:
                denied = summary["permission_denied"]
                message = (
                    f"Audit INCOMPLETE: API token lacks write permission for {denied} bottle(s). "
                    "Check collection permissions and review the displayed summary."
                    if denied else
                    "Audit INCOMPLETE. Review the displayed summary; unresolved records were not retried."
                )
                self.notify(message, sound="error")
            else:
                self.state = State.IDLE
                self.notify("Audit complete and verified. Scan the next location.", True, sound="success")

        self.submit(lambda: self.audit.finish(self.scanner_id), finished, State.AUDIT_READY)

    def reset(self) -> bool:
        if not self.can_reset:
            self.notify("Wait for the operation. An in-flight write cannot be cancelled.")
            return False
        if self.audit.belongs_to(self.scanner_id):
            self.audit.cancel(self.scanner_id)
        self.generation += 1
        self.inflight = None
        self.discard_pair()
        if self.state != State.BLOCKED:
            self.state = State.IDLE
        self.last_error = ""
        self.notify("Reset. Saved updates were not undone. " + self.instruction())
        return True

    def tick(self) -> int | None:
        if self.deadline is None or self.state not in (State.LOADING, State.DESTINATION):
            return None
        remaining = max(0, int(self.deadline - self.clock()))
        if self.clock() >= self.deadline:
            self.reset()
            self.notify("Incomplete pair expired. Scan the bottle again; no assignment was sent.", sound="error")
        return remaining

    def close(self) -> None:
        if self.closed:
            return
        if self.busy:
            raise WorkflowError("Wait for the in-flight operation before closing.")
        self.reset()
        self.closed = True
        if self.owns_executor:
            self.executor.shutdown(wait=False)
