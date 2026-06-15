#!/usr/bin/env python3
"""CLI: чтение памяти процесса по PID/имени через драйвер дампера физпамяти.

Примеры:
    # hex-дамп 256 байт по адресу
    python3 ramreader-by-pid.py --name notepad.exe --addr 0x7ff6_0000_0000 --size 256

    # чтение от базы модуля + смещение, в файл
    python3 ramreader-by-pid.py --pid 1234 --module ntdll.dll --offset 0x1000 --size 4096 --out dump.bin

    # перечислить регионы адресного пространства
    python3 ramreader-by-pid.py --pid 1234 --vads
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from rampidreader import core
from rampidreader.backend import MemProcFSBackend


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ramreader-by-pid.py",
        description="Чтение памяти процесса по PID через драйвер физпамяти (winpmem).",
    )

    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--pid", type=str, help="PID процесса (dec или 0x..)")
    target.add_argument("--name", type=str, help="Имя процесса, напр. notepad.exe")

    # Источник физпамяти. Для winpmem нужен путь к драйверу: PMEM://<winpmem_x64.sys>.
    # Драйвер достаётся из winpmem_mini:  winpmem_mini_x64.exe -d C:\path\winpmem_x64.sys
    p.add_argument("--driver", type=str, help="Путь к драйверу winpmem (.sys) — соберёт PMEM://<path>")
    p.add_argument("--device", type=str, help="Сырая device-строка LeechCore (переопределяет --driver)")

    # Что читать.
    p.add_argument("--addr", type=str, help="Виртуальный адрес (dec или 0x..)")
    p.add_argument("--module", type=str, help="Имя модуля — читать от его базы")
    p.add_argument("--offset", type=str, default="0", help="Смещение от адреса/базы модуля")
    p.add_argument("--size", type=str, help="Сколько байт прочитать")

    # Режим перечисления регионов вместо чтения.
    p.add_argument("--vads", action="store_true", help="Показать регионы (VAD) и выйти")

    # Режим полного дампа: только --pid/--name и --out, всё остальное вычисляется само.
    p.add_argument(
        "--dump-all",
        action="store_true",
        help="Снять полный дамп процесса (все регионы) в --out; рядом пишется <out>.map",
    )

    p.add_argument("--out", type=str, help="Сохранить сырые байты в файл вместо hex-вывода")
    return p


def _dump_all(proc, out_path: str) -> int:
    """Снять полный дамп процесса в ``out_path`` + карту регионов в ``out_path + '.map'``.

    В .bin идут сырые байты всех читаемых блоков подряд; .map связывает
    виртуальный адрес каждого блока с его смещением в файле:
        <va> <length> <file_offset>
    """
    map_path = out_path + ".map"
    file_off = 0

    with open(out_path, "wb") as f, open(map_path, "w", encoding="utf-8") as mf:
        mf.write(f"# дамп процесса pid={proc.pid} name={proc.name}\n")
        mf.write("# va length file_offset\n")

        def writer(va: int, data: bytes) -> None:
            nonlocal file_off
            f.write(data)
            mf.write(f"{va:#018x} {len(data)} {file_off}\n")
            file_off += len(data)

        entries = core.dump_process(proc, writer)

    complete = sum(1 for e in entries if e.complete)
    skipped = sum(1 for e in entries if e.skipped)
    partial = len(entries) - complete - skipped
    print(
        f"[+] дамп: регионов {len(entries)} — целиком {complete}, "
        f"частично {partial}, пропущено {skipped}",
        file=sys.stderr,
    )
    print(
        f"[+] записано {file_off} байт в {out_path} (карта: {map_path})",
        file=sys.stderr,
    )
    return 0


def run(argv: List[str]) -> int:
    args = build_parser().parse_args(argv)

    try:
        pid: Optional[int] = core.parse_int(args.pid) if args.pid is not None else None
        name: Optional[str] = args.name
        offset = core.parse_int(args.offset)
        addr = core.parse_int(args.addr) if args.addr is not None else None
        size = core.parse_int(args.size) if args.size is not None else None
    except ValueError as e:
        print(f"ошибка аргументов: {e}", file=sys.stderr)
        return 2

    if args.dump_all:
        if not args.out:
            print("ошибка: для --dump-all нужен --out <файл>", file=sys.stderr)
            return 2
    elif not args.vads:
        if size is None:
            print("ошибка: для чтения нужен --size (или используйте --vads)", file=sys.stderr)
            return 2
        if addr is None and args.module is None:
            print("ошибка: укажите --addr или --module", file=sys.stderr)
            return 2

    # Сборка device-строки LeechCore.
    if args.device:
        device = args.device
    elif args.driver:
        device = f"PMEM://{args.driver}"
    else:
        print("ошибка: укажите --driver <winpmem_x64.sys> или --device <строка>", file=sys.stderr)
        return 2

    try:
        with MemProcFSBackend(device=device) as backend:
            proc = core.select_process(backend.processes(), pid=pid, name=name)
            print(f"[+] процесс: pid={proc.pid} name={proc.name}", file=sys.stderr)

            if args.vads:
                print(core.format_vads(proc.vads()))
                return 0

            if args.dump_all:
                return _dump_all(proc, args.out)

            target_addr = core.resolve_address(
                proc, addr=addr, module=args.module, offset=offset
            )
            data = core.read_region(proc, target_addr, size)

            if len(data) < size:
                print(
                    f"[!] частичное чтение: получено {len(data)} из {size} байт",
                    file=sys.stderr,
                )

            if args.out:
                with open(args.out, "wb") as f:
                    f.write(data)
                print(f"[+] записано {len(data)} байт в {args.out}", file=sys.stderr)
            else:
                print(core.format_hexdump(data, target_addr))
        return 0
    except (ValueError, RuntimeError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ошибка ввода-вывода: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
