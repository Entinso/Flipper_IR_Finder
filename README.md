# Flipper IR Finder

**IR record search tool for Flipper Zero `.ir` files.**

Search recursively through directories to find IR remote files that match specific button names, protocols, addresses, or commands. Supports protocol-aware hex matching and dual-button validation mode for verifying complete remote compatibility.

---

## Features

- **Smart hex matching** — compare addresses and commands in short or long format (e.g. `FF` matches `FF000000` for NEC)
- **Protocol-aware** — 13 protocols supported with correct byte-width rules
- **Dual-button search** — find files that contain *both* buttons (e.g. Power + Vol_up) to validate complete remotes
- **Detailed match view** — shows field values and exact line numbers in the file
- **Modern UI** — Bootstrap-themed interface via [ttkbootstrap](https://ttkbootstrap.readthedocs.io/) with live progress bar and result counter
- **Theme switcher** — 6 built-in themes (Cosmo, Flatly, Journal, Darkly, Superhero, Solar)
- **Export results** — save matched file paths to a `.txt` file
- **Cross-platform** — Windows, macOS, Linux

---

## Screenshot

<img width="1302" height="912" alt="Sample_IR_Finder" src="https://github.com/user-attachments/assets/23aebcef-869e-4ae0-b88d-0334e625b847" />

---

## Requirements

- Python 3.10+
- [ttkbootstrap](https://pypi.org/project/ttkbootstrap/) *(optional — falls back to standard tkinter if not installed)*

---

## Installation

```bash
# Clone the repository
git clone https://github.com/Entinso/Flipper_IR_Finder.git
cd Flipper_IR_Finder

# Install the optional modern UI library
pip install ttkbootstrap

# Run
python Flipper_ir_finder.py
```

No build step required. The tool runs directly from a single Python file.

---

## Usage

### Basic search

1. Click **Add…** (or `Ctrl+O`) to add one or more directories to scan
2. Fill in any combination of **Button Name**, **Protocol**, **Address**, or **Command** — leave blank to match all
3. Click **Search** (or `Ctrl+Enter`)
4. Select a result to see match details; double-click to open the file

### Dual-button search

Enable **"Enable dual-button search"** to find files that contain *both* buttons. This is useful for validating that a remote supports two specific functions (e.g. Power + Mute).

### Hex input formats

Address and command fields accept hex in any of these formats — they are normalized automatically:

| Input | Normalized |
|-------|-----------|
| `FF`  | `FF`      |
| `0xFF` | `FF`   |
| `FF 00 00 00` | `FF000000` |
| `0xFF000000`  | `FF000000` |


### Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+O` | Add directory |
| `Ctrl+Enter` | Start search |
| `Escape` | Cancel search |
| `Ctrl+L` | Clear results |

---

## Supported IR Protocols

| Protocol | Short format width |
|----------|--------------------|
| NEC, Samsung32, RC5, RC5X, RC6, RCA, SIRC, SIRC15 | 1 byte (e.g. `FF`) |
| NECext, NEC42, SIRC20 | 2 bytes (e.g. `FF00`) |
| Kaseikyo, NEC42ext | 3 bytes (e.g. `FF0000`) |

---

## Thanks To
- [Flipper-IRDB](https://github.com/logickworkshop/Flipper-IRDB) by logickworkshop

For the database.