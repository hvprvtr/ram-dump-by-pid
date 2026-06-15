# ram-dumper-by-pid (Rust)

Порт Python-инструмента на Rust поверх официального крейта
[`memprocfs`](https://crates.io/crates/memprocfs) (тот же автор, что и MemProcFS).
Чтение памяти процесса **по PID/имени** через winpmem + MemProcFS/LeechCore,
**без `OpenProcess`** — логика 1:1 с Python-версией из `../python/`.

## Структура

| Слой | Файл | Зависит от нативных библиотек? |
|------|------|--------------------------------|
| Чистая логика + тесты | `src/lib.rs` | нет — мок-тесты на любой ОС |
| Адаптер memprocfs | `src/backend.rs` | да (фича `backend`) |
| CLI | `src/main.rs` | нет внешних зависимостей (argv вручную) |

`lib.rs` ничего не знает о memprocfs (трейт `ProcessLike`), поэтому
`cargo test` гоняет всю логику без драйвера и нативных dll.

## Тесты (чистая логика)

```bash
cargo test          # 46 тестов: обычные + злые (Angry Tests)
```

Часть «злых» тестов Python (отрицательный адрес/размер, bool, None, исключения
чтения) на уровне типов Rust невозможна — устранена by design (беззнаковые типы,
`Option` вместо исключений).

## Сборка бинаря (с бэкендом)

Бинарь читает реальную память только с фичей `backend`:

```bash
cargo build --release --features backend
```

### Тулчейн (важно)

`backend` тянет `memprocfs` → `libloading` → `windows-sys`, которому при сборке
нужен `dlltool` (генерация import-библиотек). Варианты окружения:

* **MSVC** — поставить Build Tools for Visual Studio (даёт `link.exe`).
* **GNU** (использовали при разработке) — самодостаточный линкер, но нужен
  `dlltool` + ассемблер `as` из MinGW-w64 binutils:
  ```
  rustup default stable-x86_64-pc-windows-gnu
  # binutils: напр. через MSYS2 → C:\msys64\mingw64\bin в PATH
  ```

## Запуск

Крейт `memprocfs` грузит нативные `vmm.dll` и `leechcore.dll` (рядом друг с
другом) — путь к `vmm.dll` передаётся через `--vmm`. Берите их из официального
релиза MemProcFS (в pip-пакете `memprocfs` отдельной `leechcore.dll` нет).

```powershell
# полный дамп процесса
ramreader-by-pid.exe --pid 1234 --dump-all --out proc.bin `
           --driver C:\path\winpmem_x64.sys `
           --vmm    C:\path\MemProcFS\vmm.dll

# карта регионов / точечное чтение — как в Python-версии
ramreader-by-pid.exe --name notepad.exe --vads        --driver ... --vmm ...
ramreader-by-pid.exe --pid 1234 --module ntdll.dll --offset 0 --size 4096 --out h.bin --driver ... --vmm ...
```

Флаги (`--pid/--name/--addr/--module/--offset/--size/--vads/--dump-all/--out`),
формат `.map` и коды возврата (`0/1/2`) совпадают с Python-версией.

## Проверено

На Win11 build 26200, живые процессы (notepad/calc/powershell):

* чтение модуля через Rust **байт-в-байт** совпадает с Python-версией и
  эталоном ОС (procdump) — одинаковый SHA256;
* `--dump-all` даёт тот же объём, что Python (188.86 МБ для notepad в один
  момент), покрытие образа `ntdll` — 100%.

> Тонкость, найденная при портировании: `Vmm::mem_read_ex` крейта возвращает
> буфер полного размера, **игнорируя** число реально прочитанных байт (дыры =
> нули), из-за чего дамп раздувается до гигабайт. Используется `mem_read_into`,
> отдающий фактическое `cb_read`, — это и даёт корректный пропуск дыр/reserved.
