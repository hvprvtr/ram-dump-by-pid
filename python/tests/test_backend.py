"""Тесты адаптера memprocfs на фейковом модуле memprocfs (без Windows).

Проверяем, что обёртка корректно достаёт данные, переживает разные имена полей
VAD и не падает, а гасит исключения нижнего слоя в None/[] (злые случаи).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rampidreader.backend import MemProcFSBackend, MemProcFSProcess, _first_attr  # noqa: E402


class FakeMemory:
    def __init__(self, blob=b"", raise_exc=None):
        self._blob = blob
        self._raise = raise_exc

    def read(self, addr, size, flag):
        if self._raise:
            raise self._raise
        return self._blob[:size]


class FakeModule:
    def __init__(self, base):
        self.base = base


class FakeRawProc:
    def __init__(self, pid=7, name="x.exe", memory=None, modules=None, vad=None,
                 module_raises=False, vad_raises=False):
        self.pid = pid
        self.name = name
        self.memory = memory or FakeMemory()
        self._modules = modules or {}
        self._vad = vad or []
        self._module_raises = module_raises
        self._vad_raises = vad_raises

    def module(self, name):
        if self._module_raises:
            raise RuntimeError("boom")
        return self._modules.get(name)

    class _Maps:
        def __init__(self, outer):
            self._outer = outer

        def vad(self):
            if self._outer._vad_raises:
                raise RuntimeError("boom")
            return self._outer._vad

    @property
    def maps(self):
        return FakeRawProc._Maps(self)


class FakeMemprocfsModule:
    FLAG_NOCACHE = 0x4


class FakeVmm:
    """Перехватывает argv, переданный в memprocfs.Vmm(...)."""

    last_args = None

    def __init__(self, args):
        FakeVmm.last_args = list(args)

    def process_list(self):
        return []

    def close(self):
        pass


class FakeMemprocfsCapture(FakeMemprocfsModule):
    Vmm = FakeVmm


@pytest.fixture
def fake_memprocfs(monkeypatch):
    """Подменяет import memprocfs фейком, перехватывающим argv Vmm."""
    FakeVmm.last_args = None
    monkeypatch.setitem(sys.modules, "memprocfs", FakeMemprocfsCapture)
    return FakeMemprocfsCapture


# --------------------------------------------------------------------------- #
# open() — argv для Vmm: символьный сервер должен быть отключён
# --------------------------------------------------------------------------- #
def test_open_passes_disable_symbolserver(fake_memprocfs):
    with MemProcFSBackend(device="PMEM://drv.sys"):
        pass
    assert "-disable-symbolserver" in FakeVmm.last_args


def test_open_keeps_device_and_order(fake_memprocfs):
    with MemProcFSBackend(device="PMEM://drv.sys"):
        pass
    args = FakeVmm.last_args
    # -device идёт со своим значением, флаг отключения сервера — отдельным токеном.
    assert args[:2] == ["-device", "PMEM://drv.sys"]
    assert "-disable-symbolserver" in args[2:]


def test_open_extra_args_after_flag(fake_memprocfs):
    """Злой случай: extra_args не вытесняют и не дублируют служебный флаг."""
    with MemProcFSBackend(device="dev", extra_args=["-disable-symbolserver", "-v"]):
        pass
    args = FakeVmm.last_args
    # Наш флаг присутствует; пользовательский дубль допустим, но порядок наш сохранён:
    # device, наш флаг, затем extra_args.
    assert args[0:2] == ["-device", "dev"]
    assert args[2] == "-disable-symbolserver"
    assert args[3:] == ["-disable-symbolserver", "-v"]


def test_open_failure_wrapped_as_runtimeerror(monkeypatch):
    """Злой случай: падение Vmm(...) превращается в понятный RuntimeError."""
    class Boom(FakeMemprocfsModule):
        class Vmm:
            def __init__(self, args):
                raise OSError("driver not loaded")

    monkeypatch.setitem(sys.modules, "memprocfs", Boom)
    with pytest.raises(RuntimeError, match="не удалось инициализировать LeechCore"):
        MemProcFSBackend(device="dev").open()


# --------------------------------------------------------------------------- #
# _first_attr
# --------------------------------------------------------------------------- #
def test_first_attr_object():
    class O:
        b = 5
    assert _first_attr(O(), "a", "b", default=0) == 5


def test_first_attr_dict():
    assert _first_attr({"end": 9}, "va_end", "end", default=0) == 9


def test_first_attr_default_when_missing():
    assert _first_attr({}, "x", "y", default=-1) == -1


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_read_returns_bytes():
    raw = FakeRawProc(memory=FakeMemory(b"\x01\x02\x03"))
    p = MemProcFSProcess(FakeMemprocfsModule(), raw)
    assert p.read(0x1000, 3) == b"\x01\x02\x03"


def test_read_swallows_exception_as_none():
    raw = FakeRawProc(memory=FakeMemory(raise_exc=RuntimeError("page fault")))
    p = MemProcFSProcess(FakeMemprocfsModule(), raw)
    assert p.read(0x1000, 16) is None


def test_read_none_stays_none():
    raw = FakeRawProc(memory=FakeMemory(blob=b""))
    raw.memory.read = lambda a, s, f: None
    p = MemProcFSProcess(FakeMemprocfsModule(), raw)
    assert p.read(0x1000, 16) is None


# --------------------------------------------------------------------------- #
# module_base
# --------------------------------------------------------------------------- #
def test_module_base_ok():
    raw = FakeRawProc(modules={"ntdll.dll": FakeModule(0xABC000)})
    p = MemProcFSProcess(FakeMemprocfsModule(), raw)
    assert p.module_base("ntdll.dll") == 0xABC000


def test_module_base_missing_is_none():
    p = MemProcFSProcess(FakeMemprocfsModule(), FakeRawProc())
    assert p.module_base("nope.dll") is None


def test_module_base_exception_is_none():
    p = MemProcFSProcess(FakeMemprocfsModule(), FakeRawProc(module_raises=True))
    assert p.module_base("any.dll") is None


# --------------------------------------------------------------------------- #
# vads — поддержка разных имён полей и устойчивость к ошибкам
# --------------------------------------------------------------------------- #
def test_vads_with_object_fields():
    class V:
        va_start = 0x1000
        va_end = 0x2000
        protection = "rw-"
        tag = "Stack"
    p = MemProcFSProcess(FakeMemprocfsModule(), FakeRawProc(vad=[V()]))
    vads = p.vads()
    assert vads[0].start == 0x1000 and vads[0].tag == "Stack"


def test_vads_with_dict_alternate_names():
    raw = FakeRawProc(vad=[{"start": 0x3000, "end": 0x4000, "prot": "r--", "info": "Image"}])
    p = MemProcFSProcess(FakeMemprocfsModule(), raw)
    vads = p.vads()
    assert vads[0].start == 0x3000 and vads[0].end == 0x4000


def test_vads_exception_is_empty_list():
    p = MemProcFSProcess(FakeMemprocfsModule(), FakeRawProc(vad_raises=True))
    assert p.vads() == []
