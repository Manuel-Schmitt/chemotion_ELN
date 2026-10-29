"""In-memory cabinet audits; only an explicit Finish marks unseen bottles Missing."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import TypedDict

from .api import ApiError, AuthenticationError, Inventory, PermissionDeniedError, Ref, UnverifiedWrite
from .workflow import (
    ASSIGNMENT_FIELDS, Conflict, Pending, Service, Target, WorkflowError, at_location,
    parse_target,
)


def snapshot(inventory: Inventory) -> dict[str, object]:
    return {key: inventory.first.get(key, "") for key in ASSIGNMENT_FIELDS + ("status",)}


class Outcome(str, Enum):
    UNSCANNED = ""
    PENDING = "pending"
    SEEN = "seen"
    MISSING = "missing"
    ALREADY_MISSING = "already_missing"
    PERMISSION_DENIED = "permission_denied"
    CONFLICT = "conflict"
    UNVERIFIED = "unverified"
    ERROR = "error"


@dataclass
class AuditBottle:
    ref: Ref
    chemical_id: int
    baseline: dict[str, object]
    expected: bool = False
    seen: bool = False
    outcome: Outcome = Outcome.UNSCANNED
    detail: str = ""
    relocated: bool = False
    recovered: bool = False
    outside_collection: bool = False

    def inventory(self) -> Inventory:
        return Inventory(self.ref, self.chemical_id, [copy.deepcopy(self.baseline)])


class AuditSummary(TypedDict):
    collection_id: int | None
    seen: int
    outside_collection: int
    relocated: int
    recovered: int
    missing: int
    already_missing: int
    conflicts: int
    permission_denied: int
    unresolved: int


class Audit:
    def __init__(self, service: Service):
        self.service = service
        self.target: Target | None = None
        self.owner_scanner_id: str | None = None
        self.records: dict[str, AuditBottle] = {}
        self.collection_refs: set[str] = set()
        self.scan_errors: set[str] = set()
        self.last_summary: AuditSummary | None = None

    @property
    def active(self) -> bool:
        return self.target is not None

    def belongs_to(self, scanner_id: str) -> bool:
        return self.active and self.owner_scanner_id == scanner_id

    def require_owner(self, scanner_id: str) -> None:
        if self.active and not self.belongs_to(scanner_id):
            raise WorkflowError(
                f"An audit is already active on scanner {self.owner_scanner_id}. "
                "Finish or cancel it before starting another audit on this terminal."
            )

    def failed_scan(self, code: str, scanner_id: str) -> None:
        if self.belongs_to(scanner_id):
            self.scan_errors.add(hashlib.sha256(code.encode("utf-8")).hexdigest())

    def start(self, target: Target, scanner_id: str, service: Service | None = None) -> int:
        if self.active:
            self.require_owner(scanner_id)
            raise WorkflowError("Finish or cancel the current audit first.")
        service = service or self.service
        target = parse_target(target.payload())
        if target.collection_id is None:
            raise WorkflowError("Audit requires collection_id on the destination label. Reprint it with its inventory collection ID.")
        inventories = service.client.inventories([target.collection_id])
        expected = [item for item in inventories if at_location(item, target)]
        if len(expected) > 30:
            raise WorkflowError("More than 30 bottles are expected here. Check the location and scope before auditing.")
        records = {
            item.ref.key: AuditBottle(item.ref, item.id, snapshot(item), expected=True)
            for item in expected
        }
        collection_refs = {item.ref.key for item in inventories}
        self.service = service
        self.owner_scanner_id = scanner_id
        self.scan_errors.clear()
        self.last_summary = None
        self.records = records
        self.collection_refs = collection_refs
        self.target = target
        return len(expected)

    def scan(self, pending: Pending, scanner_id: str) -> str:
        self.require_owner(scanner_id)
        if not self.target:
            raise WorkflowError("Start a location audit first.")
        row = self.records.get(pending.inventory.ref.key)
        if row is not None and row.seen and row.outcome == Outcome.SEEN:
            self.clear_scan_error(pending.code, scanner_id)
            return "Already scanned in this audit"
        if sum(item.seen for item in self.records.values()) >= 30 and (row is None or not row.seen):
            raise WorkflowError("30 bottles have already been seen. Finish or review the location scope.")
        moved = not at_location(pending.inventory, self.target)
        recovered = pending.inventory.first.get("status") == "Missing"
        if row is None:
            row = AuditBottle(
                pending.inventory.ref, pending.inventory.id, snapshot(pending.inventory),
                outside_collection=pending.inventory.ref.key not in self.collection_refs,
            )
            self.records[pending.inventory.ref.key] = row
        row.seen, row.outcome, row.detail = True, Outcome.PENDING, ""
        row.relocated = row.relocated or moved
        row.recovered = row.recovered or recovered
        try:
            _, message = self.service.assign(pending, self.target, audit=True)
        except (ApiError, WorkflowError) as exc:
            row.outcome = Outcome.PERMISSION_DENIED if isinstance(exc, PermissionDeniedError) else (
                Outcome.UNVERIFIED if isinstance(exc, UnverifiedWrite) else Outcome.ERROR)
            row.detail = str(exc)
            raise
        row.outcome, row.detail = Outcome.SEEN, ""
        self.clear_scan_error(pending.code, scanner_id)
        outside = row.outside_collection
        return f"{message} (outside audited collection; memberships unchanged)" if outside else message

    def clear_scan_error(self, code: str, scanner_id: str) -> None:
        if self.belongs_to(scanner_id):
            self.scan_errors.discard(hashlib.sha256(code.encode("utf-8")).hexdigest())

    def finish(self, scanner_id: str) -> AuditSummary:
        self.require_owner(scanner_id)
        if not self.active:
            raise WorkflowError("There is no active audit.")
        if self.scan_errors:
            raise WorkflowError("A bottle scan failed to load. Rescan that code successfully or cancel; Finish is blocked.")
        rows = list(self.records.values())
        if any(row.seen and row.outcome != Outcome.SEEN for row in rows):
            raise WorkflowError("Resolve failed/unverified scanned bottles before finishing, or cancel the audit.")
        for row in rows:
            if not row.expected or row.seen or row.outcome != Outcome.UNSCANNED:
                continue
            item = row.inventory()
            row.outcome, row.detail = Outcome.PENDING, "Missing update started."
            try:
                _, outcome = self.service.mark_missing(item)
            except AuthenticationError as exc:
                row.outcome, row.detail = Outcome.ERROR, str(exc)
                raise
            except (ApiError, WorkflowError) as exc:
                if isinstance(exc, PermissionDeniedError):
                    outcome = Outcome.PERMISSION_DENIED
                elif isinstance(exc, Conflict):
                    outcome = Outcome.CONFLICT
                elif isinstance(exc, UnverifiedWrite):
                    outcome = Outcome.UNVERIFIED
                else:
                    outcome = Outcome.ERROR
                row.outcome, row.detail = outcome, str(exc)
            else:
                row.outcome, row.detail = Outcome(outcome), ""
        summary = self.summary()
        self.last_summary = copy.deepcopy(summary)
        if not summary["unresolved"]:
            self.target = None
            self.owner_scanner_id = None
            self.records.clear()
            self.collection_refs.clear()
            self.scan_errors.clear()
        return summary

    def summary(self) -> AuditSummary:
        if not self.active:
            if self.last_summary is None:
                raise WorkflowError("There is no active or completed audit summary.")
            return copy.deepcopy(self.last_summary)
        rows = list(self.records.values())
        relevant = [row for row in rows if row.expected or row.seen]
        return {
            "collection_id": self.target.collection_id if self.target else None,
            "seen": sum(row.seen for row in relevant),
            "outside_collection": sum(row.outside_collection and row.seen for row in relevant),
            "relocated": sum(row.relocated and row.outcome == Outcome.SEEN for row in relevant),
            "recovered": sum(row.recovered and row.outcome == Outcome.SEEN for row in relevant),
            "missing": sum(row.outcome == Outcome.MISSING for row in relevant),
            "already_missing": sum(row.outcome == Outcome.ALREADY_MISSING for row in relevant),
            "conflicts": sum(row.outcome == Outcome.CONFLICT for row in relevant),
            "permission_denied": sum(row.outcome == Outcome.PERMISSION_DENIED for row in relevant),
            "unresolved": sum(row.outcome not in (Outcome.SEEN, Outcome.MISSING, Outcome.ALREADY_MISSING)
                              for row in relevant),
        }

    def cancel(self, scanner_id: str) -> None:
        if self.active:
            self.require_owner(scanner_id)
        self.target = None
        self.owner_scanner_id = None
        self.records.clear()
        self.collection_refs.clear()
        self.scan_errors.clear()
        self.last_summary = None
