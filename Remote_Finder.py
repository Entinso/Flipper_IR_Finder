#!/usr/bin/env python3
"""
Flipper IR Finder — IR Record Search Tool for Flipper Zero .ir files

Search recursively through selected directories for .ir files containing
button definitions. Supports hex-normalized matching, protocol-aware
short/long formats, and dual-button validation mode.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import platform
import queue
import re
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Iterator, NamedTuple

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *  # noqa: F401,F403
    _HAS_BOOTSTRAP = True
    # ttkbootstrap uses 'Panedwindow' (lowercase w); normalise to match tkinter.ttk
    if not hasattr(ttk, "PanedWindow") and hasattr(ttk, "Panedwindow"):
        ttk.PanedWindow = ttk.Panedwindow  # type: ignore[attr-defined]
except ImportError:
    from tkinter import ttk  # type: ignore[no-redef]
    _HAS_BOOTSTRAP = False

__version__ = "2.0.0"
APP_TITLE = "Flipper IR Finder"

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ============================================================================
# Constants and Enums
# ============================================================================

class ProtocolGroup(Enum):
    SHORT_1BYTE = auto()
    SHORT_2BYTE = auto()
    SHORT_3BYTE = auto()


PROTOCOL_GROUPS: dict[str, ProtocolGroup] = {
    "nec":       ProtocolGroup.SHORT_1BYTE,
    "samsung32": ProtocolGroup.SHORT_1BYTE,
    "rc5":       ProtocolGroup.SHORT_1BYTE,
    "rc5x":      ProtocolGroup.SHORT_1BYTE,
    "rc6":       ProtocolGroup.SHORT_1BYTE,
    "rca":       ProtocolGroup.SHORT_1BYTE,
    "sirc":      ProtocolGroup.SHORT_1BYTE,
    "sirc15":    ProtocolGroup.SHORT_1BYTE,
    "necext":    ProtocolGroup.SHORT_2BYTE,
    "nec42":     ProtocolGroup.SHORT_2BYTE,
    "sirc20":    ProtocolGroup.SHORT_2BYTE,
    "kaseikyo":  ProtocolGroup.SHORT_3BYTE,
    "nec42ext":  ProtocolGroup.SHORT_3BYTE,
}

SHORT_FORMAT_LENGTHS: dict[ProtocolGroup, tuple[int, int]] = {
    ProtocolGroup.SHORT_1BYTE: (2, 8),
    ProtocolGroup.SHORT_2BYTE: (4, 8),
    ProtocolGroup.SHORT_3BYTE: (6, 8),
}

COMMON_BUTTONS = (
    "All Buttons", "",
    "Power", "Vol_up", "Vol_dn", "Ch_next", "Ch_prev",
    "Up", "Down", "Right", "Left", "Ok", "Menu", "Setup",
    "Back", "Home", "Mute", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
)

SUPPORTED_PROTOCOLS = (
    "All Protocols", "",
    "NEC", "NECext", "NEC42", "NEC42ext", "Samsung32", "Kaseikyo",
    "RC5", "RC5X", "RC6", "RCA", "SIRC", "SIRC15", "SIRC20",
)

FILE_ENCODINGS = ("utf-8", "utf-16", "utf-16le", "utf-16be", "latin-1")

THEMES = ("cosmo", "flatly", "journal", "darkly", "superhero", "solar")
DEFAULT_THEME = "cosmo"

HELP_TEXT = f"""\
{APP_TITLE} v{__version__} — IR File Search Tool for Flipper Zero

USAGE
  1. Add one or more directories  (Ctrl+O)
  2. Enter search criteria — leave blank to match all
  3. Enable dual-button search for remote validation (optional)
  4. Click Search  (Ctrl+Enter) to scan
  5. Select a result file to see match details
  6. Double-click a result to open the file

KEYBOARD SHORTCUTS
  Ctrl+O      Add directory
  Ctrl+Enter  Start search
  Escape      Cancel search
  Ctrl+L      Clear results

FEATURES
  - Protocol-aware hex matching (short / long formats)
  - Dual-button search for remote validation
  - Line numbers for matched records
  - Recursive subdirectory scanning
  - Export results to a text file\
"""


# ============================================================================
# Data Classes
# ============================================================================

@dataclass(frozen=True)
class ButtonCriteria:
    name: str | None = None
    protocol: str | None = None
    address: str | None = None
    command: str | None = None

    @classmethod
    def from_inputs(cls, name: str, protocol: str, address: str, command: str) -> ButtonCriteria:
        return cls(
            name=None if not name or name.lower() == "all buttons" else name.strip(),
            protocol=None if not protocol or protocol.lower() == "all protocols" else protocol.strip(),
            address=address.strip() or None,
            command=command.strip() or None,
        )

    @property
    def has_criteria(self) -> bool:
        return any([self.name, self.protocol, self.address, self.command])


@dataclass
class SearchCriteria:
    button1: ButtonCriteria
    patterns: list[str] = field(default_factory=lambda: ["*.ir"])
    recursive: bool = True
    dual_search: bool = False
    button2: ButtonCriteria = field(default_factory=ButtonCriteria)


@dataclass
class IRRecord:
    name: str
    protocol: str = ""
    address: str = ""
    command: str = ""
    record_type: str = ""
    start_line: int = 0
    address_line: int = 0
    command_line: int = 0

    def matches(self, criteria: ButtonCriteria) -> bool:
        if criteria.name and self.name.lower() != criteria.name.lower():
            return False
        if criteria.protocol and self.protocol.lower() != criteria.protocol.lower():
            return False
        pg = PROTOCOL_GROUPS.get(self.protocol.lower())
        if criteria.address and not hex_matches(self.address, criteria.address, pg):
            return False
        if criteria.command and not hex_matches(self.command, criteria.command, pg):
            return False
        return True


class DetailedMatch(NamedTuple):
    name: str
    protocol: str
    address: str
    command: str
    address_line: int
    command_line: int
    record_start: int


@dataclass
class SearchUpdate:
    kind: str  # 'status' | 'progress' | 'result' | 'done' | 'error'
    payload: tuple


# ============================================================================
# Hex Matching
# ============================================================================

def normalize_hex(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    cleaned = re.sub(r"0x", "", value, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", cleaned)
    if not cleaned:
        return None
    if len(cleaned) % 2 == 1:
        cleaned = "0" + cleaned
    return cleaned.upper()


def hex_matches(file_hex: str | None, search_hex: str | None,
                protocol_group: ProtocolGroup | None = None) -> bool:
    if search_hex is None:
        return True
    file_norm = normalize_hex(file_hex)
    search_norm = normalize_hex(search_hex)
    if file_norm is None or search_norm is None:
        return False
    if file_norm == search_norm:
        return True
    if protocol_group and protocol_group in SHORT_FORMAT_LENGTHS:
        short_len, long_len = SHORT_FORMAT_LENGTHS[protocol_group]
        return _match_short_long(file_norm, search_norm, short_len, long_len)
    return False


def _match_short_long(file_norm: str, search_norm: str, short_len: int, long_len: int) -> bool:
    # Expand a short-format file value to its canonical long form (zero-pad on the right).
    # IR protocols store the significant byte first, so 0x01 short == 0x01000000 long.
    if len(file_norm) == short_len:
        file_norm = file_norm + "0" * (long_len - short_len)

    fl, sl = len(file_norm), len(search_norm)

    # Full-length (or over-length) search → require exact match against canonical value.
    if sl >= fl:
        return file_norm == search_norm

    # Partial search (user entered fewer bytes) → canonical file value must start with it.
    return file_norm.startswith(search_norm)


# ============================================================================
# File Parsing
# ============================================================================

KEY_VALUE_PATTERN = re.compile(r"^\s*(?:-\s*)?([A-Za-z0-9_\-]+)\s*:\s*(.*?)\s*$")


def read_file_text(path: Path) -> str | None:
    for enc in FILE_ENCODINGS:
        try:
            return path.read_text(encoding=enc, errors="strict")
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError as e:
            logger.warning("Error reading %s: %s", path, e)
            return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("Error reading %s: %s", path, e)
        return None


def parse_records(text: str, include_line_info: bool = False) -> list[IRRecord]:
    records: list[IRRecord] = []
    current: dict[str, str] = {}
    current_lines: dict[str, int] = {}
    record_start = 0

    def flush() -> None:
        nonlocal current, current_lines, record_start
        if "name" in current:
            record = IRRecord(
                name=current.get("name", ""),
                protocol=current.get("protocol", ""),
                address=current.get("address", ""),
                command=current.get("command", ""),
                record_type=current.get("type", ""),
            )
            if include_line_info:
                record.start_line = record_start
                record.address_line = current_lines.get("address", 0)
                record.command_line = current_lines.get("command", 0)
            records.append(record)
        current.clear()
        current_lines.clear()

    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            flush()
            continue
        m = KEY_VALUE_PATTERN.match(line)
        if not m:
            continue
        key = m.group(1).lower()
        value = m.group(2)
        if key == "name":
            flush()
            record_start = line_num
        current[key] = value
        if include_line_info:
            current_lines[key] = line_num

    flush()
    return records


def file_matches_criteria(records: list[IRRecord], criteria: SearchCriteria) -> bool:
    b1 = any(r.matches(criteria.button1) for r in records)
    if criteria.dual_search and criteria.button2.has_criteria:
        return b1 and any(r.matches(criteria.button2) for r in records)
    return b1


def find_detailed_matches(path: Path, criteria: SearchCriteria) -> list[DetailedMatch]:
    text = read_file_text(path)
    if not text:
        return []
    records = parse_records(text, include_line_info=True)
    matches: list[DetailedMatch] = []
    seen: set[tuple] = set()

    def add_matches(bc: ButtonCriteria) -> None:
        for rec in records:
            if rec.matches(bc):
                key = (rec.name, rec.protocol, rec.address, rec.command)
                if key not in seen:
                    seen.add(key)
                    matches.append(DetailedMatch(
                        name=rec.name,
                        protocol=rec.protocol or "N/A",
                        address=rec.address or "N/A",
                        command=rec.command or "N/A",
                        address_line=rec.address_line,
                        command_line=rec.command_line,
                        record_start=rec.start_line,
                    ))

    add_matches(criteria.button1)
    if criteria.dual_search and criteria.button2.has_criteria:
        add_matches(criteria.button2)
    return matches


# ============================================================================
# File Iterator
# ============================================================================

def iter_matching_files(directories: list[Path], patterns: list[str],
                        recursive: bool) -> Iterator[Path]:
    for directory in directories:
        if not directory.is_dir():
            continue
        try:
            it = directory.rglob("*") if recursive else directory.iterdir()
            for path in it:
                if path.is_file() and any(
                    fnmatch.fnmatch(path.name.lower(), p.lower()) for p in patterns
                ):
                    yield path
        except PermissionError as e:
            logger.warning("Permission denied: %s: %s", directory, e)
        except OSError as e:
            logger.warning("Error accessing %s: %s", directory, e)


# ============================================================================
# Background Worker
# ============================================================================

def worker_search(directories: list[Path], criteria: SearchCriteria,
                  out_queue: queue.Queue[SearchUpdate],
                  stop_event: threading.Event) -> None:
    try:
        files = list(iter_matching_files(directories, criteria.patterns, criteria.recursive))
        total = len(files)
        found_count = 0
        label = "dual-button" if criteria.dual_search else "single-button"

        out_queue.put(SearchUpdate("status", (f"Scanning {total} files ({label})...",)))
        out_queue.put(SearchUpdate("progress", (0, total)))

        for idx, path in enumerate(files, 1):
            if stop_event.is_set():
                out_queue.put(SearchUpdate("done", (found_count, total, True)))
                return

            out_queue.put(SearchUpdate("status", (f"[{idx}/{total}] {path.name}",)))
            out_queue.put(SearchUpdate("progress", (idx, total)))

            text = read_file_text(path)
            if not text:
                continue
            try:
                records = parse_records(text)
            except Exception as e:
                logger.warning("Error parsing %s: %s", path, e)
                continue

            if file_matches_criteria(records, criteria):
                found_count += 1
                out_queue.put(SearchUpdate("result", (str(path),)))

        out_queue.put(SearchUpdate("done", (found_count, total, False)))

    except Exception as e:
        logger.exception("Search worker error")
        out_queue.put(SearchUpdate("error", (str(e),)))
        out_queue.put(SearchUpdate("done", (0, 0, False)))


# ============================================================================
# Platform Utilities
# ============================================================================

def open_with_default_app(path: str) -> None:
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True)
    except Exception as e:
        messagebox.showerror("Open File", f"Could not open file:\n{e}")


# ============================================================================
# GUI Root Factory
# ============================================================================

def _create_root() -> tk.Tk:
    if _HAS_BOOTSTRAP:
        win = ttk.Window(themename=DEFAULT_THEME)  # type: ignore[attr-defined]
        win.title(APP_TITLE)
        win.geometry("1300x860")
        win.minsize(1050, 720)
        return win
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1300x860")
    root.minsize(1050, 720)
    return root


# ============================================================================
# GUI Application
# ============================================================================

class SearchApp:
    """Main application controller."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root

        self.selected_dirs: list[Path] = []
        self.current_criteria: SearchCriteria | None = None
        self.result_paths: dict[str, Path] = {}
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.msg_queue: queue.Queue[SearchUpdate] = queue.Queue()

        self._create_variables()
        self._build_menu()
        self._build_ui()
        self._setup_text_tags()
        self._bind_shortcuts()
        self._poll_queue()

    # -------------------------------------------------------------------------
    # Tkinter Variables
    # -------------------------------------------------------------------------

    def _create_variables(self) -> None:
        self.name_var = tk.StringVar()
        self.protocol_var = tk.StringVar()
        self.address_var = tk.StringVar()
        self.command_var = tk.StringVar()

        self.dual_search_var = tk.BooleanVar(value=False)
        self.name2_var = tk.StringVar()
        self.protocol2_var = tk.StringVar()
        self.address2_var = tk.StringVar()
        self.command2_var = tk.StringVar()

        self.recursive_var = tk.BooleanVar(value=True)
        self.ext_patterns_var = tk.StringVar(value="*.ir")

    # -------------------------------------------------------------------------
    # Menu Bar
    # -------------------------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Add Directory…\tCtrl+O", command=self.add_directory)
        file_menu.add_command(label="Clear Directories", command=self.clear_directories)
        file_menu.add_separator()
        file_menu.add_command(label="Export Results…", command=self._export_results)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        search_menu = tk.Menu(menubar, tearoff=0)
        search_menu.add_command(label="Search\tCtrl+Enter", command=self.start_search)
        search_menu.add_command(label="Cancel\tEsc", command=self.cancel_search)
        search_menu.add_separator()
        search_menu.add_command(label="Clear Results\tCtrl+L", command=self.clear_results)
        menubar.add_cascade(label="Search", menu=search_menu)

        if _HAS_BOOTSTRAP:
            view_menu = tk.Menu(menubar, tearoff=0)
            for theme in THEMES:
                view_menu.add_command(
                    label=theme.capitalize(),
                    command=lambda t=theme: self._apply_theme(t),
                )
            menubar.add_cascade(label="Theme", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="How to Use", command=self._show_help)
        help_menu.add_separator()
        help_menu.add_command(label=f"About {APP_TITLE}", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def _apply_theme(self, theme_name: str) -> None:
        if _HAS_BOOTSTRAP:
            ttk.Style(theme=theme_name)  # type: ignore[call-arg]

    # -------------------------------------------------------------------------
    # Main UI Layout
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 6}

        main_pw = ttk.PanedWindow(self.root, orient="horizontal")
        main_pw.pack(fill="both", expand=True, **pad)

        left = ttk.Frame(main_pw)
        right = ttk.Frame(main_pw)
        main_pw.add(left, weight=1)
        main_pw.add(right, weight=2)

        self._build_left_panel(left, pad)
        self._build_right_panel(right, pad)

        # Status bar
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom", padx=8, pady=(0, 6))

        self.progress_bar = ttk.Progressbar(bar, mode="determinate", length=200)
        self.progress_bar.pack(side="right", padx=(6, 0))

        self.status_lbl = ttk.Label(bar, text="Ready  •  Ctrl+O: Add Dir  •  Ctrl+Enter: Search",
                                    anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)

    # -------------------------------------------------------------------------
    # Left Panel
    # -------------------------------------------------------------------------

    def _build_left_panel(self, parent: ttk.Frame, pad: dict) -> None:
        crit_frame = ttk.LabelFrame(parent, text="Search Criteria")
        crit_frame.pack(fill="x", **pad)

        grid = ttk.Frame(crit_frame)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Primary Button", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        self._create_button_fields(grid, 1,
            self.name_var, self.protocol_var, self.address_var, self.command_var)

        dual_cb_kw = {"bootstyle": "round-toggle"} if _HAS_BOOTSTRAP else {}
        self.dual_cb = ttk.Checkbutton(
            grid, variable=self.dual_search_var,
            text="Enable dual-button search",
            command=self._toggle_dual_search,
            **dual_cb_kw,
        )
        self.dual_cb.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 4))

        self.second_frame = ttk.LabelFrame(grid, text="Second Button")
        self.second_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.second_frame.grid_remove()

        sg = ttk.Frame(self.second_frame)
        sg.pack(fill="x")
        sg.columnconfigure(1, weight=1)
        self.second_widgets = self._create_button_fields(
            sg, 0, self.name2_var, self.protocol2_var,
            self.address2_var, self.command2_var, enabled=False)

        ttk.Label(grid, text="File patterns:").grid(
            row=7, column=0, sticky="e", padx=(0, 5), pady=(6, 0))
        ttk.Entry(grid, textvariable=self.ext_patterns_var).grid(
            row=7, column=1, sticky="ew", padx=5, pady=(6, 0))

        # Directories
        dir_frame = ttk.LabelFrame(parent, text="Search Directories")
        dir_frame.pack(fill="both", expand=True, **pad)

        lb_frame = ttk.Frame(dir_frame)
        lb_frame.pack(fill="both", expand=True)

        self.dir_list = tk.Listbox(lb_frame, height=6, selectmode=tk.EXTENDED,
                                   activestyle="dotbox")
        sb = ttk.Scrollbar(lb_frame, orient="vertical", command=self.dir_list.yview)
        self.dir_list.configure(yscrollcommand=sb.set)
        self.dir_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        dir_btns = ttk.Frame(dir_frame)
        dir_btns.pack(fill="x", pady=(6, 0))
        btn_kw = {"bootstyle": "outline"} if _HAS_BOOTSTRAP else {}
        ttk.Button(dir_btns, text="Add…", command=self.add_directory, **btn_kw).pack(side="left")
        ttk.Button(dir_btns, text="Remove", command=self.remove_selected_dirs, **btn_kw).pack(side="left", padx=4)
        ttk.Button(dir_btns, text="Clear", command=self.clear_directories, **btn_kw).pack(side="left")

        # Action buttons
        actions = ttk.Frame(parent)
        actions.pack(fill="x", **pad)

        ttk.Checkbutton(actions, variable=self.recursive_var,
                        text="Include subdirectories").pack(anchor="w")

        btn_row = ttk.Frame(actions)
        btn_row.pack(fill="x", pady=(6, 0))

        search_kw = {"bootstyle": "primary"} if _HAS_BOOTSTRAP else {}
        cancel_kw = {"bootstyle": "danger-outline"} if _HAS_BOOTSTRAP else {}
        clear_kw  = {"bootstyle": "secondary-outline"} if _HAS_BOOTSTRAP else {}

        self.search_btn = ttk.Button(btn_row, text="Search", command=self.start_search, **search_kw)
        self.search_btn.pack(side="left")

        self.cancel_btn = ttk.Button(btn_row, text="Cancel", command=self.cancel_search,
                                     state="disabled", **cancel_kw)
        self.cancel_btn.pack(side="left", padx=4)

        ttk.Button(btn_row, text="Clear Results", command=self.clear_results, **clear_kw).pack(side="left")

    def _create_button_fields(
        self,
        parent: ttk.Frame,
        start_row: int,
        name_var: tk.StringVar,
        protocol_var: tk.StringVar,
        address_var: tk.StringVar,
        command_var: tk.StringVar,
        enabled: bool = True,
    ) -> list:
        widgets = []
        state = "normal" if enabled else "disabled"
        labels   = ["Button Name:", "Protocol:", "Address (hex):", "Command (hex):"]
        variables = [name_var, protocol_var, address_var, command_var]

        for i, (label, var) in enumerate(zip(labels, variables)):
            ttk.Label(parent, text=label).grid(
                row=start_row + i, column=0, sticky="e", padx=(0, 5), pady=2)
            if i == 0:
                w = ttk.Combobox(parent, textvariable=var, values=COMMON_BUTTONS, state=state)
            elif i == 1:
                w = ttk.Combobox(parent, textvariable=var, values=SUPPORTED_PROTOCOLS, state=state)
            else:
                w = ttk.Entry(parent, textvariable=var, state=state)
            w.grid(row=start_row + i, column=1, sticky="ew", padx=5, pady=2)
            widgets.append(w)

        return widgets

    # -------------------------------------------------------------------------
    # Right Panel
    # -------------------------------------------------------------------------

    def _build_right_panel(self, parent: ttk.Frame, pad: dict) -> None:
        results_frame = ttk.LabelFrame(parent, text="Search Results")
        results_frame.pack(fill="both", expand=True, **pad)

        rp = ttk.PanedWindow(results_frame, orient="vertical")
        rp.pack(fill="both", expand=True)

        # File list
        files_frame = ttk.Frame(rp)
        rp.add(files_frame, weight=1)

        hdr = ttk.Frame(files_frame)
        hdr.pack(fill="x", pady=(0, 4))
        ttk.Label(hdr, text="Matching files:").pack(side="left")
        count_kw = {"bootstyle": "secondary"} if _HAS_BOOTSTRAP else {}
        self.count_lbl = ttk.Label(hdr, text="0 found", **count_kw)
        self.count_lbl.pack(side="right")

        tree_frame = ttk.Frame(files_frame)
        tree_frame.pack(fill="both", expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.results_tree = ttk.Treeview(
            tree_frame, columns=("file", "folder"),
            show="headings", selectmode="browse", height=12,
        )
        self.results_tree.heading("file", text="File name")
        self.results_tree.heading("folder", text="Folder")
        self.results_tree.column("file", width=220, anchor="w", stretch=False)
        self.results_tree.column("folder", width=400, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=self.results_tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.results_tree.xview)
        self.results_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.results_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.results_tree.bind("<<TreeviewSelect>>", self._on_file_select)
        self.results_tree.bind("<Double-Button-1>", lambda e: self._open_selected())

        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Open File", command=self._open_selected)
        self.context_menu.add_command(label="Copy Path", command=self._copy_path)
        self.results_tree.bind("<Button-3>", self._show_context_menu)

        # Match details
        details_frame = ttk.Frame(rp)
        rp.add(details_frame, weight=1)

        det_hdr = ttk.Frame(details_frame)
        det_hdr.pack(fill="x", pady=(4, 4))
        ttk.Label(det_hdr, text="Match details:").pack(side="left")
        link_kw = {"bootstyle": "link"} if _HAS_BOOTSTRAP else {}
        ttk.Button(det_hdr, text="Copy Path", command=self._copy_path, **link_kw).pack(side="right")
        ttk.Button(det_hdr, text="Open File", command=self._open_selected, **link_kw).pack(side="right", padx=4)

        det_inner = ttk.Frame(details_frame)
        det_inner.pack(fill="both", expand=True)

        self.details_text = tk.Text(
            det_inner, wrap=tk.WORD, height=12,
            font=("Courier", 10), state="disabled", relief="flat", bd=1,
        )
        det_sb = ttk.Scrollbar(det_inner, orient="vertical", command=self.details_text.yview)
        self.details_text.configure(yscrollcommand=det_sb.set)
        self.details_text.pack(side="left", fill="both", expand=True)
        det_sb.pack(side="right", fill="y")

    def _setup_text_tags(self) -> None:
        self.details_text.tag_configure("header",
            font=("Courier", 10, "bold"), foreground="#1a6eaf")
        self.details_text.tag_configure("separator", foreground="#aaaaaa")
        self.details_text.tag_configure("section",
            font=("Courier", 10, "bold"), foreground="#2e7d32")
        self.details_text.tag_configure("match_num",
            font=("Courier", 10, "bold"))
        self.details_text.tag_configure("key",   foreground="#555555")
        self.details_text.tag_configure("value", foreground="#000000")
        self.details_text.tag_configure("line_info",
            foreground="#888888", font=("Courier", 9, "italic"))

    # -------------------------------------------------------------------------
    # Keyboard Shortcuts & Toggle
    # -------------------------------------------------------------------------

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-o>",      lambda e: self.add_directory())
        self.root.bind("<Control-Return>", lambda e: self.start_search())
        self.root.bind("<Escape>",         lambda e: self.cancel_search())
        self.root.bind("<Control-l>",      lambda e: self.clear_results())

    def _toggle_dual_search(self) -> None:
        enabled = self.dual_search_var.get()
        if enabled:
            self.second_frame.grid()
        else:
            self.second_frame.grid_remove()
            for v in (self.name2_var, self.protocol2_var, self.address2_var, self.command2_var):
                v.set("")
        state = "normal" if enabled else "disabled"
        for w in self.second_widgets:
            w["state"] = state

    # -------------------------------------------------------------------------
    # Directory Management
    # -------------------------------------------------------------------------

    def add_directory(self) -> None:
        path = filedialog.askdirectory(title="Select directory to search")
        if path:
            dp = Path(path)
            if dp not in self.selected_dirs:
                self.selected_dirs.append(dp)
                self.dir_list.insert("end", str(dp))

    def remove_selected_dirs(self) -> None:
        for idx in reversed(self.dir_list.curselection()):
            p = Path(self.dir_list.get(idx))
            self.dir_list.delete(idx)
            if p in self.selected_dirs:
                self.selected_dirs.remove(p)

    def clear_directories(self) -> None:
        self.selected_dirs.clear()
        self.dir_list.delete(0, "end")

    # -------------------------------------------------------------------------
    # Search Operations
    # -------------------------------------------------------------------------

    def start_search(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Search Running",
                                   "A search is already running. Cancel it first.")
            return
        if not self.selected_dirs:
            messagebox.showwarning("No Directories",
                                   "Please add at least one directory to search.")
            return

        pattern_text = self.ext_patterns_var.get().strip() or "*.ir"
        patterns = [p.strip() for p in pattern_text.split(";") if p.strip()] or ["*.ir"]

        button1 = ButtonCriteria.from_inputs(
            self.name_var.get(), self.protocol_var.get(),
            self.address_var.get(), self.command_var.get(),
        )
        button2 = ButtonCriteria.from_inputs(
            self.name2_var.get(), self.protocol2_var.get(),
            self.address2_var.get(), self.command2_var.get(),
        )
        dual = self.dual_search_var.get()

        if dual and not button1.has_criteria:
            messagebox.showwarning("Primary Criteria Required",
                                   "Specify criteria for the primary button when using dual search.")
            return
        if not dual and not button1.has_criteria:
            if not messagebox.askyesno("No Criteria",
                                       "No criteria set — this will return ALL matching files.\nContinue?"):
                return

        self.current_criteria = SearchCriteria(
            button1=button1, button2=button2,
            patterns=patterns, recursive=self.recursive_var.get(),
            dual_search=dual,
        )
        self.clear_results()
        self.stop_event.clear()
        self.progress_bar["value"] = 0
        self.progress_bar["maximum"] = 100
        self.search_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")

        self.worker = threading.Thread(
            target=worker_search,
            args=(self.selected_dirs, self.current_criteria, self.msg_queue, self.stop_event),
            daemon=True,
        )
        self.worker.start()

    def cancel_search(self) -> None:
        self.stop_event.set()
        self._set_status("Cancelling…")

    def clear_results(self) -> None:
        for iid in self.results_tree.get_children():
            self.results_tree.delete(iid)
        self.result_paths.clear()
        self.count_lbl.config(text="0 found")
        self._set_details_text("")

    # -------------------------------------------------------------------------
    # Result Display
    # -------------------------------------------------------------------------

    def _on_file_select(self, _event=None) -> None:
        sel = self.results_tree.selection()
        if not sel:
            self._set_details_text("")
            return
        path = self.result_paths.get(sel[0])
        if path:
            self._set_details_text("Loading match details…")
            threading.Thread(target=self._scan_details, args=(path,), daemon=True).start()

    def _scan_details(self, path: Path) -> None:
        if not self.current_criteria:
            self.root.after(0, lambda: self._set_details_text("No search criteria available."))
            return
        try:
            matches = find_detailed_matches(path, self.current_criteria)
            self.root.after(0, lambda: self._display_matches(matches, path))
        except Exception as e:
            self.root.after(0, lambda: self._set_details_text(f"Error: {e}"))

    def _display_matches(self, matches: list[DetailedMatch], path: Path) -> None:
        t = self.details_text
        t.config(state="normal")
        t.delete("1.0", tk.END)

        if not matches:
            t.insert("end", "No matching records found.")
            t.config(state="disabled")
            return

        t.insert("end", f"File: {path.name}\n", "header")
        t.insert("end", "=" * 60 + "\n", "separator")
        t.insert("end", "\n")

        criteria = self.current_criteria
        if criteria and criteria.dual_search and criteria.button2.has_criteria:
            primary, secondary = [], []
            for m in matches:
                mn = m.name.lower()
                if not criteria.button1.name or mn == criteria.button1.name.lower():
                    primary.append(m)
                elif not criteria.button2.name or mn == criteria.button2.name.lower():
                    secondary.append(m)
            if primary:
                t.insert("end", "PRIMARY BUTTON MATCHES:\n", "section")
                t.insert("end", "-" * 30 + "\n", "separator")
                for i, m in enumerate(primary, 1):
                    self._insert_match(t, m, i)
            if secondary:
                t.insert("end", "\nSECOND BUTTON MATCHES:\n", "section")
                t.insert("end", "-" * 30 + "\n", "separator")
                for i, m in enumerate(secondary, 1):
                    self._insert_match(t, m, i)
        else:
            for i, m in enumerate(matches, 1):
                self._insert_match(t, m, i)

        t.config(state="disabled")

    def _insert_match(self, t: tk.Text, m: DetailedMatch, num: int) -> None:
        t.insert("end", f"Match {num}:\n", "match_num")
        t.insert("end", "  name: ",     "key"); t.insert("end", f"{m.name}\n",     "value")
        t.insert("end", "  protocol: ", "key"); t.insert("end", f"{m.protocol}\n", "value")

        t.insert("end", "  address: ", "key")
        t.insert("end", m.address, "value")
        if m.address != "N/A" and m.address_line:
            t.insert("end", f"  — line {m.address_line}", "line_info")
        t.insert("end", "\n")

        t.insert("end", "  command: ", "key")
        t.insert("end", m.command, "value")
        if m.command != "N/A" and m.command_line:
            t.insert("end", f"  — line {m.command_line}", "line_info")
        t.insert("end", "\n\n")

    def _set_details_text(self, text: str) -> None:
        self.details_text.config(state="normal")
        self.details_text.delete("1.0", tk.END)
        if text:
            self.details_text.insert("1.0", text)
        self.details_text.config(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_lbl.config(text=text)

    # -------------------------------------------------------------------------
    # Context Menu / Actions
    # -------------------------------------------------------------------------

    def _show_context_menu(self, event) -> None:
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _open_selected(self) -> None:
        sel = self.results_tree.selection()
        if sel:
            path = self.result_paths.get(sel[0])
            if path and path.is_file():
                open_with_default_app(str(path))

    def _copy_path(self) -> None:
        sel = self.results_tree.selection()
        if sel:
            path = self.result_paths.get(sel[0])
            if path:
                self.root.clipboard_clear()
                self.root.clipboard_append(str(path))
                self._set_status("Path copied to clipboard.")

    def _export_results(self) -> None:
        paths = list(self.result_paths.values())
        if not paths:
            messagebox.showinfo("Export", "No results to export.")
            return
        out = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            title="Export Results",
        )
        if not out:
            return
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(f"Flipper IR Finder — Export ({len(paths)} files)\n")
                f.write("=" * 60 + "\n\n")
                for p in paths:
                    f.write(str(p) + "\n")
            self._set_status(f"Exported {len(paths)} path(s) to {Path(out).name}")
        except OSError as e:
            messagebox.showerror("Export Error", f"Could not save file:\n{e}")

    # -------------------------------------------------------------------------
    # Help / About
    # -------------------------------------------------------------------------

    def _show_help(self) -> None:
        messagebox.showinfo(f"{APP_TITLE} — Help", HELP_TEXT)

    def _show_about(self) -> None:
        ui_info = "ttkbootstrap" if _HAS_BOOTSTRAP else "tkinter (standard)"
        messagebox.showinfo(
            f"About {APP_TITLE}",
            f"{APP_TITLE} v{__version__}\n\n"
            f"IR record search tool for Flipper Zero .ir files.\n\n"
            f"UI: {ui_info}\n"
            f"Python {platform.python_version()} on {platform.system()}",
        )

    # -------------------------------------------------------------------------
    # Message Queue Polling
    # -------------------------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                self._handle_update(self.msg_queue.get_nowait())
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_queue)

    def _handle_update(self, msg: SearchUpdate) -> None:
        if msg.kind == "status":
            self._set_status(msg.payload[0])

        elif msg.kind == "progress":
            done, total = msg.payload
            if total > 0:
                self.progress_bar["value"] = (done / total) * 100

        elif msg.kind == "result":
            path = Path(msg.payload[0])
            iid = str(len(self.result_paths))
            self.result_paths[iid] = path
            self.results_tree.insert("", "end", iid=iid,
                                     values=(path.name, str(path.parent)))
            n = len(self.result_paths)
            self.count_lbl.config(text=f"{n} found")

        elif msg.kind == "done":
            found, total, cancelled = msg.payload
            n = f"{found} match{'es' if found != 1 else ''}"
            if cancelled:
                self._set_status(f"Cancelled — {n} found.")
            else:
                t = f"{total} file{'s' if total != 1 else ''}"
                self._set_status(f"Done — {n} in {t}.")
            self.progress_bar["value"] = 100 if not cancelled else self.progress_bar["value"]
            self.search_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")

        elif msg.kind == "error":
            messagebox.showerror("Search Error", f"An error occurred:\n{msg.payload[0]}")
            self._set_status("Search failed.")
            self.search_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")


# ============================================================================
# Entry Point
# ============================================================================

def main() -> None:
    root = _create_root()
    SearchApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
