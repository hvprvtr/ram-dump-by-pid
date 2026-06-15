# ram-pid-reader

Чтение памяти процесса **по PID/имени** через драйвер дампера физпамяти
(WinPmem) + MemProcFS/LeechCore, **без `OpenProcess`/`PROCESS_VM_READ`** в коде
инструмента. LeechCore поднимает подписанный драйвер WinPmem, а MemProcFS делает
`PID → CR3 → page-table walk` поверх физических чтений — инструмент лишь дёргает
`read(va, size)`.

Репозиторий содержит две независимые реализации с идентичной логикой, CLI-флагами,
форматом `.map` и кодами возврата (`0/1/2`):

| Реализация | Каталог | Запускаемый артефакт |
|------------|---------|----------------------|
| Python | [`python/`](python/) | `ram-dump-by-pid.py` |
| Rust | [`rust/`](rust/) | `ram-dump-by-pid` (`.exe`) |

Подробности по сборке, тестам и использованию — в README соответствующего каталога:
[`python/README.md`](python/README.md), [`rust/README.md`](rust/README.md).
