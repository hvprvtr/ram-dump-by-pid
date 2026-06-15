"""Тесты адаптера memprocfs на фейковом модуле memprocfs (без Windows).

Проверяем, что обёртка корректно достаёт данные, переживает разные имена полей
VAD и не падает, а гасит исключения нижнего слоя в None/[] (злые случаи).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rampidreader.backend import MemProcFSProcess, _first_attr  # noqa: E402


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
