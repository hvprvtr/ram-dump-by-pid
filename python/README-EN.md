# ram-pid-reader

Read process memory **by PID/name** via the WinPmem physical-memory dumper
driver, **without `OpenProcess`/`PROCESS_VM_READ`** in the tool's code.

Under the hood — `memprocfs` + `LeechCore`: LeechCore brings up the signed
WinPmem driver (device string `PMEM://<path to winpmem_x64.sys>`), and MemProcFS
does the `PID → CR3 → page-table walk` on top of physical reads. We just call
`read(va, size)`.

## Architecture

| Layer | File | Depends on Windows? |
|-------|------|---------------------|
| Pure logic (process selection, validation, hexdump, region walk for dumps) | `rampidreader/core.py` | no — tested with mocks |
| memprocfs/LeechCore adapter | `rampidreader/backend.py` | yes (lazy import) |
| CLI | `ram-dump-by-pid.py` | yes |

## Installation

On the target Win11 (as Administrator):

```powershell
pip install -r requirements.txt
```

## WinPmem driver

The `memprocfs` pip package **does not include** the WinPmem driver. Extract it
from `winpmem_mini` (the `-d` flag) and pass it to the tool via `--driver`:

```powershell
winpmem_mini_x64.exe -d C:\path\winpmem_x64.sys
```

LeechCore builds the device string `PMEM://C:\path\winpmem_x64.sys` from it.

## Usage

```powershell
# hex dump of 256 bytes at a virtual address
python ram-dump-by-pid.py --name notepad.exe --addr 0x7ff600000000 --size 256 --driver C:\path\winpmem_x64.sys

# from a module base + offset, raw bytes to a file
python ram-dump-by-pid.py --pid 1234 --module ntdll.dll --offset 0x1000 --size 4096 --out dump.bin --driver C:\path\winpmem_x64.sys

# address-space region map (VAD)
python ram-dump-by-pid.py --pid 1234 --vads --driver C:\path\winpmem_x64.sys

# FULL process dump — only --pid/--name and --out are needed
python ram-dump-by-pid.py --pid 1234 --dump-all --out proc.bin --driver C:\path\winpmem_x64.sys

# advanced: raw LeechCore device string (FPGA, dump file, etc.)
python ram-dump-by-pid.py --pid 1234 --vads --device FPGA
```

### Full process dump (`--dump-all`)

Walks all process regions (VAD) and writes readable memory to `--out`. Nothing
needs to be computed in advance — only a PID (or name) and a file. A map
`<out>.map` is created next to it, linking virtual addresses to the file:

```
# va length file_offset
0x00007ff7019c0000 65536 0
0x00007ff7019d0000 4096 65536
...
```

Before reading each region, the first page is "probed": if it is inaccessible,
the region (usually reserved/uncommitted memory — sometimes several gigabytes)
is skipped entirely, without chunk-by-chunk iteration. This guards against
hanging during a dump.

The region itself is read in chunks. The backend stops reading at the first
unallocated page and returns less than requested, so the cursor advances by the
number of bytes **actually read**, and when it hits a hole — by one page. This
way committed data behind holes is not lost (important: otherwise only the start
of large fragmented regions would be saved). Holes do not end up in `.bin` — the
file contains only memory that was actually present, and the `.map` stores the
exact addresses of each block. At the end a summary is printed: how many regions
were dumped fully, partially, and skipped.

Exit codes: `0` — success, `1` — read/process error, `2` — argument error.

> Verified on Win11 build 26200 (winpmem 2.0.1): byte-for-byte module reads match
> the OS reference dump (procdump), and the full `--dump-all` dump was taken from
> several processes (notepad, calc, powershell).

## Tests

Run on any OS, no driver needed (all mocked), including angry tests:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Environment requirements

* Run as Administrator (driver loading).
* The WinPmem driver must load (on Win11 with HVCI this is the main filter).
* `--driver <winpmem_x64.sys>` builds the device string `PMEM://<path>`, from
  which LeechCore brings up its own copy of the WinPmem driver (bare `pmem` does
  not work).
