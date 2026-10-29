"""Independent scanner workflows with one shared API worker and terminal audit."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Callable

from .audit import Audit
from .controller import Controller
from .workflow import ScannerConfig, Service, WorkflowError
from .zebra import ZebraScanner


@dataclass
class Panel:
    definition: ScannerConfig
    controller: Controller
    scanner: ZebraScanner | None = None
    ready: bool = False
    status: str = "Scanner not connected."
    failure_shown: bool = False


class Hub:
    def __init__(self, service: Service, changed: Callable[[str], None],
                 confirm_found: Callable[[str, str], None], feedback: Callable[[str, str], None]):
        self.config = service.config
        self.audit = Audit(service)
        # Serialize complete API operations, including fresh-read/PUT/readback, across scanners.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inventory-shared")
        self.closed = False
        self.panels: dict[str, Panel] = {}
        definitions = self.config.scanners or (ScannerConfig(
            "default", self.config.station_name, self.config.scanner_port,
        ),)
        for definition in definitions:
            name = f"{self.config.station_name} / {definition.name} [{definition.id}]"
            config = replace(self.config, station_name=name if self.config.scanners else self.config.station_name,
                             scanners=(), scanner_port=definition.port)
            scoped_service = Service(service.client, config)
            controller = Controller(
                scoped_service, self.audit, lambda key=definition.id: changed(key),
                lambda code, key=definition.id: confirm_found(key, code),
                executor=self.executor,
                feedback=lambda sound, key=definition.id: feedback(key, sound),
                scanner_id=definition.id,
            )
            keyboard = self.config.scanner_backend == "keyboard"
            self.panels[definition.id] = Panel(
                definition, controller, ready=keyboard,
                status="Keyboard input; scanner-controlled sound disabled." if keyboard else "Scanner not connected.",
            )
        self.active_id = next(iter(self.panels))

    @property
    def active(self) -> Panel:
        return self.panels[self.active_id]

    def select(self, scanner_id: str) -> None:
        if scanner_id not in self.panels:
            raise WorkflowError("Unknown scanner ID.")
        self.active_id = scanner_id

    def shutdown(self) -> None:
        if not self.closed:
            self.closed = True
            self.executor.shutdown(wait=True, cancel_futures=True)
