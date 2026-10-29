"""Validated labels and inventory operations shared by the CLI and desktop UI."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .api import ApiError, Client, Inventory

PREFIX = "ci1|"
MAX_CODE_BYTES = 2048
MAX_LOCATION_LENGTH = 100
ASSIGNMENT_FIELDS = (
    "current_building", "current_room", "current_cabinet",
    "current_group", "borrowed_by",
)


class WorkflowError(ValueError):
    """An actionable input or workflow error."""


def positive_id(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise WorkflowError(f"{label} must be a positive integer.")
    return value


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=unique_object,
                          parse_constant=lambda _: reject_constant())
    except json.JSONDecodeError as exc:
        raise WorkflowError("Invalid JSON.") from exc


def reject_constant() -> None:
    raise WorkflowError("Non-finite JSON numbers are not supported.")


@dataclass(frozen=True)
class Target:
    type: str = field(default="location", compare=False)
    group: str = ""
    building: str = ""
    room: str = ""
    cabinet: str = ""
    borrower: str = ""
    collection_id: int | None = None

    @property
    def assignment(self) -> dict[str, str]:
        return dict(zip(ASSIGNMENT_FIELDS, (
            self.building, self.room, self.cabinet, self.group, self.borrower,
        )))

    @property
    def caption(self) -> str:
        path = " / ".join(part for part in (self.building, self.room, self.cabinet) if part)
        return " | ".join(part for part in (
            path, f"Group: {self.group}" if self.group else "",
            f"Borrower: {self.borrower}" if self.borrower else "",
        ) if part)

    @property
    def scope_caption(self) -> str:
        return (f"{self.caption} | Collection {self.collection_id}"
                if self.collection_id is not None else self.caption)

    def payload(self) -> str:
        if self.type not in ("location", "person"):
            raise WorkflowError("Unsupported destination label type.")
        values = (self.building, self.room, self.cabinet, self.group, self.borrower)
        for field_name, value in zip(("building", "room", "cabinet", "group", "borrower"), values):
            validate_destination_text(field_name, value)
        if not any(values):
            raise WorkflowError("A destination label must supply at least one nonblank location, group or borrower value.")
        collection_id = "" if self.collection_id is None else str(positive_id(self.collection_id, "Collection ID"))
        text = PREFIX + "|".join((*(_escape_label_value(value) for value in values), collection_id))
        parse_target(text)
        return text


def validate_destination_text(field_name: str, value: Any) -> str:
    if (not isinstance(value, str) or len(value) > MAX_LOCATION_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
            or value != value.strip()):
        raise WorkflowError(f"{field_name} must be trimmed text of at most {MAX_LOCATION_LENGTH} characters.")
    return value


def _escape_label_value(value: str) -> str:
    return value.replace("%", "%25").replace("|", "%7C")


def _unescape_label_value(value: str) -> str:
    decoded = []
    index = 0
    while index < len(value):
        if value[index] != "%":
            decoded.append(value[index])
            index += 1
            continue
        escape = value[index:index + 3]
        if escape == "%25":
            decoded.append("%")
        elif escape == "%7C":
            decoded.append("|")
        else:
            raise WorkflowError("Malformed destination escape; only %25 and %7C are supported.")
        index += 3
    return "".join(decoded)


def parse_target(code: str) -> Target:
    try:
        size = len(code.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WorkflowError("Destination labels must contain valid UTF-8 text.") from exc
    if size > MAX_CODE_BYTES:
        raise WorkflowError(f"Scanned input exceeds {MAX_CODE_BYTES} bytes.")
    if not code.startswith(PREFIX):
        raise WorkflowError("Unsupported or obsolete destination label. Reprint it using the ci1 format.")
    slots = code[len(PREFIX):].split("|")
    if len(slots) != 6:
        raise WorkflowError("A ci1 destination label must contain exactly five destination fields and one collection ID slot.")
    fields = ("building", "room", "cabinet", "group", "borrower")
    parts = {
        name: validate_destination_text(name, _unescape_label_value(value))
        for name, value in zip(fields, slots[:5])
    }
    if slots[5]:
        if not re.fullmatch(r"[0-9]+", slots[5]) or str(int(slots[5])) != slots[5]:
            raise WorkflowError("Collection ID must be a canonical positive decimal integer.")
        collection_id = positive_id(int(slots[5]), "Collection ID")
    else:
        collection_id = None
    if not any(parts.values()):
        raise WorkflowError("A destination label must supply at least one nonblank location, group or borrower value.")
    return Target(group=parts["group"], building=parts["building"], room=parts["room"],
                  cabinet=parts["cabinet"], borrower=parts["borrower"], collection_id=collection_id)


@dataclass(frozen=True)
class ScannerConfig:
    id: str
    name: str
    port: str


@dataclass(frozen=True)
class Config:
    base_url: str
    station_name: str = "Inventory station"
    timeout: float = 15
    pair_timeout: float = 60
    fullscreen: bool = True
    scanner_backend: str = "keyboard"
    scanner_port: str = ""
    scanner_baudrate: int = 9600
    scanner_timeout: float = 2
    scanners: tuple[ScannerConfig, ...] = ()

    @classmethod
    def load(cls, path: Path) -> Config:
        data = strict_json(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "inventory_collection_ids" in data:
            raise WorkflowError(
                "Remove inventory_collection_ids from station configuration and reprint location labels "
                "with collection_id. Audit scope now comes from the scanned location label."
            )
        if isinstance(data, dict) and "group_id" in data:
            raise WorkflowError(
                "Remove group_id from station configuration. The station is no longer group-locked; "
                "labels now supply static group and borrower text."
            )
        if isinstance(data, dict) and "audit_directory" in data:
            raise WorkflowError("Remove audit_directory from station configuration; audit progress is now kept in memory only.")
        fields = set(cls.__dataclass_fields__)
        if not isinstance(data, dict) or set(data) - fields:
            raise WorkflowError("Configuration must be an object containing only supported settings.")
        url = data.get("base_url")
        if not isinstance(url, str):
            raise WorkflowError("base_url must be an HTTPS URL.")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise WorkflowError("base_url must be HTTPS without credentials, query or fragment.")
        name = data.get("station_name", cls.station_name)
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise WorkflowError("station_name must be nonblank text of at most 100 characters.")
        for key in ("timeout", "pair_timeout"):
            value = data.get(key, getattr(cls, key))
            if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 3600:
                raise WorkflowError(f"{key} must be between 1 and 3600 seconds.")
        if type(data.get("fullscreen", True)) is not bool:
            raise WorkflowError("fullscreen must be a boolean.")
        backend = data.get("scanner_backend", "keyboard")
        if backend not in ("keyboard", "zebra_ssi"):
            raise WorkflowError("scanner_backend must be keyboard or zebra_ssi.")
        port = data.get("scanner_port", "")
        if not isinstance(port, str) or port != port.strip() or any(ord(char) < 32 for char in port):
            raise WorkflowError("scanner_port must be a device path without control characters.")
        scanners = data.get("scanners", [])
        if not isinstance(scanners, list):
            raise WorkflowError("scanners must be an array of scanner definitions.")
        definitions = []
        for item in scanners:
            if not isinstance(item, dict) or set(item) != {"id", "name", "port"}:
                raise WorkflowError("Each scanner requires exactly id, name and port.")
            identifier, scanner_name, scanner_port = item["id"], item["name"], item["port"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", identifier):
                raise WorkflowError("Scanner IDs must contain 1-40 ASCII letters, digits, underscores or hyphens.")
            if (not isinstance(scanner_name, str) or not scanner_name.strip() or len(scanner_name) > 60
                    or not scanner_name.isprintable()):
                raise WorkflowError("Scanner names must contain 1-60 printable characters.")
            if (not isinstance(scanner_port, str) or not scanner_port or scanner_port != scanner_port.strip()
                    or not scanner_port.isprintable()):
                raise WorkflowError("Each scanner port must be a nonblank device path.")
            definitions.append(ScannerConfig(identifier, scanner_name, scanner_port))
        if len({item.id for item in definitions}) != len(definitions):
            raise WorkflowError("Scanner IDs must be unique.")
        if len({str(Path(item.port).expanduser().resolve()) for item in definitions}) != len(definitions):
            raise WorkflowError("Each scanner must use a different serial device.")
        if definitions and (backend != "zebra_ssi" or port):
            raise WorkflowError("For scanners, select zebra_ssi and leave the legacy scanner_port empty.")
        data["scanners"] = tuple(definitions)
        if backend == "zebra_ssi" and not port and not definitions:
            raise WorkflowError("Set scanner_port to the Zebra SSI serial device.")
        baudrate = data.get("scanner_baudrate", 9600)
        if type(baudrate) is not int or baudrate not in (9600, 19200, 38400, 57600, 115200):
            raise WorkflowError("scanner_baudrate must be 9600, 19200, 38400, 57600 or 115200.")
        scanner_timeout = data.get("scanner_timeout", 2)
        if (type(scanner_timeout) not in (int, float) or not math.isfinite(scanner_timeout)
                or not 0.2 <= scanner_timeout <= 30):
            raise WorkflowError("scanner_timeout must be between 0.2 and 30 seconds.")
        return cls(**data)


def signature(inventory: Inventory, include_status: bool = True) -> tuple:
    fields = ASSIGNMENT_FIELDS + (("status",) if include_status else ())
    return tuple(inventory.first.get(field, "") for field in fields)


def at_location(inventory: Inventory, target: Target) -> bool:
    return signature(inventory, False) == tuple(target.assignment.values())


@dataclass(frozen=True)
class Pending:
    inventory: Inventory
    label: str
    code: str


class Conflict(WorkflowError):
    def __init__(self, latest: Inventory):
        super().__init__("The recorded location or status changed. Review the refreshed bottle and scan the destination again.")
        self.latest = latest


class FoundConfirmation(WorkflowError):
    def __init__(self) -> None:
        super().__init__("This bottle is Missing. Confirm Found to restore Available and assign it.")


class Service:
    def __init__(self, client: Client, config: Config):
        self.client = client
        self.config = config

    def load(self, code: str) -> Pending:
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise WorkflowError(f"Scanned input exceeds {MAX_CODE_BYTES} bytes.")
        if code.startswith((PREFIX, "chemotion-inventory:")):
            raise WorkflowError("Scan the bottle code first, not a destination.")
        ref = self.client.resolve(code)
        inventory = self.client.inventory(ref)
        label = self.client.describe(ref)
        return Pending(inventory, label, code)

    def assign(self, pending: Pending, target: Target, *, found: bool = False,
               audit: bool = False) -> tuple[Inventory, str]:
        target = parse_target(target.payload())
        latest = self.client.inventory(pending.inventory.ref)
        if latest.id != pending.inventory.id or signature(latest) != signature(pending.inventory):
            raise Conflict(latest)
        recovering = latest.first.get("status") == "Missing"
        if recovering and not (found or audit):
            raise FoundConfirmation()
        data = copy.deepcopy(latest.data)
        data[0].update(target.assignment)
        if recovering:
            notes = data[0].get("important_notes", "")
            if notes is None:
                notes = ""
            if not isinstance(notes, str):
                raise WorkflowError("Important notes is not text; correct it in chemotion before recovery.")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            note = f"Found {now} at {target.caption} ({self.config.station_name})."
            data[0]["important_notes"] = f"{notes}\n{note}".lstrip("\n")
            data[0]["status"] = "Available"
        if data == latest.data:
            return latest, "Already assigned (verified)"
        result = self.client.update_inventory(latest, data)
        return result, "Recovered and saved" if recovering else "Saved and verified"

    def mark_missing(self, baseline: Inventory) -> tuple[Inventory, str]:
        latest = self.client.inventory(baseline.ref)
        if latest.id != baseline.id or signature(latest) != signature(baseline):
            raise Conflict(latest)
        if latest.first.get("status") == "Missing":
            return latest, "already_missing"
        data = copy.deepcopy(latest.data)
        data[0]["status"] = "Missing"
        return self.client.update_inventory(latest, data), "missing"
