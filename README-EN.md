# ram-pid-reader

Read process memory **by PID/name** via the WinPmem physical-memory dumper
driver + MemProcFS/LeechCore, **without `OpenProcess`/`PROCESS_VM_READ`** in the
tool's code. LeechCore brings up the signed WinPmem driver, and MemProcFS does
the `PID → CR3 → page-table walk` on top of physical reads — the tool merely
calls `read(va, size)`.

The repository contains two independent implementations with identical logic,
CLI flags, `.map` format, and exit codes (`0/1/2`):

| Implementation | Directory | Runnable artifact |
|----------------|-----------|-------------------|
| Python | [`python/`](python/) | `ram-dump-by-pid.py` |
| Rust | [`rust/`](rust/) | `ram-dump-by-pid` (`.exe`) |

Build, test, and usage details are in the README of the respective directory:
[`python/README.md`](python/README.md), [`rust/README.md`](rust/README.md)
(English: [`python/README-EN.md`](python/README-EN.md),
[`rust/README-EN.md`](rust/README-EN.md)).
