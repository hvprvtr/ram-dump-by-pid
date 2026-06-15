"""Адаптер поверх memprocfs/LeechCore. Работает только на Windows с драйвером.

Изолирует ВСЕ вызовы memprocfs, превращая их в чистые объекты, понятные
:mod:`rampidreader.core`. Импорт memprocfs ленивый — модуль грузится только при
реальном открытии устройства, поэтому тесты ядра идут на любой платформе.
"""

from __future__ import annotations

from typing import List, Optional

from .core import Vad


class MemProcFSProcess:
    """Обёртка над объектом процесса memprocfs, реализующая ProcessLike."""

    def __init__(self, vmm_module, proc):
        self._memprocfs = vmm_module
        self._proc = proc
        self.pid = int(proc.pid)
        self.name = str(proc.name)

    def read(self, addr: int, size: int) -> Optional[bytes]:
        try:
            # FLAG_NOCACHE — для «живой» памяти, иначе вернётся закэшированная страница.
            flag = getattr(self._memprocfs, "FLAG_NOCACHE", 0)
            data = self._proc.memory.read(addr, size, flag)
        except Exception:
            return None
        if data is None:
            return None
        return bytes(data)

    def module_base(self, module: str) -> Optional[int]:
        try:
            m = self._proc.module(module)
        except Exception:
            return None
        if m is None:
            return None
        return int(m.base)

    def vads(self) -> List[Vad]:
        try:
            raw = self._proc.maps.vad()
        except Exception:
            return []
        result: List[Vad] = []
        for v in raw:
            # Имена полей в memprocfs могут отличаться между версиями — берём
            # с запасными вариантами и сверим на реальной машине.
            start = _first_attr(v, "va_start", "start", "base", default=0)
            end = _first_attr(v, "va_end", "end", default=0)
            prot = _first_attr(v, "protection", "prot", "flags", default="")
            tag = _first_attr(v, "tag", "info", "name", default="")
            result.append(Vad(int(start), int(end), str(prot), str(tag)))
        return result


def _first_attr(obj, *names, default):
    """Вернуть первый существующий атрибут из ``names`` (поддержка и dict, и объекта)."""
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        elif hasattr(obj, n):
            return getattr(obj, n)
    return default


class MemProcFSBackend:
    """Открывает устройство физпамяти и перечисляет процессы."""

    def __init__(self, device: str, extra_args: Optional[List[str]] = None):
        self.device = device
        self.extra_args = extra_args or []
        self._memprocfs = None
        self._vmm = None

    def open(self) -> "MemProcFSBackend":
        try:
            import memprocfs  # ленивый импорт: только на Windows с драйвером
        except ImportError as e:
            raise RuntimeError(
                "не удалось импортировать memprocfs — установите пакет "
                "(pip install memprocfs) и запустите на Windows с драйвером winpmem"
            ) from e

        self._memprocfs = memprocfs
        args = ["-device", self.device, *self.extra_args]
        try:
            self._vmm = memprocfs.Vmm(args)
        except Exception as e:
            raise RuntimeError(
                f"не удалось инициализировать LeechCore с device={self.device!r}: {e} "
                f"(нужны права администратора и загруженный драйвер)"
            ) from e
        return self

    def processes(self) -> List[MemProcFSProcess]:
        if self._vmm is None:
            raise RuntimeError("backend не открыт — сначала вызовите open()")
        return [
            MemProcFSProcess(self._memprocfs, p) for p in self._vmm.process_list()
        ]

    def close(self) -> None:
        if self._vmm is not None:
            try:
                self._vmm.close()
            except Exception:
                pass
            self._vmm = None

    def __enter__(self) -> "MemProcFSBackend":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
