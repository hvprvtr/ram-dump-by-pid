"""Чистая логика инструмента — без зависимости от memprocfs.

Все функции работают с «утиными» объектами процесса (protocol :class:`ProcessLike`),
поэтому покрываются юнит-тестами на моках без реального драйвера.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Protocol, Sequence

# Верхняя граница одного чтения, чтобы случайный --size 0xFFFFFFFFFFFFFFFF
# не привёл к попытке аллоцировать всю память. 256 МиБ — с запасом для дампа региона.
MAX_READ_SIZE = 256 * 1024 * 1024

# Адресное пространство x64: виртуальный адрес не может превышать 2**64.
ADDRESS_SPACE_LIMIT = 1 << 64

# Размер чанка при полном дампе процесса: большие регионы читаем по кускам,
# чтобы не аллоцировать гигабайты разом и переживать дыры (выгруженные страницы).
DEFAULT_DUMP_CHUNK = 4 * 1024 * 1024

# Проба: одна страница в начале региона. Если она недоступна — регион, скорее
# всего, reserved/незакоммичен (в адресном пространстве такие бывают по нескольку
# гигабайт). Тогда не долбим его чанками впустую, а пропускаем целиком.
DEFAULT_PROBE_SIZE = 4096

# Шаг пропуска при дыре внутри региона. memprocfs.read обрывает чтение на первой
# невыделенной странице (возвращает меньше запрошенного), поэтому, упёршись в
# дыру, сдвигаемся ровно на страницу и пробуем читать дальше — committed-данные
# за дырой не теряются.
PAGE_SIZE = 4096


@dataclass(frozen=True)
class Vad:
    """Регион виртуального адресного пространства процесса."""

    start: int
    end: int
    protection: str
    tag: str

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class DumpRegion:
    """Итог дампа одного региона: сколько запросили и сколько реально прочли."""

    start: int
    end: int
    requested: int
    written: int
    error: str = ""

    @property
    def complete(self) -> bool:
        """Регион снят целиком (без дыр и ошибок)."""
        return self.error == "" and self.written == self.requested

    @property
    def skipped(self) -> bool:
        """Из региона не удалось прочитать ни байта."""
        return self.written == 0


class ProcessLike(Protocol):
    """Минимальный контракт процесса, которым пользуется core."""

    pid: int
    name: str

    def read(self, addr: int, size: int) -> Optional[bytes]:
        """Прочитать ``size`` байт по виртуальному адресу ``addr``.

        Возвращает прочитанные байты (возможно, короче ``size`` при частичном
        чтении) или ``None``, если страница недоступна.
        """
        ...

    def module_base(self, module: str) -> Optional[int]:
        """База модуля по имени или ``None``, если модуль не найден."""
        ...

    def vads(self) -> List[Vad]:
        """Список регионов адресного пространства."""
        ...


def parse_int(text: str) -> int:
    """Разобрать целое в dec или hex (``0x...``).

    :raises ValueError: если строка не является числом.
    """
    if text is None:
        raise ValueError("ожидалось число, получено None")
    s = text.strip()
    if not s:
        raise ValueError("ожидалось число, получена пустая строка")
    try:
        # base=0 распознаёт 0x.., 0o.., 0b.. и обычный dec.
        return int(s, 0)
    except ValueError:
        raise ValueError(f"некорректное число: {text!r}")


def select_process(
    processes: Iterable[ProcessLike],
    pid: Optional[int] = None,
    name: Optional[str] = None,
) -> ProcessLike:
    """Выбрать единственный процесс по PID или имени.

    :raises ValueError: если не задан ни один критерий, заданы оба сразу,
        процесс не найден или имя неоднозначно (несколько совпадений).
    """
    if pid is None and name is None:
        raise ValueError("нужно указать либо pid, либо name")
    if pid is not None and name is not None:
        raise ValueError("pid и name взаимоисключающие — задайте что-то одно")

    procs = list(processes)

    if pid is not None:
        matches = [p for p in procs if p.pid == pid]
        if not matches:
            raise ValueError(f"процесс с pid={pid} не найден")
        return matches[0]

    # Поиск по имени — регистронезависимый.
    target = name.lower()
    matches = [p for p in procs if p.name.lower() == target]
    if not matches:
        raise ValueError(f"процесс с именем {name!r} не найден")
    if len(matches) > 1:
        pids = ", ".join(str(p.pid) for p in matches)
        raise ValueError(
            f"имя {name!r} неоднозначно: найдено {len(matches)} процессов "
            f"(pid: {pids}) — уточните через --pid"
        )
    return matches[0]


def read_region(
    process: ProcessLike,
    addr: int,
    size: int,
    max_size: int = MAX_READ_SIZE,
) -> bytes:
    """Прочитать регион памяти процесса с валидацией параметров.

    :raises ValueError: при некорректных ``addr``/``size`` или выходе за пределы
        адресного пространства.
    :raises RuntimeError: если бэкенд не смог прочитать ни байта.
    """
    if not isinstance(addr, int) or isinstance(addr, bool):
        raise ValueError(f"адрес должен быть целым, получено {type(addr).__name__}")
    if not isinstance(size, int) or isinstance(size, bool):
        raise ValueError(f"размер должен быть целым, получено {type(size).__name__}")
    if addr < 0:
        raise ValueError(f"адрес не может быть отрицательным: {addr}")
    if size <= 0:
        raise ValueError(f"размер должен быть положительным: {size}")
    if size > max_size:
        raise ValueError(
            f"размер {size} превышает лимит {max_size} байт "
            f"({max_size // (1024 * 1024)} МиБ)"
        )
    if addr + size > ADDRESS_SPACE_LIMIT:
        raise ValueError(
            f"регион [{addr:#x}..{addr + size:#x}) выходит за пределы "
            f"адресного пространства x64"
        )

    data = process.read(addr, size)
    if data is None:
        raise RuntimeError(
            f"чтение не удалось: адрес {addr:#x} недоступен "
            f"(страница не отображена или выгружена)"
        )
    return bytes(data)


def format_hexdump(data: bytes, base_addr: int = 0, width: int = 16) -> str:
    """Классический hex-дамп: смещение | байты | ASCII."""
    if width <= 0:
        raise ValueError(f"ширина строки должна быть положительной: {width}")
    if not data:
        return ""

    lines: List[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        hex_part = hex_part.ljust(width * 3 - 1)
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{base_addr + offset:016x}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


def format_vads(vads: Sequence[Vad]) -> str:
    """Таблица регионов адресного пространства."""
    if not vads:
        return "(регионов нет)"
    header = f"{'START':>18}  {'END':>18}  {'SIZE':>12}  {'PROT':<8}  TAG"
    lines = [header]
    for v in vads:
        lines.append(
            f"{v.start:#018x}  {v.end:#018x}  {v.size:>12}  "
            f"{v.protection:<8}  {v.tag}"
        )
    return "\n".join(lines)


def resolve_address(
    process: ProcessLike,
    addr: Optional[int] = None,
    module: Optional[str] = None,
    offset: int = 0,
) -> int:
    """Вычислить целевой адрес: либо явный ``addr``, либо ``база модуля + offset``.

    :raises ValueError: если не задано ничего, заданы оба способа, или модуль
        не найден.
    """
    if addr is None and module is None:
        raise ValueError("нужно указать либо адрес, либо модуль")
    if addr is not None and module is not None:
        raise ValueError("адрес и модуль взаимоисключающие — задайте что-то одно")

    if addr is not None:
        return addr + offset

    base = process.module_base(module)
    if base is None:
        raise ValueError(f"модуль {module!r} не найден в процессе")
    return base + offset


def dump_process(
    process: ProcessLike,
    writer: Callable[[int, bytes], None],
    regions: Optional[Sequence[Vad]] = None,
    chunk: int = DEFAULT_DUMP_CHUNK,
    probe_size: int = DEFAULT_PROBE_SIZE,
    hole_skip: int = PAGE_SIZE,
) -> List[DumpRegion]:
    """Снять все читаемые регионы процесса, отдавая прочитанные блоки в ``writer``.

    Обходит ``regions`` (по умолчанию — ``process.vads()``), читает каждый регион
    кусками по ``chunk`` байт и для каждого успешно прочитанного куска вызывает
    ``writer(va, data)``. Недоступные/выгруженные страницы молча пропускаются —
    это нормально для дампа физпамяти: часть региона может быть не резидентна.

    Перед чтением региона читается «проба» в ``probe_size`` байт с его начала.
    Если проба не прошла, регион считается недоступным (как правило, reserved или
    незакоммиченная память — она бывает по нескольку гигабайт) и пропускается
    целиком, без перебора чанками. Это ключевая защита от зависания на дампе.
    ``probe_size <= 0`` отключает пробу.

    Чтение региона идёт чанками по ``chunk`` байт, но курсор сдвигается на число
    РЕАЛЬНО прочитанных байт: бэкенд обрывает чтение на первой невыделенной
    странице и возвращает меньше запрошенного. Упёршись в такую дыру (пустой
    ответ), сдвигаемся на ``hole_skip`` байт и продолжаем — committed-данные за
    дырой не теряются.

    Запись в файл и ведение карты вынесены в ``writer``, поэтому функция остаётся
    чистой и тестируется на моках без файловой системы.

    :returns: список :class:`DumpRegion` — по одному на регион, с числом
        запрошенных и реально записанных байт (и текстом ошибки, если была).
    :raises ValueError: если ``chunk`` или ``hole_skip`` не положительны.
    """
    if not isinstance(chunk, int) or isinstance(chunk, bool) or chunk <= 0:
        raise ValueError(f"размер чанка должен быть положительным целым: {chunk!r}")
    if not isinstance(hole_skip, int) or isinstance(hole_skip, bool) or hole_skip <= 0:
        raise ValueError(f"шаг пропуска дыры должен быть положительным целым: {hole_skip!r}")

    if regions is None:
        regions = process.vads()

    entries: List[DumpRegion] = []
    for v in regions:
        size = v.end - v.start
        if size <= 0:
            # Вырожденный регион (end <= start) — не читаем, но фиксируем в отчёте.
            entries.append(
                DumpRegion(v.start, v.end, 0, 0, "пустой или некорректный регион")
            )
            continue

        # Проба начала региона: отсекает огромные reserved-регионы одним чтением.
        if probe_size > 0:
            probe_n = min(probe_size, size)
            try:
                head = process.read(v.start, probe_n)
            except Exception as e:
                entries.append(
                    DumpRegion(v.start, v.end, size, 0, f"регион недоступен: {e}")
                )
                continue
            if not head:
                entries.append(
                    DumpRegion(
                        v.start, v.end, size, 0, "регион недоступен (проба не прошла)"
                    )
                )
                continue

        written = 0
        error = ""
        offset = 0
        while offset < size:
            n = min(chunk, size - offset)
            try:
                data = process.read(v.start + offset, n)
            except Exception as e:  # бэкенд может бросить что угодно — это дыра, не крах
                data = None
                if not error:
                    error = f"ошибка чтения на {v.start + offset:#x}: {e}"
            if data:
                writer(v.start + offset, bytes(data))
                written += len(data)
                # Сдвигаемся на реально прочитанное: при частичном ответе остаток
                # чанка ещё не прочитан (там, скорее всего, начинается дыра).
                offset += len(data)
            else:
                # Дыра/недоступная страница — пропускаем её и пробуем дальше.
                offset += hole_skip

        entries.append(DumpRegion(v.start, v.end, size, written, error))
    return entries
