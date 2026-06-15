# ram-dumper-by-pid (Rust)

A Rust port of the Python tool on top of the official
[`memprocfs`](https://crates.io/crates/memprocfs) crate (same author as
MemProcFS). Reads process memory **by PID/name** via winpmem + MemProcFS/LeechCore,
**without `OpenProcess`** — logic 1:1 with the Python version in `../python/`.

## Structure

| Layer | File | Depends on native libraries? |
|-------|------|------------------------------|
| Pure logic + tests | `src/lib.rs` | no — mock tests on any OS |
| memprocfs adapter | `src/backend.rs` | yes (`backend` feature) |
| CLI | `src/main.rs` | no external deps (argv parsed by hand) |

`lib.rs` knows nothing about memprocfs (the `ProcessLike` trait), so `cargo test`
runs all the logic without a driver or native dlls.

## Tests (pure logic)

```bash
cargo test          # 46 tests: regular + angry (Angry Tests)
```

Some of the Python "angry" tests (negative address/size, bool, None, read
exceptions) are impossible at the Rust type level — eliminated by design
(unsigned types, `Option` instead of exceptions).

## Building the binary (with the backend)

The binary reads real memory only with the `backend` feature:

```bash
cargo build --release --features backend
```

### Toolchain (important)

`backend` pulls in `memprocfs` → `libloading` → `windows-sys`, which needs
`dlltool` at build time (generating import libraries). Environment options:

* **MSVC** — install Build Tools for Visual Studio (provides `link.exe`).
* **GNU** (used during development) — self-contained linker, but needs
  `dlltool` + the `as` assembler from MinGW-w64 binutils:
  ```
  rustup default stable-x86_64-pc-windows-gnu
  # binutils: e.g. via MSYS2 → C:\msys64\mingw64\bin in PATH
  ```

## Running

The `memprocfs` crate loads the native `vmm.dll` and `leechcore.dll` (next to
each other). Take them from the official MemProcFS release (the `memprocfs` pip
package has no separate `leechcore.dll`).

`--driver` and `--vmm` are optional: by default `winpmem_x64.sys` and `vmm.dll`
are looked up next to the binary (`current_exe`) — just put the exe and dlls in
one folder and run from there. An explicit path, if given, is checked for
existence. The Microsoft Symbol Server is disabled in the binary
(`-disable-symbolserver`), so no symbol EULA appears and no internet is needed.

```powershell
# full process dump (driver and vmm.dll — from the folder next to the exe)
ram-dump-by-pid.exe --pid 1234 --dump-all --out proc.bin

# region map / point read — same as the Python version
ram-dump-by-pid.exe --name notepad.exe --vads
ram-dump-by-pid.exe --pid 1234 --module ntdll.dll --offset 0 --size 4096 --out h.bin

# with explicit paths, if the dlls are not next to the exe
ram-dump-by-pid.exe --pid 1234 --dump-all --out proc.bin `
           --driver C:\path\winpmem_x64.sys --vmm C:\path\MemProcFS\vmm.dll
```

The flags (`--pid/--name/--addr/--module/--offset/--size/--vads/--dump-all/--out`),
the `.map` format, and the exit codes (`0/1/2`) match the Python version.

## Verified

On Win11 build 26200, live processes (notepad/calc/powershell):

* a module read through Rust is **byte-for-byte** identical to the Python version
  and the OS reference (procdump) — same SHA256;
* `--dump-all` produces the same size as Python (188.86 MB for notepad at one
  moment), with 100% coverage of the `ntdll` image.

> A subtlety found during porting: the crate's `Vmm::mem_read_ex` returns a
> full-size buffer, **ignoring** the number of bytes actually read (holes = zeros),
> which inflates the dump to gigabytes. `mem_read_into` is used instead, returning
> the actual `cb_read` — and that gives correct skipping of holes/reserved memory.
