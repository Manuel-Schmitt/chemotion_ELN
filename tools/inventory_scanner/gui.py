"""Full-screen interface with keyboard input or optional Zebra SSI scanning."""

import logging
import queue
import textwrap
import tkinter as tk
from tkinter import messagebox, ttk

from .controller import Controller, MessageLevel
from .hub import Hub
from .workflow import Service, WorkflowError
from .zebra import ScannerError, ZebraScanner

logger = logging.getLogger(__name__)

STATUS_COLORS = {
    MessageLevel.INSTRUCTION: "#eef2f6",
    MessageLevel.WORKING: "#fff2cc",
    MessageLevel.SUCCESS: "#d9f2df",
    MessageLevel.WARNING: "#fff2cc",
    MessageLevel.ERROR: "#ffe1df",
}


class Station:
    def __init__(self, root: tk.Tk, service: Service):
        self.root = root
        self.confirming = False
        self.poll_id = None
        self.cleaned_up = False
        self.reporting_error = False
        self.hub = Hub(service, self.panel_changed, self.request_found, self.feedback)
        root.report_callback_exception = self.callback_error
        root.bind("<Destroy>", self.destroyed, add="+")
        root.title(service.config.station_name)
        root.geometry("1000x650")
        root.minsize(720, 440)
        root.attributes("-fullscreen", service.config.fullscreen)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<F11>", self.fullscreen)
        root.bind("<Escape>", lambda _: root.attributes("-fullscreen", False))
        style = ttk.Style(root)
        style.configure("TButton", font=("DejaVu Sans", 13), padding=7)
        style.configure("Inventory.Action.TButton", padding=5)
        style.configure("TLabel", font=("DejaVu Sans", 13))
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.heading = tk.Label(header, text=service.config.station_name, font=("DejaVu Sans", 14, "bold"),
                                height=2, width=1, anchor="w", justify="left", wraplength=520)
        self.heading.grid(row=0, column=0, sticky="ew")
        self.heading.bind("<Configure>", lambda event: self.heading.configure(wraplength=max(100, event.width - 8)))
        self.scanner_selector = ttk.Combobox(header, state="readonly", width=22)
        self.scanner_selector.grid(row=0, column=1, sticky="ne", padx=(5, 0))
        self.scanner_selector.bind("<<ComboboxSelected>>", self.select_scanner)
        self.modes = ttk.Frame(frame)
        self.modes.grid(row=1, column=0, sticky="ew", pady=8)
        self.mode_buttons = []
        for mode in ("Assign", "Lookup", "Audit"):
            button = ttk.Button(self.modes, text=mode, command=lambda value=mode: self.change_mode(value))
            button.pack(side="left", padx=(0, 8))
            self.mode_buttons.append(button)
        self.scanner_connect = ttk.Button(self.modes, text="Connect scanner", command=self.connect_scanner)
        if service.config.scanner_backend == "zebra_ssi":
            self.scanner_connect.pack(side="right")
        self.status = tk.Label(frame, anchor="w", justify="left", font=("DejaVu Sans", 16, "bold"),
                               height=2, width=1, wraplength=900, padx=8, pady=4)
        self.status.grid(row=2, column=0, sticky="ew")
        self.status.bind("<Configure>", lambda event: self.status.configure(wraplength=max(200, event.width - 20)))
        self.code = ttk.Entry(frame, font=("DejaVu Sans", 18))
        self.code.grid(row=3, column=0, sticky="ew", pady=8)
        self.code.bind("<Return>", self.scan)
        detail_frame = ttk.Frame(frame)
        detail_frame.grid(row=4, column=0, sticky="nsew")
        detail_frame.rowconfigure(0, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        self.details = tk.Text(detail_frame, height=2, width=1, wrap="word",
                               font=("DejaVu Sans", 14), state="disabled")
        self.details.grid(row=0, column=0, sticky="nsew")
        self.detail_scroll = ttk.Scrollbar(detail_frame, command=self.details.yview)
        self.detail_scroll.grid(row=0, column=1, sticky="ns")
        self.details.configure(yscrollcommand=self.detail_scroll.set)
        self.timer = tk.Label(frame, height=2, width=1, anchor="w", justify="left",
                              font=("DejaVu Sans", 12))
        self.timer.grid(row=5, column=0, sticky="ew", pady=4)
        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, sticky="ew")
        self.finish = ttk.Button(actions, text="Finish location", command=self.finish_audit,
                                 style="Inventory.Action.TButton")
        self.finish.pack(side="left", padx=(0, 8))
        self.found_button = ttk.Button(actions, text="Confirm Found", command=self.confirm_selected,
                                       style="Inventory.Action.TButton")
        self.reset_button = ttk.Button(actions, text="Reset / Cancel", command=self.reset,
                                       style="Inventory.Action.TButton")
        self.reset_button.pack(side="left", padx=(0, 8))
        self.reconnect = ttk.Button(actions, text="Reconnect", command=self.connect,
                                    style="Inventory.Action.TButton")
        self.reconnect.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Exit", command=self.close,
                   style="Inventory.Action.TButton").pack(side="right")
        for scanner_id, panel in self.hub.panels.items():
            panel.controller.connect()
            if service.config.scanner_backend == "zebra_ssi":
                self.connect_scanner(scanner_id)
        self.poll_id = self.root.after(50, self.poll)
        self.code.focus_set()

    @property
    def controller(self) -> Controller:
        return self.hub.active.controller

    @property
    def scanner(self) -> ZebraScanner | None:
        return self.hub.active.scanner

    @scanner.setter
    def scanner(self, value) -> None:
        self.hub.active.scanner = value

    @property
    def scanner_ready(self) -> bool:
        return self.hub.active.ready

    @scanner_ready.setter
    def scanner_ready(self, value: bool) -> None:
        self.hub.active.ready = value

    @property
    def scanner_status(self) -> str:
        return self.hub.active.status

    @scanner_status.setter
    def scanner_status(self, value: str) -> None:
        self.hub.active.status = value

    def panel_changed(self, scanner_id: str) -> None:
        self.render()

    def update_selector(self) -> None:
        labels = []
        for panel in self.hub.panels.values():
            model = panel.controller
            flag = " ! " if not panel.ready or model.can_reconnect or model.level == MessageLevel.ERROR or model.awaiting_found else ""
            labels.append(f"{panel.definition.name} [{panel.definition.id}]{flag}")
        self.scanner_selector.configure(values=labels)
        self.scanner_selector.current(list(self.hub.panels).index(self.hub.active_id))

    def select_scanner(self, _=None) -> None:
        if self.confirming:
            return
        index = self.scanner_selector.current()
        if index >= 0:
            self.hub.select(list(self.hub.panels)[index])
            self.code.delete(0, "end")
            self.render()

    def destroyed(self, event) -> None:
        if event.widget == self.root:
            self.cleanup()

    def cleanup(self) -> None:
        if self.cleaned_up:
            return
        self.cleaned_up = True
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        try:
            for panel in self.hub.panels.values():
                if panel.scanner is not None:
                    try:
                        panel.scanner.close()
                    except Exception:
                        logger.exception("Scanner %s failed to close", panel.definition.id)
                    finally:
                        panel.scanner = None
        finally:
            self.hub.shutdown()

    def connect_scanner(self, scanner_id: str | None = None) -> None:
        scanner_id = scanner_id or self.hub.active_id
        panel = self.hub.panels[scanner_id]
        if not panel.controller.can_connect_scanner:
            panel.controller.notify("Wait for the current operation before reconnecting the scanner.")
            return
        if panel.scanner is not None:
            try:
                panel.scanner.close()
            except ScannerError as exc:
                self.scanner_error(str(exc), scanner_id)
                return
        config = panel.controller.service.config
        panel.scanner = ZebraScanner(config.scanner_port, config.scanner_baudrate, config.scanner_timeout)
        panel.ready, panel.failure_shown = False, False
        panel.status = "Connecting Zebra SSI; waiting for command acknowledgement..."
        panel.scanner.start()
        self.panel_changed(scanner_id)

    def scanner_error(self, message: str, scanner_id: str | None = None) -> None:
        scanner_id = scanner_id or self.hub.active_id
        panel = self.hub.panels[scanner_id]
        panel.ready = False
        panel.status = message + " No reliable scanner sound. Inspect the last operation before reconnecting."
        if not panel.failure_shown:
            panel.failure_shown = True
            panel.controller.audit.failed_scan(
                "[scanner transport interrupted; cancel this audit]", scanner_id)
        self.panel_changed(scanner_id)

    def feedback(self, scanner_id: str, sound: str) -> None:
        panel = self.hub.panels[scanner_id]
        if panel.scanner is not None and panel.ready:
            try:
                panel.scanner.feedback(sound)
            except ScannerError as exc:
                self.scanner_error(str(exc), scanner_id)

    def poll_scanner(self) -> None:
        events = []
        for scanner_id, panel in self.hub.panels.items():
            try:
                events.extend(self.device_events(scanner_id))
            except Exception as exc:
                self.report_error(exc, panel.controller)
        for scanner_id, event in sorted(events, key=lambda item: item[1].timestamp):
            try:
                self.scanner_event(scanner_id, event)
            except Exception as exc:
                self.report_error(exc, self.hub.panels[scanner_id].controller)

    def device_events(self, scanner_id: str) -> list:
        panel = self.hub.panels[scanner_id]
        scanner = panel.scanner
        if scanner is None:
            return []
        if scanner.failure is not None:
            if not panel.failure_shown:
                self.scanner_error(scanner.failure, scanner_id)
            return []
        events = []
        for _ in range(32):
            try:
                event = scanner.events.get_nowait()
            except queue.Empty:
                break
            events.append((scanner_id, event))
        return events

    def scanner_event(self, scanner_id: str, event) -> None:
        panel = self.hub.panels[scanner_id]
        if event.kind == "ready":
            panel.ready = True
            panel.status = event.message
            self.panel_changed(scanner_id)
        elif event.kind == "scan":
            if panel.ready and not self.confirming:
                self.hub.select(scanner_id)
                self.code.delete(0, "end")
                self.render()
                panel.controller.scan(event.message)
            else:
                panel.controller.notify("Scanner input rejected during confirmation/disconnection. Rescan.",
                                        sound="error")
        elif event.kind == "status":
            panel.status = event.message + " Zebra SSI connected."
            self.panel_changed(scanner_id)

    def render(self) -> None:
        model = self.controller
        self.update_selector()
        owner = f" | Audit on {model.audit.owner_scanner_id}" if model.audit.active else ""
        self.heading.configure(
            text=f"{self.hub.config.station_name} | {model.account_name} | {model.mode}{owner}")
        headline = textwrap.shorten(model.message, width=110, placeholder="... (details below)")
        self.status.configure(text=headline, background=STATUS_COLORS[model.level])
        parts = [model.message]
        if model.audit.active and model.audit.target:
            parts.append(f"Audit on {model.audit.owner_scanner_id}: {model.audit.target.scope_caption}")
        if model.details:
            parts.append(model.details)
        if model.last_error and model.last_error != model.message:
            parts.append(f"Last error (previous operation): {model.last_error}")
        parts.append(self.scanner_status)
        parts.append(f"{self.hub.config.station_name} | {self.hub.active.definition.name} "
                     f"[{self.hub.active_id}] | {model.account_name} | {model.mode}")
        detail_text = "\n\n".join(parts)
        if self.details.get("1.0", "end-1c") != detail_text:
            self.details.configure(state="normal")
            self.details.delete("1.0", "end")
            self.details.insert("1.0", detail_text)
            self.details.configure(state="disabled")
        self.finish.configure(state="normal" if model.can_finish_audit and self.scanner_ready else "disabled")
        self.reset_button.configure(state="normal" if model.can_reset else "disabled")
        self.reconnect.configure(state="normal" if model.can_reconnect else "disabled")
        if model.awaiting_found and self.scanner_ready:
            self.found_button.pack(side="left", padx=(0, 8))
        else:
            self.found_button.pack_forget()
        for button in self.mode_buttons:
            button.configure(state="normal" if model.can_change_mode else "disabled")
        self.scanner_connect.configure(state="normal" if model.can_connect_scanner else "disabled")
        if not model.can_scan or not self.scanner_ready:
            self.code.configure(state="disabled")
        else:
            self.code.configure(state="normal")

    def poll(self) -> None:
        self.poll_id = None
        if self.cleaned_up or all(panel.controller.closed for panel in self.hub.panels.values()):
            return
        try:
            remaining = None
            for scanner_id, panel in self.hub.panels.items():
                try:
                    countdown = panel.controller.tick()
                    panel.controller.poll()
                    if scanner_id == self.hub.active_id:
                        remaining = countdown
                except Exception as exc:
                    self.report_error(exc, panel.controller)
            self.poll_scanner()
            countdown = f" | Pair resets in {remaining}s" if remaining is not None else ""
            self.timer.configure(
                text=textwrap.shorten(self.scanner_status + countdown, width=160, placeholder="..."),
                wraplength=max(400, self.root.winfo_width() - 30),
                foreground="#9c1c1c" if not self.scanner_ready else "#222222")
        except Exception as exc:
            self.report_error(exc)
        finally:
            if not self.cleaned_up and not all(panel.controller.closed for panel in self.hub.panels.values()):
                self.poll_id = self.root.after(50, self.poll)

    def report_error(self, exc: Exception, model: Controller | None = None) -> None:
        if self.reporting_error:
            logger.error("Station callback failed", exc_info=(type(exc), exc, exc.__traceback__))
            return
        self.reporting_error = True
        try:
            (model or self.controller).unexpected_error(exc)
        except Exception:
            logger.exception("Could not display station error")
            self.status.configure(text="Station error. Inspect the terminal log; try a new scan.",
                                  background=STATUS_COLORS[MessageLevel.ERROR])
        finally:
            self.reporting_error = False

    def callback_error(self, _kind, exc, traceback) -> None:
        self.report_error(exc.with_traceback(traceback))

    def scan(self, _=None) -> None:
        code = self.code.get()
        self.code.delete(0, "end")
        if self.scanner_ready and not self.confirming:
            self.controller.scan(code)
        else:
            self.controller.notify("Scanner unavailable or confirmation open; input was not accepted.")

    def request_found(self, scanner_id: str, code: str) -> None:
        self.panel_changed(scanner_id)

    def confirm_selected(self) -> None:
        code = self.controller.awaiting_found
        if code:
            self.confirm_found(code)

    def ask(self, title: str, message: str) -> bool:
        self.confirming = True
        self.scanner_selector.configure(state="disabled")
        try:
            return messagebox.askyesno(title, message, parent=self.root)
        finally:
            self.confirming = False
            self.scanner_selector.configure(state="readonly")

    def confirm_found(self, code: str) -> None:
        panel = self.hub.active
        model = panel.controller
        confirmed = self.ask("Recover Missing bottle",
                            f"Scanner: {panel.definition.name} [{panel.definition.id}]\n"
                            "Physically found this bottle?\nRestore Available and apply the scanned destination?")
        model.awaiting_found = None
        if confirmed and panel.ready:
            model.assign(code, found=True)
        else:
            model.notify("Recovery cancelled. Nothing saved; scan another destination or Reset.")
        self.code.focus_set()

    def change_mode(self, mode: str) -> None:
        self.controller.set_mode(mode)
        self.code.focus_set()

    def reset(self) -> None:
        model = self.controller
        if model.audit.belongs_to(model.scanner_id):
            if not self.ask(
                "Cancel / reset",
                f"Scanner: {self.hub.active.definition.name} [{self.hub.active_id}]\n"
                "Saved updates will NOT be undone. Unverified writes may have reached chemotion.\n"
                "Cancel this operation? Unseen bottles will not be newly marked Missing.",
            ):
                return
        model.reset()
        self.code.focus_set()

    def finish_audit(self) -> None:
        if self.scanner_ready and not self.confirming:
            self.controller.finish_audit()
        self.code.focus_set()

    def connect(self) -> None:
        self.controller.connect()
        self.code.focus_set()

    def fullscreen(self, _=None) -> None:
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def close(self) -> None:
        if any(panel.controller.busy for panel in self.hub.panels.values()):
            messagebox.showwarning("Operation in progress", "Wait for the operation before exiting.", parent=self.root)
            return
        if not self.ask("Exit", "Exit all scanner sessions? Saved changes remain applied."):
            return
        for panel in self.hub.panels.values():
            panel.controller.close()
        self.root.destroy()


def run(service: Service) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise WorkflowError("Cannot open a graphical display. Run the station inside the Pi desktop session.") from exc
    try:
        Station(root, service)
        root.mainloop()
    except (OSError, WorkflowError) as exc:
        messagebox.showerror("Station error", str(exc), parent=root)
        root.destroy()
        raise
    finally:
        if root.tk.call("info", "commands", "."):
            root.destroy()
