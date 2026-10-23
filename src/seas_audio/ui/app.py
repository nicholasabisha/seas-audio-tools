"""Drag-and-drop window for the Seas audio tools.

One window, two tabs. Both tabs share a ``DropZone`` that accepts files *and*
folders from Explorer and hands back real absolute paths, which is the whole
reason this is Tkinter + tkdnd rather than a webview.

Long work runs on a worker thread and talks to the UI only through a queue, so
no Tk widget is ever touched off the main thread.
"""

from __future__ import annotations

import csv
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from seas_audio import __version__
from seas_audio.core.cache import FingerprintCache
from seas_audio.core.config import (
    APP_TITLE,
    DEFAULT_DURATION_TOLERANCE,
    DEFAULT_MAX_OFFSET_FRAMES,
    DEFAULT_SIMILARITY_THRESHOLD,
    AppPaths,
    Settings,
)
from seas_audio.core.fpcalc import FpcalcNotFoundError, probe
from seas_audio.core.library import build_library
from seas_audio.core.scanner import collect_audio_files
from seas_audio.tools import duplicates, sanitizer

try:  # tkdnd ships compiled Tcl binaries, so treat it as optional.
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the local environment
    DND_FILES = None
    TkinterDnD = None
    DND_AVAILABLE = False

QUEUE_POLL_MS = 60
MAX_LOG_LINES = 3000

COLOUR_DROP = "#f4f7fb"
COLOUR_DROP_ACTIVE = "#dbe9ff"
COLOUR_BORDER = "#b8c4d6"
COLOUR_TEXT = "#22303f"
COLOUR_MUTED = "#5a6b7d"
COLOUR_ERROR = "#b3261e"
COLOUR_OK = "#1b6b35"

_RootBase = TkinterDnD.Tk if DND_AVAILABLE else tk.Tk


def _asset(name: str) -> Path | None:
    """Find a bundled asset both in development and inside a PyInstaller app."""
    bundle = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if bundle:
        candidates.append(Path(bundle) / name)
    here = Path(__file__).resolve()
    candidates.append(here.parents[3] / "assets" / name)
    candidates.append(here.parents[2] / "assets" / name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _reveal_in_explorer(path: str) -> None:
    """Open Explorer with the file selected."""
    target = os.path.normpath(path)
    try:
        if os.name == "nt":
            subprocess.run(["explorer", "/select,", target], check=False)
        else:  # pragma: no cover - Windows is the target platform
            subprocess.run(["xdg-open", os.path.dirname(target)], check=False)
    except OSError:
        pass


class LogPane(ttk.Frame):
    """Read-only scrolling log with colour tags."""

    def __init__(self, master: tk.Misc, height: int = 12) -> None:
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._text = tk.Text(
            self,
            height=height,
            wrap="word",
            state="disabled",
            background="#fbfcfd",
            foreground=COLOUR_TEXT,
            relief="flat",
            padx=8,
            pady=6,
        )
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self._text.tag_configure("error", foreground=COLOUR_ERROR)
        self._text.tag_configure("ok", foreground=COLOUR_OK)
        self._text.tag_configure("muted", foreground=COLOUR_MUTED)

    def append(self, message: str, tag: str | None = None) -> None:
        self._text.configure(state="normal")
        self._text.insert("end", message + "\n", (tag,) if tag else ())
        lines = int(self._text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self._text.delete("1.0", str(lines - MAX_LOG_LINES) + ".0")
        self._text.see("end")
        self._text.configure(state="disabled")

    def clear(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")


class DropZone(ttk.Frame):
    """A target that accepts dropped files and folders and keeps their paths."""

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        *,
        hint: str = "",
        height: int = 6,
        on_change=None,
    ) -> None:
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self._paths: list[Path] = []
        self._on_change = on_change
        self._enabled = True

        ttk.Label(self, text=title, font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )

        self._target = tk.Label(
            self,
            text=(
                "Drop files or folders here"
                if DND_AVAILABLE
                else "Drag and drop unavailable - use Browse..."
            ),
            background=COLOUR_DROP,
            foreground=COLOUR_MUTED,
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=COLOUR_BORDER,
            padx=12,
            pady=18,
            cursor="hand2",
        )
        self._target.grid(row=1, column=0, sticky="ew")
        self._target.bind("<Button-1>", lambda _event: self.browse())

        self._listbox = tk.Listbox(
            self,
            height=height,
            selectmode="extended",
            activestyle="none",
            background="#ffffff",
            foreground=COLOUR_TEXT,
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=scrollbar.set)
        self._listbox.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        scrollbar.grid(row=2, column=1, sticky="ns", pady=(6, 0))
        self.rowconfigure(2, weight=1)

        buttons = ttk.Frame(self)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(buttons, text="Browse...", command=self.browse).pack(side="left")
        ttk.Button(buttons, text="Remove selected", command=self.remove_selected).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="Clear", command=self.clear).pack(side="left", padx=(6, 0))

        self._hint = ttk.Label(self, text=hint, foreground=COLOUR_MUTED)
        self._hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        if DND_AVAILABLE:
            for widget in (self._target, self._listbox):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DropEnter>>", self._on_drag_enter)
                widget.dnd_bind("<<DropLeave>>", self._on_drag_leave)

    @property
    def paths(self) -> list[Path]:
        return list(self._paths)

    def add_paths(self, raw_paths) -> int:
        """Add paths, keeping the dropped order and skipping duplicates."""
        added = 0
        seen = {_norm(path) for path in self._paths}
        for raw in raw_paths:
            if not raw:
                continue
            path = Path(str(raw).strip().strip('"'))
            key = _norm(path)
            if key in seen:
                continue
            seen.add(key)
            self._paths.append(path)
            added += 1
        if added:
            self._refresh()
            self._notify()
        return added

    def clear(self) -> None:
        if not self._paths:
            return
        self._paths.clear()
        self._refresh()
        self._notify()

    def remove_selected(self) -> None:
        selection = set(self._listbox.curselection())
        if not selection:
            return
        self._paths = [
            path for index, path in enumerate(self._paths) if index not in selection
        ]
        self._refresh()
        self._notify()

    def browse(self) -> None:
        if not self._enabled:
            return
        chosen = filedialog.askdirectory(title="Choose a music folder")
        if chosen:
            self.add_paths([chosen])

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._target.configure(cursor="hand2" if enabled else "arrow")

    def _refresh(self) -> None:
        self._listbox.delete(0, "end")
        for path in self._paths:
            suffix = "\\" if path.is_dir() else ""
            self._listbox.insert("end", f"{path}{suffix}")
        if not self._paths:
            self._hint.configure(text="Nothing queued yet.")
        else:
            self._hint.configure(text=f"{len(self._paths)} path(s) queued.")

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def _on_drop(self, event):
        raw = getattr(event, "data", "") or ""
        try:
            # tkdnd hands back a raw Tcl list; splitlist keeps spaced paths intact.
            parts = self.tk.splitlist(raw)
        except Exception:
            parts = [raw]
        self.add_paths(parts)
        self._paint(active=False)

    def _on_drag_enter(self, _event):
        self._paint(active=True)

    def _on_drag_leave(self, _event):
        self._paint(active=False)

    def _paint(self, *, active: bool) -> None:
        self._target.configure(
            background=COLOUR_DROP_ACTIVE if active else COLOUR_DROP,
            highlightbackground="#4a86e8" if active else COLOUR_BORDER,
        )


class JobFrame(ttk.Frame):
    """Frame that runs one background job and receives queued events."""

    def __init__(self, master: tk.Misc, app: "App") -> None:
        super().__init__(master, padding=10)
        self.app = app
        self._events: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._running = False

    def start_job(self, target) -> None:
        if self._running:
            return
        self._running = True
        self._cancel.clear()
        self.on_job_start()
        threading.Thread(target=self._run_guarded, args=(target,), daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll)

    def _run_guarded(self, target) -> None:
        try:
            target()
        except FpcalcNotFoundError as exc:
            self._emit("error", f"fpcalc is missing.\n{exc}")
        except Exception as exc:  # keep the window alive whatever happens
            self._emit("error", f"{type(exc).__name__}: {exc}")
        finally:
            self._emit("finished")

    def _emit(self, kind: str, *payload) -> None:
        self._events.put((kind, payload))

    def _poll(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "finished":
                self._running = False
                self.on_job_finish()
                return
            self.handle_event(kind, *payload)
        if self._running:
            self.after(QUEUE_POLL_MS, self._poll)

    def request_cancel(self) -> None:
        self._cancel.set()
        self.on_cancel_requested()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def on_job_start(self) -> None: ...

    def on_job_finish(self) -> None: ...

    def on_cancel_requested(self) -> None: ...

    def handle_event(self, kind: str, *payload) -> None: ...


class SanitizeTab(JobFrame):
    """Drop a folder, fix the tags."""

    def __init__(self, master: tk.Misc, app: "App") -> None:
        super().__init__(master, app)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self.drop = DropZone(
            self,
            "Files or folders to sanitize",
            hint="MP3 files only. Other audio formats are skipped.",
        )
        self.drop.grid(row=0, column=0, sticky="nsew")

        options = ttk.LabelFrame(self, text="Options", padding=8)
        options.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.dry_run = tk.BooleanVar(value=False)
        self.backup = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options, text="Dry run (report only, change nothing)", variable=self.dry_run
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            options, text="Keep a .bak copy before writing", variable=self.backup
        ).grid(row=0, column=1, sticky="w", padx=(18, 0))

        controls = ttk.Frame(self)
        controls.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.run_button = ttk.Button(controls, text="Sanitize", command=self.run)
        self.run_button.pack(side="left")
        self.cancel_button = ttk.Button(
            controls, text="Cancel", command=self.request_cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(controls, maximum=100, value=0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.progress_label = ttk.Label(controls, text="idle", foreground=COLOUR_MUTED)
        self.progress_label.pack(side="left", padx=(10, 0))

        self.log = LogPane(self, height=14)
        self.log.grid(row=3, column=0, sticky="nsew", pady=(10, 0))

    def run(self) -> None:
        if not self.drop.paths:
            messagebox.showinfo(APP_TITLE, "Drop a file or folder first.")
            return
        self.log.clear()
        self.start_job(self._work)

    def _work(self) -> None:
        targets = self.drop.paths
        self._emit("log", f"Scanning {len(targets)} target(s)...")

        def warn(message: str) -> None:
            self._emit("log", message, "muted")

        files = collect_audio_files(targets, warn=warn)
        if not files:
            self._emit("log", "No supported audio files found.", "error")
            return

        self._emit("log", f"Found {len(files)} audio file(s).")
        results = sanitizer.sanitize_many(
            files,
            dry_run=self.dry_run.get(),
            backup=self.backup.get(),
            on_progress=lambda done, total, name: self._emit("progress", done, total, name),
            should_cancel=lambda: self.cancelled,
        )

        counts: dict[str, int] = {}
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
            if result.status == sanitizer.STATUS_ERROR:
                self._emit("log", f"error  {result.path.name}: {result.message}", "error")
            elif result.status == sanitizer.STATUS_SKIPPED:
                continue
            elif result.status == sanitizer.STATUS_CLEAN:
                self._emit("log", f"clean  {result.path.name}", "muted")
            else:
                self._emit(
                    "log", f"{result.status}  {result.path.name}: {result.message}", "ok"
                )

        if self.cancelled:
            self._emit("log", "Cancelled - stopped early.", "error")
        self._emit(
            "log",
            "Summary: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())),
            "ok",
        )
        if self.dry_run.get():
            self._emit("log", "Dry run - no files were modified.", "muted")

    def on_job_start(self) -> None:
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.drop.set_enabled(False)
        self.progress.configure(value=0)
        self.progress_label.configure(text="starting...")

    def on_job_finish(self) -> None:
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.drop.set_enabled(True)
        self.progress_label.configure(text="finished")
        self.app.refresh_status()

    def on_cancel_requested(self) -> None:
        self.progress_label.configure(text="cancelling...")

    def handle_event(self, kind: str, *payload) -> None:
        if kind == "log":
            message = payload[0]
            tag = payload[1] if len(payload) > 1 else None
            self.log.append(message, tag)
        elif kind == "progress":
            done, total, name = payload
            self.progress.configure(value=(done / total * 100) if total else 0)
            self.progress_label.configure(text=f"{done:,}/{total:,}  {name}"[:64])
        elif kind == "error":
            self.log.append(payload[0], "error")
            messagebox.showerror(APP_TITLE, payload[0])


class DuplicatesTab(JobFrame):
    """Drop a library, find the near-duplicates."""

    def __init__(self, master: tk.Misc, app: "App") -> None:
        super().__init__(master, app)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        self._pairs: list[duplicates.DuplicatePair] = []

        zones = ttk.Frame(self)
        zones.grid(row=0, column=0, sticky="nsew")
        zones.columnconfigure(0, weight=1)
        zones.columnconfigure(1, weight=1)

        self.primary = DropZone(
            zones,
            "Library / batch to scan",
            hint="Fingerprints are cached, so repeat runs are fast.",
            height=5,
        )
        self.primary.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        self.reference = DropZone(
            zones,
            "Reference library (optional)",
            hint="Leave empty to scan the batch against itself.",
            height=5,
        )
        self.reference.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        tuning = ttk.LabelFrame(self, text="Matching", padding=8)
        tuning.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        self.threshold = tk.DoubleVar(value=DEFAULT_SIMILARITY_THRESHOLD)
        self.tolerance = tk.DoubleVar(value=DEFAULT_DURATION_TOLERANCE)
        self.max_offset = tk.IntVar(value=DEFAULT_MAX_OFFSET_FRAMES)
        self.use_cache = tk.BooleanVar(value=True)

        ttk.Label(tuning, text="Similarity threshold").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            tuning,
            from_=0.5,
            to=1.0,
            increment=0.01,
            width=6,
            textvariable=self.threshold,
            format="%.2f",
        ).grid(row=0, column=1, sticky="w", padx=(6, 18))

        ttk.Label(tuning, text="Duration window (s)").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(
            tuning, from_=0, to=120, increment=1, width=6, textvariable=self.tolerance
        ).grid(row=0, column=3, sticky="w", padx=(6, 18))

        ttk.Label(tuning, text="Offset search").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(
            tuning, from_=0, to=500, increment=10, width=6, textvariable=self.max_offset
        ).grid(row=0, column=5, sticky="w", padx=(6, 18))

        ttk.Checkbutton(
            tuning, text="Use fingerprint cache", variable=self.use_cache
        ).grid(row=0, column=6, sticky="w")

        controls = ttk.Frame(self)
        controls.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.run_button = ttk.Button(controls, text="Find duplicates", command=self.run)
        self.run_button.pack(side="left")
        self.cancel_button = ttk.Button(
            controls, text="Cancel", command=self.request_cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(6, 0))
        self.export_button = ttk.Button(
            controls, text="Export CSV...", command=self.export_csv, state="disabled"
        )
        self.export_button.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(controls, maximum=100, value=0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.progress_label = ttk.Label(controls, text="idle", foreground=COLOUR_MUTED)
        self.progress_label.pack(side="left", padx=(10, 0))

        self.results = ttk.Treeview(
            self,
            columns=("similarity", "file_a", "kbps_a", "file_b", "kbps_b"),
            show="headings",
            height=8,
        )
        for column, heading, width, anchor in (
            ("similarity", "Match", 70, "center"),
            ("file_a", "Scanned file", 300, "w"),
            ("kbps_a", "kbps", 60, "center"),
            ("file_b", "Matches", 300, "w"),
            ("kbps_b", "kbps", 60, "center"),
        ):
            self.results.heading(column, text=heading)
            self.results.column(column, width=width, anchor=anchor)
        result_scroll = ttk.Scrollbar(self, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=result_scroll.set)
        self.results.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        result_scroll.grid(row=3, column=1, sticky="ns", pady=(10, 0))
        self.results.bind("<Double-1>", self._reveal_selected)

        self.log = LogPane(self, height=7)
        self.log.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(10, 0))

    def run(self) -> None:
        if not self.primary.paths:
            messagebox.showinfo(APP_TITLE, "Drop the library or batch you want to scan.")
            return
        self.log.clear()
        self.results.delete(*self.results.get_children())
        self._pairs = []
        self.export_button.configure(state="disabled")
        self.start_job(self._work)

    def _work(self) -> None:
        paths = self.app.paths
        cache = FingerprintCache(paths.fingerprint_cache)
        skip = [paths.quarantine_dir]

        def warn(message: str) -> None:
            self._emit("log", message, "muted")

        self._emit("phase", "Scanning folders...")
        primary_files = collect_audio_files(self.primary.paths, skip_dirs=skip, warn=warn)
        if not primary_files:
            self._emit("log", "No supported audio files in the scanned paths.", "error")
            return
        self._emit("log", f"Primary set: {len(primary_files)} file(s).")

        reference_files = (
            collect_audio_files(self.reference.paths, skip_dirs=skip, warn=warn)
            if self.reference.paths
            else []
        )
        if reference_files:
            self._emit("log", f"Reference set: {len(reference_files)} file(s).")

        self._emit("phase", "Fingerprinting primary...")
        primary_build = build_library(
            primary_files,
            cache,
            use_cache=self.use_cache.get(),
            on_progress=lambda done, total: self._emit("progress", done, total),
            should_cancel=lambda: self.cancelled,
        )
        if primary_build.cache_note:
            self._emit("log", primary_build.cache_note, "muted")
        if primary_build.added:
            self._emit("log", f"Fingerprinted {primary_build.added} new file(s).")
        else:
            self._emit("log", "All primary fingerprints came from the cache.", "ok")
        for path, reason in primary_build.failures:
            self._emit("log", f"unreadable  {Path(path).name}: {reason}", "error")

        if self.cancelled:
            return

        reference_build = None
        if reference_files:
            self._emit("phase", "Fingerprinting reference...")
            reference_build = build_library(
                reference_files,
                cache,
                use_cache=self.use_cache.get(),
                on_progress=lambda done, total: self._emit("progress", done, total),
                should_cancel=lambda: self.cancelled,
            )
            for path, reason in reference_build.failures:
                self._emit("log", f"unreadable  {Path(path).name}: {reason}", "error")

        if self.cancelled:
            return

        params = duplicates.SimilarityParams(
            duration_tolerance=float(self.tolerance.get()),
            threshold=float(self.threshold.get()),
            max_offset_frames=int(self.max_offset.get()),
        )
        reference_records = reference_build.records if reference_build else None
        total = duplicates.count_candidate_pairs(
            primary_build.records, reference_records, params.duration_tolerance
        )
        self._emit("log", f"Comparing {total:,} candidate pair(s).")
        self._emit("phase", "Comparing fingerprints...")

        pairs = duplicates.find_duplicates(
            primary_build.records,
            reference_records,
            params,
            on_progress=lambda done, total_pairs: self._emit("progress", done, total_pairs),
            should_cancel=lambda: self.cancelled,
        )

        if self.cancelled:
            self._emit("log", "Cancelled - partial results discarded.", "error")
            return
        if not pairs:
            self._emit("log", "No duplicates above the threshold.", "ok")
            return

        self._emit("results", pairs)
        self._emit("log", f"Found {len(pairs)} duplicate pair(s).", "ok")
        self._emit(
            "log",
            "Double-click a row to reveal the file. Export writes the full report.",
            "muted",
        )

    def export_csv(self) -> None:
        if not self._pairs:
            return
        default = self.app.paths.report_path("duplicate_report.csv")
        target = filedialog.asksaveasfilename(
            title="Save duplicate report",
            defaultextension=".csv",
            initialdir=str(default.parent),
            initialfile=default.name,
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(duplicates.ROW_FIELDS))
                writer.writeheader()
                for pair in self._pairs:
                    writer.writerow(pair.as_row())
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not write report: {exc}")
            return
        self.log.append(f"Report written to {target}", "ok")

    def _reveal_selected(self, _event=None) -> None:
        selection = self.results.selection()
        if not selection:
            return
        try:
            pair = self._pairs[int(selection[0])]
        except (ValueError, IndexError):
            return
        _reveal_in_explorer(pair.path_a)

    def on_job_start(self) -> None:
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.primary.set_enabled(False)
        self.reference.set_enabled(False)
        self.progress.configure(value=0)
        self.progress_label.configure(text="starting...")

    def on_job_finish(self) -> None:
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.primary.set_enabled(True)
        self.reference.set_enabled(True)
        self.app.refresh_status()

    def on_cancel_requested(self) -> None:
        self.progress_label.configure(text="cancelling...")

    def handle_event(self, kind: str, *payload) -> None:
        if kind == "log":
            message = payload[0]
            tag = payload[1] if len(payload) > 1 else None
            self.log.append(message, tag)
        elif kind == "phase":
            self.progress_label.configure(text=payload[0])
            self.progress.configure(value=0)
        elif kind == "progress":
            done, total = payload[0], payload[1]
            self.progress.configure(value=(done / total * 100) if total else 0)
            if total:
                self.progress_label.configure(text=f"{done:,}/{total:,}")
        elif kind == "results":
            self._show_pairs(payload[0])
        elif kind == "error":
            self.log.append(payload[0], "error")
            messagebox.showerror(APP_TITLE, payload[0])

    def _show_pairs(self, pairs: list) -> None:
        self._pairs = pairs
        self.results.delete(*self.results.get_children())
        for index, pair in enumerate(pairs):
            self.results.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    f"{pair.similarity * 100:.1f}%",
                    pair.filename_a,
                    pair.kbps_a,
                    pair.filename_b,
                    pair.kbps_b,
                ),
            )
        self.export_button.configure(state="normal" if pairs else "disabled")


class App(_RootBase):
    """The main window: header, notebook, status bar."""

    def __init__(self, initial_paths: list[str] | None = None) -> None:
        super().__init__()
        self.paths = AppPaths.default().ensure()
        self.settings = Settings.load(self.paths.settings_file)

        self.title(f"{APP_TITLE} {__version__}")
        self.geometry("1060x800")
        self.minsize(860, 620)

        icon = _asset("icon.ico")
        if icon is not None:
            try:
                self.iconbitmap(str(icon))
            except tk.TclError:
                pass

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(12, 10, 12, 4))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Seas Audio Tools", font=("Segoe UI", 14, "bold")).pack(
            side="left"
        )
        ttk.Label(
            header, text="drop folders in, pick a tool", foreground=COLOUR_MUTED
        ).pack(side="left", padx=(10, 0), pady=(4, 0))

        notebook = ttk.Notebook(self)
        notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 6))

        self.sanitize_tab = SanitizeTab(notebook, self)
        self.duplicates_tab = DuplicatesTab(notebook, self)
        notebook.add(self.sanitize_tab, text="  Sanitize tags  ")
        notebook.add(self.duplicates_tab, text="  Find duplicates  ")

        self.status = ttk.Label(
            self, text="", anchor="w", foreground=COLOUR_MUTED, padding=(12, 4)
        )
        self.status.grid(row=2, column=0, sticky="ew")

        if initial_paths:
            self.sanitize_tab.drop.add_paths(initial_paths)
            self.duplicates_tab.primary.add_paths(initial_paths)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self.refresh_status)

    def refresh_status(self) -> None:
        status = probe()
        if status.lower().startswith("fpcalc version"):
            self.status.configure(
                text=(
                    f"{status.splitlines()[0]}  |  cache: {self.paths.cache_dir}"
                    f"  |  drag-drop: {'on' if DND_AVAILABLE else 'off'}"
                ),
                foreground=COLOUR_MUTED,
            )
        else:
            self.status.configure(text=f"fpcalc problem: {status}", foreground=COLOUR_ERROR)

    def _on_close(self) -> None:
        self.settings.library_paths = [
            str(path) for path in self.duplicates_tab.primary.paths
        ]
        try:
            self.settings.save(self.paths.settings_file)
        except OSError:
            pass
        self.destroy()


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``seas-audio gui`` and the ``seas-audio-gui`` script."""
    if os.name == "nt":
        try:  # crisp text on high-DPI displays
            from ctypes import windll

            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    arguments = list(sys.argv[1:] if argv is None else argv)
    initial = [item for item in arguments if not item.startswith("-")]

    app = App(initial_paths=initial)
    if not DND_AVAILABLE:
        app.after(
            400,
            lambda: messagebox.showwarning(
                APP_TITLE,
                "tkinterdnd2 is not installed, so drag and drop is disabled.\n"
                "Install it with:  python -m pip install tkinterdnd2\n"
                "The Browse... buttons still work.",
            ),
        )
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())