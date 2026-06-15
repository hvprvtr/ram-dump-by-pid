"""Тесты CLI-обвязки с подменой бэкенда (без Windows/драйвера)."""

import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Имя файла с дефисом нельзя подключить через `import` — грузим по пути.
_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ram-dump-by-pid.py"
)
_spec = importlib.util.spec_from_file_location("ram_dump_by_pid", _SCRIPT)
reader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reader)

from rampidreader.core import Vad  # noqa: E402


class FakeProc:
    def __init__(self, pid, name, blob=b"", modules=None, vads=None):
        self.pid = pid
        self.name = name
        self._blob = blob
        self._modules = modules or {}
        self._vads = vads or []

    def read(self, addr, size):
        return self._blob[:size] if self._blob else None

    def module_base(self, m):
        return self._modules.get(m)

    def vads(self):
        return list(self._vads)


class FakeBackend:
    """Подмена MemProcFSBackend: возвращает заранее заданные процессы."""

    procs = []
    last_device = None

    def __init__(self, device):
        self.device = device
        FakeBackend.last_device = device

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def processes(self):
        return list(FakeBackend.procs)


@pytest.fixture(autouse=True)
def patch_backend(monkeypatch):
    monkeypatch.setattr(reader, "MemProcFSBackend", FakeBackend)
    FakeBackend.procs = [FakeProc(1234, "notepad.exe", blob=b"ABCD",
                                  modules={"ntdll.dll": 0x7FF000000000})]
    yield


# --------------------------------------------------------------------------- #
# Обычные
# --------------------------------------------------------------------------- #
DRV = ["--driver", "winpmem_x64.sys"]


def test_read_by_pid_hexdump(capsys):
    rc = reader.run(["--pid", "1234", "--addr", "0x1000", "--size", "4", *DRV])
    out = capsys.readouterr().out
    assert rc == 0
    assert "41 42 43 44" in out


def test_driver_builds_pmem_device_string():
    reader.run(["--pid", "1234", "--addr", "0x1000", "--size", "4", *DRV])
    assert FakeBackend.last_device == "PMEM://winpmem_x64.sys"


def test_raw_device_overrides_driver():
    reader.run(["--pid", "1234", "--addr", "0x1000", "--size", "4",
                "--device", "FPGA", *DRV])
    assert FakeBackend.last_device == "FPGA"


def test_read_by_name(capsys):
    rc = reader.run(["--name", "notepad.exe", "--addr", "0x1000", "--size", "4", *DRV])
    assert rc == 0
    assert "41 42 43 44" in capsys.readouterr().out


def test_read_from_module(capsys):
    rc = reader.run(["--pid", "1234", "--module", "ntdll.dll", "--size", "2", *DRV])
    assert rc == 0


def test_vads_mode(capsys):
    FakeBackend.procs = [FakeProc(1234, "notepad.exe",
                                  vads=[Vad(0x1000, 0x2000, "rw-", "Heap")])]
    rc = reader.run(["--pid", "1234", "--vads", *DRV])
    assert rc == 0
    assert "Heap" in capsys.readouterr().out


def test_out_file(tmp_path, capsys):
    dst = tmp_path / "dump.bin"
    rc = reader.run(["--pid", "1234", "--addr", "0x1000", "--size", "4",
                     "--out", str(dst), *DRV])
    assert rc == 0
    assert dst.read_bytes() == b"ABCD"


# --------------------------------------------------------------------------- #
# --dump-all — обычные
# --------------------------------------------------------------------------- #
def test_dump_all_writes_bin_and_map(tmp_path):
    FakeBackend.procs = [FakeProc(1234, "notepad.exe", blob=b"ABCDEFGHIJKLMNOP",
                                  vads=[Vad(0x1000, 0x1010, "rw-", "Heap")])]
    dst = tmp_path / "full.bin"
    rc = reader.run(["--pid", "1234", "--dump-all", "--out", str(dst), *DRV])
    assert rc == 0
    assert dst.read_bytes() == b"ABCDEFGHIJKLMNOP"
    mp = (tmp_path / "full.bin.map").read_text(encoding="utf-8")
    assert "pid=1234" in mp
    assert "0x0000000000001000 16 0" in mp  # va length file_offset


def test_dump_all_needs_only_pid_and_out(tmp_path):
    # Ни --size, ни --addr/--module не требуются.
    FakeBackend.procs = [FakeProc(1234, "notepad.exe", blob=b"XYZ",
                                  vads=[Vad(0x2000, 0x2003, "r--", "x")])]
    dst = tmp_path / "d.bin"
    rc = reader.run(["--pid", "1234", "--dump-all", "--out", str(dst), *DRV])
    assert rc == 0
    assert dst.read_bytes() == b"XYZ"


def test_dump_all_concatenates_multiple_regions(tmp_path):
    FakeBackend.procs = [FakeProc(1234, "notepad.exe", blob=b"AAAA",
                                  vads=[Vad(0x1000, 0x1004, "rw-", "a"),
                                        Vad(0x5000, 0x5004, "rw-", "b")])]
    dst = tmp_path / "m.bin"
    rc = reader.run(["--pid", "1234", "--dump-all", "--out", str(dst), *DRV])
    assert rc == 0
    # два региона по 4 байта → 8 байт в файле, две записи в карте
    assert len(dst.read_bytes()) == 8
    lines = [l for l in (tmp_path / "m.bin.map").read_text(encoding="utf-8").splitlines()
             if not l.startswith("#")]
    assert len(lines) == 2
    assert lines[1].endswith(" 4 4")  # второй блок: length=4, file_offset=4


# --------------------------------------------------------------------------- #
# --dump-all — злые
# --------------------------------------------------------------------------- #
def test_dump_all_without_out_returns_2(capsys):
    rc = reader.run(["--pid", "1234", "--dump-all", *DRV])
    assert rc == 2
    assert "out" in capsys.readouterr().err


def test_dump_all_empty_process_writes_empty_file(tmp_path):
    # Нет регионов вообще → пустой .bin, rc 0, не падаем.
    FakeBackend.procs = [FakeProc(1234, "notepad.exe", vads=[])]
    dst = tmp_path / "empty.bin"
    rc = reader.run(["--pid", "1234", "--dump-all", "--out", str(dst), *DRV])
    assert rc == 0
    assert dst.read_bytes() == b""


def test_dump_all_unreadable_regions_skipped(tmp_path, capsys):
    # Регионы есть, но read возвращает None (blob пуст) → пустой дамп, rc 0.
    FakeBackend.procs = [FakeProc(1234, "notepad.exe", blob=b"",
                                  vads=[Vad(0x1000, 0x1010, "rw-", "gone")])]
    dst = tmp_path / "skip.bin"
    rc = reader.run(["--pid", "1234", "--dump-all", "--out", str(dst), *DRV])
    assert rc == 0
    assert dst.read_bytes() == b""
    assert "пропущено 1" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Злые
# --------------------------------------------------------------------------- #
def test_missing_target_exits_2():
    with pytest.raises(SystemExit) as e:  # argparse: required mutually exclusive group
        reader.run(["--addr", "0x1000", "--size", "4"])
    assert e.value.code == 2


def test_both_pid_and_name_exits_2():
    with pytest.raises(SystemExit) as e:
        reader.run(["--pid", "1", "--name", "x.exe", "--size", "4"])
    assert e.value.code == 2


def test_read_without_size_returns_2(capsys):
    rc = reader.run(["--pid", "1234", "--addr", "0x1000"])
    assert rc == 2
    assert "size" in capsys.readouterr().err


def test_read_without_addr_or_module_returns_2(capsys):
    rc = reader.run(["--pid", "1234", "--size", "16"])
    assert rc == 2
    assert "addr" in capsys.readouterr().err


def test_bad_pid_returns_2(capsys):
    rc = reader.run(["--pid", "notanumber", "--addr", "0x1000", "--size", "4"])
    assert rc == 2


def test_pid_not_found_returns_1(capsys):
    rc = reader.run(["--pid", "9999", "--addr", "0x1000", "--size", "4", *DRV])
    assert rc == 1
    assert "не найден" in capsys.readouterr().err


def test_unmapped_address_returns_1(capsys):
    FakeBackend.procs = [FakeProc(1234, "notepad.exe", blob=b"")]  # read -> None
    rc = reader.run(["--pid", "1234", "--addr", "0xDEAD0000", "--size", "16", *DRV])
    assert rc == 1
    assert "недоступен" in capsys.readouterr().err


def test_no_driver_and_no_device_returns_2(capsys):
    rc = reader.run(["--pid", "1234", "--addr", "0x1000", "--size", "4"])
    assert rc == 2
    assert "driver" in capsys.readouterr().err
