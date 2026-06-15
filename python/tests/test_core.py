"""Юнит-тесты ядра. Не требуют Windows/драйвера — всё на моках.

Помимо обычных тестов содержат «злые» (Angry Tests): граничные значения,
некорректный ввод, переполнения, частичные и провальные чтения, неоднозначность.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rampidreader import core  # noqa: E402
from rampidreader.core import DumpRegion, Vad  # noqa: E402


# --------------------------------------------------------------------------- #
# Моки процесса
# --------------------------------------------------------------------------- #
class FakeProcess:
    def __init__(self, pid, name, mem=None, modules=None, vads=None):
        self.pid = pid
        self.name = name
        self._mem = mem if mem is not None else {}
        self._modules = modules or {}
        self._vads = vads or []
        self.read_calls = []

    def read(self, addr, size):
        self.read_calls.append((addr, size))
        if addr in self._mem:
            data = self._mem[addr]
            return data[:size]  # эмуляция возможного частичного чтения
        return None

    def module_base(self, module):
        return self._modules.get(module)

    def vads(self):
        return list(self._vads)


# --------------------------------------------------------------------------- #
# parse_int — обычные
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [("0", 0), ("1234", 1234), ("0x10", 16), ("0xDEADBEEF", 0xDEADBEEF),
     ("  42  ", 42), ("0o17", 15), ("0b1010", 10)],
)
def test_parse_int_ok(text, expected):
    assert core.parse_int(text) == expected


# parse_int — злые
@pytest.mark.parametrize("bad", ["", "   ", "abc", "0xZZ", "12.5", "1,000", "0x", "--5", "1 2"])
def test_parse_int_rejects_garbage(bad):
    with pytest.raises(ValueError):
        core.parse_int(bad)


def test_parse_int_rejects_none():
    with pytest.raises(ValueError):
        core.parse_int(None)


# --------------------------------------------------------------------------- #
# select_process — обычные
# --------------------------------------------------------------------------- #
def test_select_by_pid():
    procs = [FakeProcess(1, "a.exe"), FakeProcess(2, "b.exe")]
    assert core.select_process(procs, pid=2).pid == 2


def test_select_by_name_case_insensitive():
    procs = [FakeProcess(10, "Notepad.exe")]
    assert core.select_process(procs, name="notepad.exe").pid == 10


# select_process — злые
def test_select_requires_a_criterion():
    with pytest.raises(ValueError, match="pid.*name"):
        core.select_process([FakeProcess(1, "a.exe")])


def test_select_rejects_both_criteria():
    with pytest.raises(ValueError, match="взаимоисключающ"):
        core.select_process([FakeProcess(1, "a.exe")], pid=1, name="a.exe")


def test_select_pid_not_found():
    with pytest.raises(ValueError, match="не найден"):
        core.select_process([FakeProcess(1, "a.exe")], pid=999)


def test_select_name_not_found():
    with pytest.raises(ValueError, match="не найден"):
        core.select_process([FakeProcess(1, "a.exe")], name="ghost.exe")


def test_select_name_ambiguous_lists_pids():
    procs = [FakeProcess(11, "chrome.exe"), FakeProcess(22, "chrome.exe")]
    with pytest.raises(ValueError, match="неоднозначно"):
        core.select_process(procs, name="chrome.exe")


def test_select_empty_process_list():
    with pytest.raises(ValueError, match="не найден"):
        core.select_process([], pid=1)


# --------------------------------------------------------------------------- #
# read_region — обычные
# --------------------------------------------------------------------------- #
def test_read_region_ok():
    proc = FakeProcess(1, "a.exe", mem={0x1000: b"\xde\xad\xbe\xef"})
    assert core.read_region(proc, 0x1000, 4) == b"\xde\xad\xbe\xef"


def test_read_region_partial_returns_what_it_got():
    # backend отдал меньше запрошенного — это не ошибка, возвращаем как есть.
    proc = FakeProcess(1, "a.exe", mem={0x1000: b"\x01\x02"})
    data = core.read_region(proc, 0x1000, 16)
    assert data == b"\x01\x02"


# read_region — злые
def test_read_region_zero_size():
    proc = FakeProcess(1, "a.exe", mem={0x1000: b"x"})
    with pytest.raises(ValueError, match="положительн"):
        core.read_region(proc, 0x1000, 0)


def test_read_region_negative_size():
    proc = FakeProcess(1, "a.exe")
    with pytest.raises(ValueError, match="положительн"):
        core.read_region(proc, 0x1000, -1)


def test_read_region_negative_addr():
    proc = FakeProcess(1, "a.exe")
    with pytest.raises(ValueError, match="отрицательн"):
        core.read_region(proc, -8, 4)


def test_read_region_size_over_limit():
    proc = FakeProcess(1, "a.exe")
    with pytest.raises(ValueError, match="превышает лимит"):
        core.read_region(proc, 0x1000, core.MAX_READ_SIZE + 1)


def test_read_region_address_space_overflow():
    proc = FakeProcess(1, "a.exe")
    with pytest.raises(ValueError, match="адресного пространства"):
        core.read_region(proc, (1 << 64) - 4, 8)


def test_read_region_unmapped_raises_runtime():
    proc = FakeProcess(1, "a.exe", mem={})  # read вернёт None
    with pytest.raises(RuntimeError, match="недоступен"):
        core.read_region(proc, 0x4000, 16)


def test_read_region_rejects_bool_as_size():
    # bool — подкласс int, но это явно ошибка вызывающего.
    proc = FakeProcess(1, "a.exe", mem={0x1000: b"x"})
    with pytest.raises(ValueError):
        core.read_region(proc, 0x1000, True)


def test_read_region_rejects_bool_as_addr():
    proc = FakeProcess(1, "a.exe", mem={0x1000: b"x"})
    with pytest.raises(ValueError):
        core.read_region(proc, True, 4)


# --------------------------------------------------------------------------- #
# resolve_address
# --------------------------------------------------------------------------- #
def test_resolve_explicit_addr_with_offset():
    proc = FakeProcess(1, "a.exe")
    assert core.resolve_address(proc, addr=0x1000, offset=0x10) == 0x1010


def test_resolve_from_module_base():
    proc = FakeProcess(1, "a.exe", modules={"ntdll.dll": 0x7FF000000000})
    assert core.resolve_address(proc, module="ntdll.dll", offset=0x20) == 0x7FF000000020


def test_resolve_requires_something():
    with pytest.raises(ValueError, match="адрес.*модул"):
        core.resolve_address(FakeProcess(1, "a.exe"))


def test_resolve_rejects_both():
    proc = FakeProcess(1, "a.exe", modules={"ntdll.dll": 0x1000})
    with pytest.raises(ValueError, match="взаимоисключающ"):
        core.resolve_address(proc, addr=0x1000, module="ntdll.dll")


def test_resolve_module_not_found():
    proc = FakeProcess(1, "a.exe", modules={})
    with pytest.raises(ValueError, match="не найден"):
        core.resolve_address(proc, module="missing.dll")


# --------------------------------------------------------------------------- #
# format_hexdump
# --------------------------------------------------------------------------- #
def test_hexdump_empty_is_empty_string():
    assert core.format_hexdump(b"") == ""


def test_hexdump_basic_layout():
    out = core.format_hexdump(b"ABC", base_addr=0x1000)
    assert out.startswith("0000000000001000  ")
    assert "41 42 43" in out
    assert out.endswith("|ABC|")


def test_hexdump_non_printable_becomes_dot():
    out = core.format_hexdump(b"\x00\x01\xff")
    assert out.endswith("|...|")


def test_hexdump_multiline_wraps_at_width():
    out = core.format_hexdump(b"\x00" * 20, width=16)
    assert len(out.splitlines()) == 2


def test_hexdump_rejects_nonpositive_width():
    with pytest.raises(ValueError):
        core.format_hexdump(b"abc", width=0)


# --------------------------------------------------------------------------- #
# format_vads
# --------------------------------------------------------------------------- #
def test_format_vads_empty():
    assert core.format_vads([]) == "(регионов нет)"


def test_format_vads_contains_region():
    vads = [Vad(0x1000, 0x2000, "rw-", "Heap")]
    out = core.format_vads(vads)
    assert "Heap" in out and "0x0000000000001000" in out


def test_vad_size_computed():
    assert Vad(0x1000, 0x1800, "r--", "x").size == 0x800


# --------------------------------------------------------------------------- #
# dump_process — обычные
# --------------------------------------------------------------------------- #
class RangeProcess:
    """Мок, отдающий байты по любому адресу из «карты» {start: bytes}.

    В отличие от FakeProcess умеет частичные чтения и чтение из середины
    региона — нужно для проверки чанкинга и дыр.
    """

    def __init__(self, pid=1, name="a.exe", segments=None, vads=None, fail_at=None):
        self.pid = pid
        self.name = name
        # segments: {base_addr: bytes} — непрерывный блок памяти от base_addr.
        self._segments = segments or {}
        self._vads = vads or []
        self._fail_at = set(fail_at or [])
        self.read_calls = []

    def read(self, addr, size):
        self.read_calls.append((addr, size))
        if addr in self._fail_at:
            raise OSError("смоделированный сбой бэкенда")
        for base, blob in self._segments.items():
            if base <= addr < base + len(blob):
                start = addr - base
                return blob[start : start + size]
        return None

    def module_base(self, module):
        return None

    def vads(self):
        return list(self._vads)


def test_dump_process_basic_single_region():
    proc = RangeProcess(
        segments={0x1000: b"ABCD"},
        vads=[Vad(0x1000, 0x1004, "rw-", "Heap")],
    )
    blocks = []
    entries = core.dump_process(proc, lambda va, data: blocks.append((va, data)))
    assert blocks == [(0x1000, b"ABCD")]
    assert len(entries) == 1
    assert entries[0].complete and entries[0].written == 4


def test_dump_process_multiple_regions():
    proc = RangeProcess(
        segments={0x1000: b"AAAA", 0x5000: b"BB"},
        vads=[Vad(0x1000, 0x1004, "rw-", "a"), Vad(0x5000, 0x5002, "r--", "b")],
    )
    written = []
    entries = core.dump_process(proc, lambda va, data: written.append((va, data)))
    assert written == [(0x1000, b"AAAA"), (0x5000, b"BB")]
    assert all(e.complete for e in entries)


def test_dump_process_uses_process_vads_by_default():
    proc = RangeProcess(
        segments={0x2000: b"XYZ"},
        vads=[Vad(0x2000, 0x2003, "rw-", "v")],
    )
    entries = core.dump_process(proc, lambda va, data: None)
    assert [e.start for e in entries] == [0x2000]


def test_dump_process_explicit_regions_override_vads():
    proc = RangeProcess(
        segments={0x9000: b"ZZZZ"},
        vads=[Vad(0x1000, 0x1004, "rw-", "ignored")],
    )
    entries = core.dump_process(
        proc, lambda va, data: None, regions=[Vad(0x9000, 0x9004, "rw-", "use")]
    )
    assert [e.start for e in entries] == [0x9000]
    assert entries[0].complete


def test_dump_process_chunks_large_region():
    # Регион 10 байт, чанк 4 → читается кусками 4+4+2, склейка непрерывна.
    proc = RangeProcess(
        segments={0x1000: b"0123456789"},
        vads=[Vad(0x1000, 0x100A, "rw-", "big")],
    )
    blocks = []
    entries = core.dump_process(
        proc, lambda va, data: blocks.append((va, data)), chunk=4
    )
    assert blocks == [(0x1000, b"0123"), (0x1004, b"4567"), (0x1008, b"89")]
    assert entries[0].complete and entries[0].written == 10
    assert b"".join(d for _, d in blocks) == b"0123456789"


# --------------------------------------------------------------------------- #
# dump_process — злые
# --------------------------------------------------------------------------- #
def test_dump_process_no_regions_writes_nothing():
    proc = RangeProcess(vads=[])
    calls = []
    entries = core.dump_process(proc, lambda va, data: calls.append(va))
    assert entries == [] and calls == []


def test_dump_process_skips_fully_unreadable_region():
    # Регион есть в VAD, но в памяти его нет (reserved / выгружен) → read None.
    proc = RangeProcess(segments={}, vads=[Vad(0x4000, 0x4100, "rw-", "gone")])
    calls = []
    entries = core.dump_process(proc, lambda va, data: calls.append(va))
    assert calls == []  # writer не вызывался
    assert entries[0].skipped and not entries[0].complete
    assert entries[0].written == 0


def test_dump_process_hole_in_middle_is_partial():
    # Первый чанк читается, второй — дыра (None), третий снова есть.
    class HoleProc(RangeProcess):
        def read(self, addr, size):
            self.read_calls.append((addr, size))
            return None if addr == 0x1004 else b"\xaa" * size

    proc = HoleProc(vads=[Vad(0x1000, 0x100C, "rw-", "swiss")])
    blocks = []
    entries = core.dump_process(
        proc, lambda va, data: blocks.append((va, len(data))), chunk=4, hole_skip=4
    )
    # дыра 0x1004 пропущена, два других чанка записаны
    assert blocks == [(0x1000, 4), (0x1008, 4)]
    assert entries[0].written == 8 and entries[0].requested == 0xC
    assert not entries[0].complete and not entries[0].skipped


def test_dump_process_read_exception_becomes_error_not_crash():
    proc = RangeProcess(
        segments={0x1000: b"AAAA"},
        vads=[Vad(0x1000, 0x1004, "rw-", "boom")],
        fail_at=[0x1000],
    )
    entries = core.dump_process(proc, lambda va, data: None)
    # исключение бэкенда не валит дамп — оно превращается в текст ошибки
    assert entries[0].error and "сбой" in entries[0].error
    assert entries[0].skipped


def test_dump_process_one_bad_region_does_not_stop_others():
    proc = RangeProcess(
        segments={0x5000: b"OK!!"},
        vads=[Vad(0x1000, 0x1004, "rw-", "bad"), Vad(0x5000, 0x5004, "rw-", "good")],
        fail_at=[0x1000],
    )
    written = []
    entries = core.dump_process(proc, lambda va, data: written.append(va))
    assert written == [0x5000]  # второй регион всё равно снят
    assert entries[0].skipped and entries[1].complete


@pytest.mark.parametrize("bad_end", [0x1000, 0x0FFF, 0])
def test_dump_process_degenerate_region_recorded_not_read(bad_end):
    # end <= start: не читаем, но фиксируем как ошибочный регион.
    proc = RangeProcess(vads=[Vad(0x1000, bad_end, "rw-", "weird")])
    calls = []
    entries = core.dump_process(proc, lambda va, data: calls.append(va))
    assert calls == []
    assert entries[0].error and entries[0].requested == 0


def test_dump_process_partial_read_does_not_skip_data_after_hole():
    # Регрессия: memprocfs.read отдаёт байты до первой дыры и обрывается, возвращая
    # меньше запрошенного. Курсор обязан двигаться на ПРОЧИТАННОЕ, а не на размер
    # чанка, иначе committed-данные за дырой теряются («только начало региона»).
    class MemLikeProc:
        pid, name = 1, "a.exe"

        def __init__(self):
            self.read_calls = []
            # данные: [0x1000,0x1004) и [0x1008,0x100C); дыра между ними
            self._data = {0x1000: b"AAAA", 0x1008: b"BBBB"}

        def read(self, addr, size):
            self.read_calls.append((addr, size))
            return self._data[addr][:size] if addr in self._data else None

        def module_base(self, m):
            return None

        def vads(self):
            return [Vad(0x1000, 0x100C, "rw-", "frag")]

    proc = MemLikeProc()
    blocks = []
    # один чанк на весь регион — ровно тот случай, где проявлялся баг
    entries = core.dump_process(
        proc, lambda va, d: blocks.append((va, d)), chunk=64, hole_skip=4
    )
    assert blocks == [(0x1000, b"AAAA"), (0x1008, b"BBBB")]  # данные за дырой целы
    assert entries[0].written == 8


@pytest.mark.parametrize("bad_skip", [0, -1, True])
def test_dump_process_rejects_nonpositive_hole_skip(bad_skip):
    proc = RangeProcess(vads=[Vad(0x1000, 0x1004, "rw-", "x")])
    with pytest.raises(ValueError, match="пропуск"):
        core.dump_process(proc, lambda va, data: None, hole_skip=bad_skip)


def test_dump_process_probe_skips_huge_unreadable_region_in_one_read():
    # Гигантский reserved-регион (4 ГБ) без памяти: проба должна отсечь его
    # ОДНИМ чтением, а не перебором миллиона чанков (защита от зависания).
    huge = Vad(0x1000, 0x1000 + 4 * 1024 * 1024 * 1024, "rw-", "reserved")
    proc = RangeProcess(segments={}, vads=[huge])
    calls = []
    entries = core.dump_process(proc, lambda va, data: calls.append(va), chunk=4096)
    assert len(proc.read_calls) == 1  # только проба
    assert calls == []
    assert entries[0].skipped and "проба" in entries[0].error


def test_dump_process_probe_disabled_falls_back_to_chunk_scan():
    # probe_size=0 отключает пробу — тогда нечитаемый регион перебирается чанками.
    region = Vad(0x1000, 0x100C, "rw-", "x")  # 12 байт, чанк 4 → 3 попытки
    proc = RangeProcess(segments={}, vads=[region])
    entries = core.dump_process(
        proc, lambda va, data: None, chunk=4, probe_size=0, hole_skip=4
    )
    assert len(proc.read_calls) == 3  # без пробы — полный перебор по страницам
    assert entries[0].skipped


def test_dump_process_probe_failure_exception_skips_region():
    proc = RangeProcess(
        segments={0x1000: b"AAAA"},
        vads=[Vad(0x1000, 0x1004, "rw-", "boom")],
        fail_at=[0x1000],
    )
    entries = core.dump_process(proc, lambda va, data: None)
    assert entries[0].skipped and "недоступен" in entries[0].error


@pytest.mark.parametrize("bad_chunk", [0, -1, -4096])
def test_dump_process_rejects_nonpositive_chunk(bad_chunk):
    proc = RangeProcess(vads=[Vad(0x1000, 0x1004, "rw-", "x")])
    with pytest.raises(ValueError, match="чанк"):
        core.dump_process(proc, lambda va, data: None, chunk=bad_chunk)


def test_dump_process_rejects_bool_chunk():
    proc = RangeProcess(vads=[Vad(0x1000, 0x1004, "rw-", "x")])
    with pytest.raises(ValueError):
        core.dump_process(proc, lambda va, data: None, chunk=True)


# --------------------------------------------------------------------------- #
# DumpRegion — свойства
# --------------------------------------------------------------------------- #
def test_dump_region_complete_flag():
    assert DumpRegion(0x1000, 0x1004, 4, 4).complete
    assert not DumpRegion(0x1000, 0x1004, 4, 2).complete
    assert not DumpRegion(0x1000, 0x1004, 4, 4, "boom").complete


def test_dump_region_skipped_flag():
    assert DumpRegion(0x1000, 0x1004, 4, 0).skipped
    assert not DumpRegion(0x1000, 0x1004, 4, 1).skipped
