//! CLI: чтение памяти процесса по PID/имени через драйвер физпамяти (winpmem).
//! Зеркало `ramreader-by-pid.py`. Коды возврата: 0 — успех, 1 — ошибка чтения/процесса,
//! 2 — ошибка аргументов. Аргументы парсятся вручную (без внешних зависимостей).

use rampidreader as core;
#[cfg(feature = "backend")]
use rampidreader::{
    ProcessLike, DEFAULT_DUMP_CHUNK, DEFAULT_PROBE_SIZE, MAX_READ_SIZE, PAGE_SIZE,
};
#[cfg(feature = "backend")]
use std::fs::File;
#[cfg(feature = "backend")]
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

#[cfg(feature = "backend")]
mod backend;

const USAGE: &str = "\
ram-dump-by-pid — чтение памяти процесса по PID через драйвер физпамяти (winpmem)

ВЫБОР ЦЕЛИ (одно из):
  --pid <N>          PID процесса (dec или 0x..)
  --name <str>       имя процесса, напр. notepad.exe

ИСТОЧНИК ПАМЯТИ:
  --driver <path>    путь к winpmem_x64.sys — соберёт PMEM://<path>
                     (по умолч. winpmem_x64.sys рядом с бинарником)
  --device <str>     сырая device-строка LeechCore (переопределяет --driver)
  --vmm <path>       путь к нативной vmm.dll (нужен бэкенду)
                     (по умолч. vmm.dll рядом с бинарником)

ЧТО ЧИТАТЬ:
  --addr <N>         виртуальный адрес
  --module <str>     имя модуля — читать от его базы
  --offset <N>       смещение от адреса/базы модуля (по умолч. 0)
  --size <N>         сколько байт прочитать
  --vads             показать регионы (VAD) и выйти
  --dump-all         снять полный дамп процесса в --out (+ <out>.map)
  --out <path>       сохранить сырые байты в файл вместо hex-вывода
";

#[derive(Default)]
struct Args {
    pid: Option<String>,
    name: Option<String>,
    driver: Option<String>,
    device: Option<String>,
    vmm: Option<String>,
    addr: Option<String>,
    module: Option<String>,
    offset: Option<String>,
    size: Option<String>,
    vads: bool,
    dump_all: bool,
    out: Option<String>,
}

/// Каталог запускаемого бинарника — для дефолтных путей к dll/драйверу.
fn exe_dir() -> Option<PathBuf> {
    std::env::current_exe()
        .ok()?
        .parent()
        .map(|p| p.to_path_buf())
}

/// Разрешить путь к файлу. Если задан явный — проверить, что он существует.
/// Иначе попробовать `<каталог exe>/<default_name>`. Понятная ошибка, если
/// ничего не найдено — с просьбой указать путь через `flag`.
fn resolve_path(explicit: Option<&str>, default_name: &str, flag: &str) -> Result<String, String> {
    if let Some(p) = explicit {
        return if Path::new(p).is_file() {
            Ok(p.to_string())
        } else {
            Err(format!("указанный через {flag} путь не существует: {p}"))
        };
    }
    let dir = exe_dir().ok_or("не удалось определить каталог бинарника")?;
    let cand = dir.join(default_name);
    if cand.is_file() {
        cand.to_str()
            .map(str::to_string)
            .ok_or_else(|| format!("путь содержит не-UTF8 символы: {}", cand.display()))
    } else {
        Err(format!(
            "{default_name} не найден рядом с бинарником ({}) — укажите путь через {flag} <путь>",
            cand.display()
        ))
    }
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(code) => return ExitCode::from(code),
    };
    ExitCode::from(run(args) as u8)
}

/// Ручной разбор argv. Поддерживает `--key value` и `--key=value`.
fn parse_args() -> Result<Args, u8> {
    let mut a = Args::default();
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        let raw = argv[i].clone();
        let (key, inline) = match raw.split_once('=') {
            Some((k, v)) => (k.to_string(), Some(v.to_string())),
            None => (raw.clone(), None),
        };

        // Достать значение для флага, требующего аргумент.
        macro_rules! value {
            () => {{
                match inline.clone() {
                    Some(v) => v,
                    None => {
                        i += 1;
                        match argv.get(i) {
                            Some(v) => v.clone(),
                            None => {
                                eprintln!("ошибка аргументов: {key} требует значение");
                                return Err(2);
                            }
                        }
                    }
                }
            }};
        }

        match key.as_str() {
            "--pid" => a.pid = Some(value!()),
            "--name" => a.name = Some(value!()),
            "--driver" => a.driver = Some(value!()),
            "--device" => a.device = Some(value!()),
            "--vmm" => a.vmm = Some(value!()),
            "--addr" => a.addr = Some(value!()),
            "--module" => a.module = Some(value!()),
            "--offset" => a.offset = Some(value!()),
            "--size" => a.size = Some(value!()),
            "--out" => a.out = Some(value!()),
            "--vads" => a.vads = true,
            "--dump-all" => a.dump_all = true,
            "-h" | "--help" => {
                print!("{USAGE}");
                return Err(0);
            }
            _ => {
                eprintln!("ошибка аргументов: неизвестный аргумент {raw}");
                return Err(2);
            }
        }
        i += 1;
    }
    Ok(a)
}

fn run(args: Args) -> i32 {
    // Взаимоисключающая обязательная цель (как argparse mutually exclusive group).
    match (&args.pid, &args.name) {
        (None, None) => {
            eprintln!("ошибка аргументов: укажите --pid или --name");
            return 2;
        }
        (Some(_), Some(_)) => {
            eprintln!("ошибка аргументов: --pid и --name взаимоисключающие");
            return 2;
        }
        _ => {}
    }

    // Разбор числовых аргументов — ошибки парсинга это код 2.
    macro_rules! parse_opt {
        ($field:expr) => {
            match $field.as_deref().map(core::parse_int).transpose() {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("ошибка аргументов: {e}");
                    return 2;
                }
            }
        };
    }
    let pid = parse_opt!(args.pid).map(|x| x as u32);
    let addr = parse_opt!(args.addr);
    let size = parse_opt!(args.size);
    let offset = match args.offset.as_deref() {
        Some(s) => match core::parse_int(s) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("ошибка аргументов: {e}");
                return 2;
            }
        },
        None => 0,
    };

    // Валидация режимов.
    if args.dump_all {
        if args.out.is_none() {
            eprintln!("ошибка: для --dump-all нужен --out <файл>");
            return 2;
        }
    } else if !args.vads {
        if size.is_none() {
            eprintln!("ошибка: для чтения нужен --size (или используйте --vads)");
            return 2;
        }
        if addr.is_none() && args.module.is_none() {
            eprintln!("ошибка: укажите --addr или --module");
            return 2;
        }
    }

    // Сборка device-строки LeechCore. --device (сырая строка) переопределяет всё
    // и проверку файла не требует. Иначе берём драйвер: явный --driver либо дефолт
    // winpmem_x64.sys рядом с бинарником.
    let device = if let Some(d) = args.device.clone() {
        d
    } else {
        match resolve_path(args.driver.as_deref(), "winpmem_x64.sys", "--driver") {
            Ok(p) => format!("PMEM://{p}"),
            Err(e) => {
                eprintln!("ошибка: {e}");
                return 2;
            }
        }
    };

    backend_run(&args, &device, pid, addr, offset, size)
}

#[cfg(feature = "backend")]
fn backend_run(
    args: &Args,
    device: &str,
    pid: Option<u32>,
    addr: Option<u64>,
    offset: u64,
    size: Option<u64>,
) -> i32 {
    let vmm_dll = match resolve_path(args.vmm.as_deref(), "vmm.dll", "--vmm") {
        Ok(p) => p,
        Err(e) => {
            eprintln!("ошибка: {e}");
            return 2;
        }
    };
    let vmm = match backend::open(&vmm_dll, device) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("ошибка: не удалось инициализировать LeechCore: {e}");
            return 1;
        }
    };
    let procs = backend::processes(&vmm);
    let proc = match core::select_process(&procs, pid, args.name.as_deref()) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("ошибка: {e}");
            return 1;
        }
    };
    eprintln!("[+] процесс: pid={} name={}", proc.pid(), proc.name());
    let proc: &dyn ProcessLike = proc;

    if args.vads {
        println!("{}", core::format_vads(&proc.vads()));
        return 0;
    }
    if args.dump_all {
        return dump_all(proc, args.out.as_deref().unwrap());
    }

    let target = match core::resolve_address(proc, addr, args.module.as_deref(), offset) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("ошибка: {e}");
            return 1;
        }
    };
    let size = size.unwrap();
    let data = match core::read_region(proc, target, size, MAX_READ_SIZE) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("ошибка: {e}");
            return 1;
        }
    };
    if (data.len() as u64) < size {
        eprintln!(
            "[!] частичное чтение: получено {} из {} байт",
            data.len(),
            size
        );
    }
    match args.out.as_deref() {
        Some(out) => match File::create(out).and_then(|mut f| f.write_all(&data)) {
            Ok(_) => eprintln!("[+] записано {} байт в {}", data.len(), out),
            Err(e) => {
                eprintln!("ошибка ввода-вывода: {e}");
                return 1;
            }
        },
        None => match core::format_hexdump(&data, target, 16) {
            Ok(s) => println!("{s}"),
            Err(e) => {
                eprintln!("ошибка: {e}");
                return 1;
            }
        },
    }
    0
}

#[cfg(not(feature = "backend"))]
fn backend_run(
    _args: &Args,
    _device: &str,
    _pid: Option<u32>,
    _addr: Option<u64>,
    _offset: u64,
    _size: Option<u64>,
) -> i32 {
    eprintln!("ошибка: бинарь собран без поддержки бэкенда (соберите с --features backend)");
    1
}

/// Полный дамп процесса в `out_path` + карта регионов в `out_path + ".map"`.
#[cfg(feature = "backend")]
fn dump_all(proc: &dyn ProcessLike, out_path: &str) -> i32 {
    let map_path = format!("{out_path}.map");
    let mut f = match File::create(out_path) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("ошибка ввода-вывода: {e}");
            return 1;
        }
    };
    let mut mf = match File::create(&map_path) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("ошибка ввода-вывода: {e}");
            return 1;
        }
    };
    let _ = writeln!(mf, "# дамп процесса pid={} name={}", proc.pid(), proc.name());
    let _ = writeln!(mf, "# va length file_offset");

    let mut file_off: u64 = 0;
    let entries = core::dump_process(
        proc,
        |va, data| {
            f.write_all(data).expect("запись .bin");
            writeln!(mf, "{:#018x} {} {}", va, data.len(), file_off).expect("запись .map");
            file_off += data.len() as u64;
        },
        None,
        DEFAULT_DUMP_CHUNK,
        DEFAULT_PROBE_SIZE,
        PAGE_SIZE,
    )
    .expect("dump_process с дефолтными параметрами не падает");

    let complete = entries.iter().filter(|e| e.complete()).count();
    let skipped = entries.iter().filter(|e| e.skipped()).count();
    let partial = entries.len() - complete - skipped;
    eprintln!(
        "[+] дамп: регионов {} — целиком {}, частично {}, пропущено {}",
        entries.len(),
        complete,
        partial,
        skipped
    );
    eprintln!(
        "[+] записано {} байт в {} (карта: {})",
        file_off, out_path, map_path
    );
    0
}
