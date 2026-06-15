//! Чистая логика инструмента — без зависимости от memprocfs.
//!
//! Все функции работают с абстракцией процесса (трейт [`ProcessLike`]), поэтому
//! покрываются юнит-тестами на моках без реального драйвера. Зеркало
//! `rampidreader/core.py` из Python-версии.

use std::fmt;

/// Верхняя граница одного чтения, чтобы случайный `--size 0xFFFFFFFFFFFFFFFF`
/// не привёл к попытке аллоцировать всю память. 256 МиБ — с запасом для региона.
pub const MAX_READ_SIZE: u64 = 256 * 1024 * 1024;

/// Размер чанка при полном дампе: большие регионы читаем по кускам.
pub const DEFAULT_DUMP_CHUNK: usize = 4 * 1024 * 1024;

/// Проба: одна страница в начале региона (отсекает reserved-регионы).
pub const DEFAULT_PROBE_SIZE: usize = 4096;

/// Шаг пропуска при дыре внутри региона (страница).
pub const PAGE_SIZE: u64 = 4096;

/// Ошибки чистой логики. `InvalidArg` ≈ Python `ValueError`, `ReadFailed` ≈ `RuntimeError`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CoreError {
    InvalidArg(String),
    ReadFailed(String),
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CoreError::InvalidArg(m) => write!(f, "{m}"),
            CoreError::ReadFailed(m) => write!(f, "{m}"),
        }
    }
}

impl std::error::Error for CoreError {}

fn invalid(m: impl Into<String>) -> CoreError {
    CoreError::InvalidArg(m.into())
}

/// Регион виртуального адресного пространства процесса.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Vad {
    pub start: u64,
    pub end: u64,
    pub protection: String,
    pub tag: String,
}

impl Vad {
    pub fn new(start: u64, end: u64, protection: &str, tag: &str) -> Self {
        Vad {
            start,
            end,
            protection: protection.to_string(),
            tag: tag.to_string(),
        }
    }

    /// Размер региона. Для вырожденного (`end <= start`) — 0.
    pub fn size(&self) -> u64 {
        self.end.saturating_sub(self.start)
    }
}

/// Итог дампа одного региона: сколько запросили и сколько реально прочли.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DumpRegion {
    pub start: u64,
    pub end: u64,
    pub requested: u64,
    pub written: u64,
    pub error: String,
}

impl DumpRegion {
    /// Регион снят целиком (без дыр и ошибок).
    pub fn complete(&self) -> bool {
        self.error.is_empty() && self.written == self.requested
    }

    /// Из региона не удалось прочитать ни байта.
    pub fn skipped(&self) -> bool {
        self.written == 0
    }
}

/// Минимальный контракт процесса, которым пользуется логика.
///
/// `read` возвращает `Some(bytes)` (возможно, короче запрошенного при частичном
/// чтении) или `None`, если страница недоступна. В отличие от Python здесь нет
/// «исключений чтения» — недоступность выражается через `None`, что устраняет
/// целый класс ошибок на уровне типов.
pub trait ProcessLike {
    fn pid(&self) -> u32;
    fn name(&self) -> &str;
    fn read(&self, addr: u64, size: usize) -> Option<Vec<u8>>;
    fn module_base(&self, module: &str) -> Option<u64>;
    fn vads(&self) -> Vec<Vad>;
}

/// Разобрать целое в dec или hex/oct/bin (`0x`/`0o`/`0b`).
pub fn parse_int(text: &str) -> Result<u64, CoreError> {
    let s = text.trim();
    if s.is_empty() {
        return Err(invalid("ожидалось число, получена пустая строка"));
    }
    let strip_ci = |s: &'_ str, p: &str| -> Option<usize> {
        if s.len() >= p.len() && s[..p.len()].eq_ignore_ascii_case(p) {
            Some(p.len())
        } else {
            None
        }
    };
    let (radix, digits): (u32, &str) = if let Some(n) = strip_ci(s, "0x") {
        (16, &s[n..])
    } else if let Some(n) = strip_ci(s, "0o") {
        (8, &s[n..])
    } else if let Some(n) = strip_ci(s, "0b") {
        (2, &s[n..])
    } else {
        (10, s)
    };
    if digits.is_empty() {
        return Err(invalid(format!("некорректное число: {text:?}")));
    }
    u64::from_str_radix(digits, radix).map_err(|_| invalid(format!("некорректное число: {text:?}")))
}

/// Выбрать единственный процесс по PID или имени.
pub fn select_process<'a, P: ProcessLike>(
    processes: &'a [P],
    pid: Option<u32>,
    name: Option<&str>,
) -> Result<&'a P, CoreError> {
    match (pid, name) {
        (None, None) => return Err(invalid("нужно указать либо pid, либо name")),
        (Some(_), Some(_)) => {
            return Err(invalid(
                "pid и name взаимоисключающие — задайте что-то одно",
            ))
        }
        _ => {}
    }

    if let Some(pid) = pid {
        return processes
            .iter()
            .find(|p| p.pid() == pid)
            .ok_or_else(|| invalid(format!("процесс с pid={pid} не найден")));
    }

    let target = name.unwrap().to_lowercase();
    let matches: Vec<&P> = processes
        .iter()
        .filter(|p| p.name().to_lowercase() == target)
        .collect();
    match matches.len() {
        0 => Err(invalid(format!(
            "процесс с именем {:?} не найден",
            name.unwrap()
        ))),
        1 => Ok(matches[0]),
        n => {
            let pids: Vec<String> = matches.iter().map(|p| p.pid().to_string()).collect();
            Err(invalid(format!(
                "имя {:?} неоднозначно: найдено {n} процессов (pid: {}) — уточните через --pid",
                name.unwrap(),
                pids.join(", ")
            )))
        }
    }
}

/// Прочитать регион памяти процесса с валидацией параметров.
pub fn read_region(
    process: &dyn ProcessLike,
    addr: u64,
    size: u64,
    max_size: u64,
) -> Result<Vec<u8>, CoreError> {
    if size == 0 {
        return Err(invalid(format!("размер должен быть положительным: {size}")));
    }
    if size > max_size {
        return Err(invalid(format!(
            "размер {size} превышает лимит {max_size} байт ({} МиБ)",
            max_size / (1024 * 1024)
        )));
    }
    if addr.checked_add(size).is_none() {
        return Err(invalid(format!(
            "регион [{addr:#x}..) выходит за пределы адресного пространства x64"
        )));
    }

    match process.read(addr, size as usize) {
        None => Err(CoreError::ReadFailed(format!(
            "чтение не удалось: адрес {addr:#x} недоступен (страница не отображена или выгружена)"
        ))),
        Some(data) => Ok(data),
    }
}

/// Классический hex-дамп: смещение | байты | ASCII.
pub fn format_hexdump(data: &[u8], base_addr: u64, width: usize) -> Result<String, CoreError> {
    if width == 0 {
        return Err(invalid("ширина строки должна быть положительной: 0"));
    }
    if data.is_empty() {
        return Ok(String::new());
    }
    let mut lines: Vec<String> = Vec::new();
    let mut offset = 0usize;
    while offset < data.len() {
        let chunk = &data[offset..(offset + width).min(data.len())];
        let hex_part = chunk
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<Vec<_>>()
            .join(" ");
        let hex_padded = format!("{:<w$}", hex_part, w = width * 3 - 1);
        let ascii_part: String = chunk
            .iter()
            .map(|&b| if (0x20..0x7F).contains(&b) { b as char } else { '.' })
            .collect();
        lines.push(format!(
            "{:016x}  {}  |{}|",
            base_addr + offset as u64,
            hex_padded,
            ascii_part
        ));
        offset += width;
    }
    Ok(lines.join("\n"))
}

/// Таблица регионов адресного пространства.
pub fn format_vads(vads: &[Vad]) -> String {
    if vads.is_empty() {
        return "(регионов нет)".to_string();
    }
    let mut lines = vec![format!(
        "{:>18}  {:>18}  {:>12}  {:<8}  TAG",
        "START", "END", "SIZE", "PROT"
    )];
    for v in vads {
        lines.push(format!(
            "{:#018x}  {:#018x}  {:>12}  {:<8}  {}",
            v.start,
            v.end,
            v.size(),
            v.protection,
            v.tag
        ));
    }
    lines.join("\n")
}

/// Вычислить целевой адрес: либо явный `addr`, либо `база модуля + offset`.
pub fn resolve_address(
    process: &dyn ProcessLike,
    addr: Option<u64>,
    module: Option<&str>,
    offset: u64,
) -> Result<u64, CoreError> {
    match (addr, module) {
        (None, None) => return Err(invalid("нужно указать либо адрес, либо модуль")),
        (Some(_), Some(_)) => {
            return Err(invalid(
                "адрес и модуль взаимоисключающие — задайте что-то одно",
            ))
        }
        _ => {}
    }
    if let Some(addr) = addr {
        return Ok(addr.wrapping_add(offset));
    }
    let module = module.unwrap();
    match process.module_base(module) {
        Some(base) => Ok(base.wrapping_add(offset)),
        None => Err(invalid(format!("модуль {module:?} не найден в процессе"))),
    }
}

/// Снять все читаемые регионы процесса, отдавая прочитанные блоки в `writer`.
///
/// Перед чтением региона берётся «проба» первой страницы: если она недоступна,
/// регион (как правило, reserved/незакоммиченная память) пропускается целиком,
/// без перебора чанками. Чтение идёт чанками, но курсор сдвигается на число
/// **реально прочитанных** байт; упёршись в дыру (пустой ответ) — на `hole_skip`.
/// Так committed-данные за дырами не теряются.
pub fn dump_process<F: FnMut(u64, &[u8])>(
    process: &dyn ProcessLike,
    mut writer: F,
    regions: Option<Vec<Vad>>,
    chunk: usize,
    probe_size: usize,
    hole_skip: u64,
) -> Result<Vec<DumpRegion>, CoreError> {
    if chunk == 0 {
        return Err(invalid("размер чанка должен быть положительным целым: 0"));
    }
    if hole_skip == 0 {
        return Err(invalid(
            "шаг пропуска дыры должен быть положительным целым: 0",
        ));
    }

    let regions = regions.unwrap_or_else(|| process.vads());
    let mut entries: Vec<DumpRegion> = Vec::new();

    for v in &regions {
        if v.end <= v.start {
            entries.push(DumpRegion {
                start: v.start,
                end: v.end,
                requested: 0,
                written: 0,
                error: "пустой или некорректный регион".to_string(),
            });
            continue;
        }
        let size = v.end - v.start;

        // Проба начала региона.
        if probe_size > 0 {
            let probe_n = (probe_size as u64).min(size) as usize;
            match process.read(v.start, probe_n) {
                Some(ref head) if !head.is_empty() => {}
                _ => {
                    entries.push(DumpRegion {
                        start: v.start,
                        end: v.end,
                        requested: size,
                        written: 0,
                        error: "регион недоступен (проба не прошла)".to_string(),
                    });
                    continue;
                }
            }
        }

        let mut written = 0u64;
        let mut offset = 0u64;
        while offset < size {
            let n = (chunk as u64).min(size - offset) as usize;
            match process.read(v.start + offset, n) {
                Some(data) if !data.is_empty() => {
                    writer(v.start + offset, &data);
                    written += data.len() as u64;
                    // Сдвиг на реально прочитанное: остаток чанка ещё не прочитан
                    // (там, скорее всего, начинается дыра).
                    offset += data.len() as u64;
                }
                _ => {
                    // Дыра/недоступная страница — пропускаем её и пробуем дальше.
                    offset += hole_skip;
                }
            }
        }

        entries.push(DumpRegion {
            start: v.start,
            end: v.end,
            requested: size,
            written,
            error: String::new(),
        });
    }
    Ok(entries)
}

// =========================================================================== //
// Тесты — аналоги test_core.py. Обычные + злые (Angry Tests).
//
// Часть «злых» тестов Python (отрицательный адрес/размер, bool вместо int,
// None, исключения чтения) на уровне типов Rust невозможна и устранена by
// design: addr/size — беззнаковые, read возвращает Option, а не бросает.
// =========================================================================== //
#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    // --- Моки --- //
    #[derive(Debug)]
    struct FakeProcess {
        pid: u32,
        name: String,
        mem: HashMap<u64, Vec<u8>>,
        modules: HashMap<String, u64>,
        vads: Vec<Vad>,
    }

    impl FakeProcess {
        fn new(pid: u32, name: &str) -> Self {
            FakeProcess {
                pid,
                name: name.to_string(),
                mem: HashMap::new(),
                modules: HashMap::new(),
                vads: Vec::new(),
            }
        }
        fn with_mem(mut self, addr: u64, data: &[u8]) -> Self {
            self.mem.insert(addr, data.to_vec());
            self
        }
        fn with_module(mut self, m: &str, base: u64) -> Self {
            self.modules.insert(m.to_string(), base);
            self
        }
        fn with_vads(mut self, vads: Vec<Vad>) -> Self {
            self.vads = vads;
            self
        }
    }

    impl ProcessLike for FakeProcess {
        fn pid(&self) -> u32 {
            self.pid
        }
        fn name(&self) -> &str {
            &self.name
        }
        fn read(&self, addr: u64, size: usize) -> Option<Vec<u8>> {
            self.mem.get(&addr).map(|d| d[..size.min(d.len())].to_vec())
        }
        fn module_base(&self, module: &str) -> Option<u64> {
            self.modules.get(module).copied()
        }
        fn vads(&self) -> Vec<Vad> {
            self.vads.clone()
        }
    }

    /// Мок с «сегментами»: read отдаёт байты по любому адресу из сегмента —
    /// нужен для чанкинга и дыр (аналог RangeProcess из Python).
    struct RangeProcess {
        segments: HashMap<u64, Vec<u8>>,
        vads: Vec<Vad>,
        read_calls: std::cell::RefCell<Vec<(u64, usize)>>,
    }
    impl RangeProcess {
        fn new(segments: HashMap<u64, Vec<u8>>, vads: Vec<Vad>) -> Self {
            RangeProcess {
                segments,
                vads,
                read_calls: std::cell::RefCell::new(Vec::new()),
            }
        }
    }
    impl ProcessLike for RangeProcess {
        fn pid(&self) -> u32 {
            1
        }
        fn name(&self) -> &str {
            "a.exe"
        }
        fn read(&self, addr: u64, size: usize) -> Option<Vec<u8>> {
            self.read_calls.borrow_mut().push((addr, size));
            for (base, blob) in &self.segments {
                if *base <= addr && addr < base + blob.len() as u64 {
                    let start = (addr - base) as usize;
                    let end = (start + size).min(blob.len());
                    return Some(blob[start..end].to_vec());
                }
            }
            None
        }
        fn module_base(&self, _m: &str) -> Option<u64> {
            None
        }
        fn vads(&self) -> Vec<Vad> {
            self.vads.clone()
        }
    }

    // --- parse_int --- //
    #[test]
    fn parse_int_ok() {
        for (t, e) in [
            ("0", 0u64),
            ("1234", 1234),
            ("0x10", 16),
            ("0xDEADBEEF", 0xDEADBEEF),
            ("  42  ", 42),
            ("0o17", 15),
            ("0b1010", 10),
        ] {
            assert_eq!(parse_int(t).unwrap(), e, "input {t:?}");
        }
    }

    #[test]
    fn parse_int_rejects_garbage() {
        for bad in ["", "   ", "abc", "0xZZ", "12.5", "1,000", "0x", "--5", "1 2"] {
            assert!(parse_int(bad).is_err(), "должно падать: {bad:?}");
        }
    }

    // --- select_process --- //
    #[test]
    fn select_by_pid() {
        let procs = [FakeProcess::new(1, "a.exe"), FakeProcess::new(2, "b.exe")];
        assert_eq!(select_process(&procs, Some(2), None).unwrap().pid(), 2);
    }

    #[test]
    fn select_by_name_case_insensitive() {
        let procs = [FakeProcess::new(10, "Notepad.exe")];
        assert_eq!(
            select_process(&procs, None, Some("notepad.exe"))
                .unwrap()
                .pid(),
            10
        );
    }

    #[test]
    fn select_requires_a_criterion() {
        let procs = [FakeProcess::new(1, "a.exe")];
        assert!(select_process(&procs, None, None).is_err());
    }

    #[test]
    fn select_rejects_both_criteria() {
        let procs = [FakeProcess::new(1, "a.exe")];
        let e = select_process(&procs, Some(1), Some("a.exe")).unwrap_err();
        assert!(format!("{e}").contains("взаимоисключающ"));
    }

    #[test]
    fn select_pid_not_found() {
        let procs = [FakeProcess::new(1, "a.exe")];
        let e = select_process(&procs, Some(999), None).unwrap_err();
        assert!(format!("{e}").contains("не найден"));
    }

    #[test]
    fn select_name_not_found() {
        let procs = [FakeProcess::new(1, "a.exe")];
        let e = select_process(&procs, None, Some("ghost.exe")).unwrap_err();
        assert!(format!("{e}").contains("не найден"));
    }

    #[test]
    fn select_name_ambiguous_lists_pids() {
        let procs = [
            FakeProcess::new(11, "chrome.exe"),
            FakeProcess::new(22, "chrome.exe"),
        ];
        let e = select_process(&procs, None, Some("chrome.exe")).unwrap_err();
        assert!(format!("{e}").contains("неоднозначно"));
    }

    #[test]
    fn select_empty_process_list() {
        let procs: [FakeProcess; 0] = [];
        assert!(select_process(&procs, Some(1), None).is_err());
    }

    // --- read_region --- //
    #[test]
    fn read_region_ok() {
        let p = FakeProcess::new(1, "a.exe").with_mem(0x1000, &[0xde, 0xad, 0xbe, 0xef]);
        assert_eq!(
            read_region(&p, 0x1000, 4, MAX_READ_SIZE).unwrap(),
            vec![0xde, 0xad, 0xbe, 0xef]
        );
    }

    #[test]
    fn read_region_partial_returns_what_it_got() {
        let p = FakeProcess::new(1, "a.exe").with_mem(0x1000, &[1, 2]);
        assert_eq!(read_region(&p, 0x1000, 16, MAX_READ_SIZE).unwrap(), vec![1, 2]);
    }

    #[test]
    fn read_region_zero_size() {
        let p = FakeProcess::new(1, "a.exe").with_mem(0x1000, b"x");
        let e = read_region(&p, 0x1000, 0, MAX_READ_SIZE).unwrap_err();
        assert!(format!("{e}").contains("положительн"));
    }

    #[test]
    fn read_region_size_over_limit() {
        let p = FakeProcess::new(1, "a.exe");
        let e = read_region(&p, 0x1000, MAX_READ_SIZE + 1, MAX_READ_SIZE).unwrap_err();
        assert!(format!("{e}").contains("превышает лимит"));
    }

    #[test]
    fn read_region_address_space_overflow() {
        let p = FakeProcess::new(1, "a.exe");
        let e = read_region(&p, u64::MAX - 3, 8, MAX_READ_SIZE).unwrap_err();
        assert!(format!("{e}").contains("адресного пространства"));
    }

    #[test]
    fn read_region_unmapped_is_read_failed() {
        let p = FakeProcess::new(1, "a.exe");
        let e = read_region(&p, 0x4000, 16, MAX_READ_SIZE).unwrap_err();
        assert!(matches!(e, CoreError::ReadFailed(_)));
        assert!(format!("{e}").contains("недоступен"));
    }

    // --- resolve_address --- //
    #[test]
    fn resolve_explicit_addr_with_offset() {
        let p = FakeProcess::new(1, "a.exe");
        assert_eq!(resolve_address(&p, Some(0x1000), None, 0x10).unwrap(), 0x1010);
    }

    #[test]
    fn resolve_from_module_base() {
        let p = FakeProcess::new(1, "a.exe").with_module("ntdll.dll", 0x7FF000000000);
        assert_eq!(
            resolve_address(&p, None, Some("ntdll.dll"), 0x20).unwrap(),
            0x7FF000000020
        );
    }

    #[test]
    fn resolve_requires_something() {
        let p = FakeProcess::new(1, "a.exe");
        assert!(resolve_address(&p, None, None, 0).is_err());
    }

    #[test]
    fn resolve_rejects_both() {
        let p = FakeProcess::new(1, "a.exe").with_module("ntdll.dll", 0x1000);
        let e = resolve_address(&p, Some(0x1000), Some("ntdll.dll"), 0).unwrap_err();
        assert!(format!("{e}").contains("взаимоисключающ"));
    }

    #[test]
    fn resolve_module_not_found() {
        let p = FakeProcess::new(1, "a.exe");
        let e = resolve_address(&p, None, Some("missing.dll"), 0).unwrap_err();
        assert!(format!("{e}").contains("не найден"));
    }

    // --- format_hexdump --- //
    #[test]
    fn hexdump_empty_is_empty_string() {
        assert_eq!(format_hexdump(b"", 0, 16).unwrap(), "");
    }

    #[test]
    fn hexdump_basic_layout() {
        let out = format_hexdump(b"ABC", 0x1000, 16).unwrap();
        assert!(out.starts_with("0000000000001000  "));
        assert!(out.contains("41 42 43"));
        assert!(out.ends_with("|ABC|"));
    }

    #[test]
    fn hexdump_non_printable_becomes_dot() {
        let out = format_hexdump(&[0x00, 0x01, 0xff], 0, 16).unwrap();
        assert!(out.ends_with("|...|"));
    }

    #[test]
    fn hexdump_multiline_wraps_at_width() {
        let out = format_hexdump(&[0u8; 20], 0, 16).unwrap();
        assert_eq!(out.lines().count(), 2);
    }

    #[test]
    fn hexdump_rejects_zero_width() {
        assert!(format_hexdump(b"abc", 0, 0).is_err());
    }

    // --- format_vads --- //
    #[test]
    fn format_vads_empty() {
        assert_eq!(format_vads(&[]), "(регионов нет)");
    }

    #[test]
    fn format_vads_contains_region() {
        let out = format_vads(&[Vad::new(0x1000, 0x2000, "rw-", "Heap")]);
        assert!(out.contains("Heap") && out.contains("0x0000000000001000"));
    }

    #[test]
    fn vad_size_computed() {
        assert_eq!(Vad::new(0x1000, 0x1800, "r--", "x").size(), 0x800);
    }

    // --- dump_process --- //
    #[test]
    fn dump_basic_single_region() {
        let p = RangeProcess::new(
            HashMap::from([(0x1000u64, b"ABCD".to_vec())]),
            vec![Vad::new(0x1000, 0x1004, "rw-", "Heap")],
        );
        let mut blocks: Vec<(u64, Vec<u8>)> = Vec::new();
        let entries = dump_process(
            &p,
            |va, d| blocks.push((va, d.to_vec())),
            None,
            DEFAULT_DUMP_CHUNK,
            DEFAULT_PROBE_SIZE,
            PAGE_SIZE,
        )
        .unwrap();
        assert_eq!(blocks, vec![(0x1000, b"ABCD".to_vec())]);
        assert_eq!(entries.len(), 1);
        assert!(entries[0].complete() && entries[0].written == 4);
    }

    #[test]
    fn dump_multiple_regions() {
        let p = RangeProcess::new(
            HashMap::from([(0x1000u64, b"AAAA".to_vec()), (0x5000u64, b"BB".to_vec())]),
            vec![
                Vad::new(0x1000, 0x1004, "rw-", "a"),
                Vad::new(0x5000, 0x5002, "r--", "b"),
            ],
        );
        let mut w: Vec<(u64, Vec<u8>)> = Vec::new();
        let entries = dump_process(
            &p,
            |va, d| w.push((va, d.to_vec())),
            None,
            DEFAULT_DUMP_CHUNK,
            DEFAULT_PROBE_SIZE,
            PAGE_SIZE,
        )
        .unwrap();
        assert_eq!(w, vec![(0x1000, b"AAAA".to_vec()), (0x5000, b"BB".to_vec())]);
        assert!(entries.iter().all(|e| e.complete()));
    }

    #[test]
    fn dump_uses_process_vads_by_default() {
        let p = RangeProcess::new(
            HashMap::from([(0x2000u64, b"XYZ".to_vec())]),
            vec![Vad::new(0x2000, 0x2003, "rw-", "v")],
        );
        let entries =
            dump_process(&p, |_, _| {}, None, DEFAULT_DUMP_CHUNK, DEFAULT_PROBE_SIZE, PAGE_SIZE)
                .unwrap();
        assert_eq!(entries.iter().map(|e| e.start).collect::<Vec<_>>(), vec![0x2000]);
    }

    #[test]
    fn dump_explicit_regions_override_vads() {
        let p = RangeProcess::new(
            HashMap::from([(0x9000u64, b"ZZZZ".to_vec())]),
            vec![Vad::new(0x1000, 0x1004, "rw-", "ignored")],
        );
        let entries = dump_process(
            &p,
            |_, _| {},
            Some(vec![Vad::new(0x9000, 0x9004, "rw-", "use")]),
            DEFAULT_DUMP_CHUNK,
            DEFAULT_PROBE_SIZE,
            PAGE_SIZE,
        )
        .unwrap();
        assert_eq!(entries.iter().map(|e| e.start).collect::<Vec<_>>(), vec![0x9000]);
        assert!(entries[0].complete());
    }

    #[test]
    fn dump_chunks_large_region() {
        let p = RangeProcess::new(
            HashMap::from([(0x1000u64, b"0123456789".to_vec())]),
            vec![Vad::new(0x1000, 0x100A, "rw-", "big")],
        );
        let mut blocks: Vec<(u64, Vec<u8>)> = Vec::new();
        let entries = dump_process(
            &p,
            |va, d| blocks.push((va, d.to_vec())),
            None,
            4,
            DEFAULT_PROBE_SIZE,
            PAGE_SIZE,
        )
        .unwrap();
        assert_eq!(
            blocks,
            vec![
                (0x1000, b"0123".to_vec()),
                (0x1004, b"4567".to_vec()),
                (0x1008, b"89".to_vec()),
            ]
        );
        assert!(entries[0].complete() && entries[0].written == 10);
    }

    #[test]
    fn dump_no_regions_writes_nothing() {
        let p = RangeProcess::new(HashMap::new(), vec![]);
        let mut calls = 0;
        let entries =
            dump_process(&p, |_, _| calls += 1, None, DEFAULT_DUMP_CHUNK, DEFAULT_PROBE_SIZE, PAGE_SIZE)
                .unwrap();
        assert!(entries.is_empty() && calls == 0);
    }

    #[test]
    fn dump_skips_fully_unreadable_region() {
        let p = RangeProcess::new(HashMap::new(), vec![Vad::new(0x4000, 0x4100, "rw-", "gone")]);
        let mut calls = 0;
        let entries =
            dump_process(&p, |_, _| calls += 1, None, DEFAULT_DUMP_CHUNK, DEFAULT_PROBE_SIZE, PAGE_SIZE)
                .unwrap();
        assert_eq!(calls, 0);
        assert!(entries[0].skipped() && !entries[0].complete());
        assert_eq!(entries[0].written, 0);
    }

    #[test]
    fn dump_partial_read_does_not_skip_data_after_hole() {
        // Регрессия: read отдаёт данные до первой дыры и обрывается. Курсор
        // обязан двигаться на ПРОЧИТАННОЕ, иначе данные за дырой теряются.
        let p = RangeProcess::new(
            HashMap::from([(0x1000u64, b"AAAA".to_vec()), (0x1008u64, b"BBBB".to_vec())]),
            vec![Vad::new(0x1000, 0x100C, "rw-", "frag")],
        );
        let mut blocks: Vec<(u64, Vec<u8>)> = Vec::new();
        let entries =
            dump_process(&p, |va, d| blocks.push((va, d.to_vec())), None, 64, DEFAULT_PROBE_SIZE, 4)
                .unwrap();
        assert_eq!(
            blocks,
            vec![(0x1000, b"AAAA".to_vec()), (0x1008, b"BBBB".to_vec())]
        );
        assert_eq!(entries[0].written, 8);
    }

    #[test]
    fn dump_hole_in_middle_is_partial() {
        // 12 байт, дыра в [0x1004,0x1008), данные по краям; чанк=4, hole_skip=4.
        let p = RangeProcess::new(
            HashMap::from([(0x1000u64, b"AAAA".to_vec()), (0x1008u64, b"CCCC".to_vec())]),
            vec![Vad::new(0x1000, 0x100C, "rw-", "swiss")],
        );
        let mut blocks: Vec<(u64, usize)> = Vec::new();
        let entries =
            dump_process(&p, |va, d| blocks.push((va, d.len())), None, 4, DEFAULT_PROBE_SIZE, 4)
                .unwrap();
        assert_eq!(blocks, vec![(0x1000, 4), (0x1008, 4)]);
        assert_eq!(entries[0].written, 8);
        assert_eq!(entries[0].requested, 0xC);
        assert!(!entries[0].complete() && !entries[0].skipped());
    }

    #[test]
    fn dump_one_bad_region_does_not_stop_others() {
        // Первый регион нечитаем (нет сегмента), второй — есть.
        let p = RangeProcess::new(
            HashMap::from([(0x5000u64, b"OK!!".to_vec())]),
            vec![
                Vad::new(0x1000, 0x1004, "rw-", "bad"),
                Vad::new(0x5000, 0x5004, "rw-", "good"),
            ],
        );
        let mut w: Vec<u64> = Vec::new();
        let entries =
            dump_process(&p, |va, _| w.push(va), None, DEFAULT_DUMP_CHUNK, DEFAULT_PROBE_SIZE, PAGE_SIZE)
                .unwrap();
        assert_eq!(w, vec![0x5000]);
        assert!(entries[0].skipped() && entries[1].complete());
    }

    #[test]
    fn dump_degenerate_region_recorded_not_read() {
        for bad_end in [0x1000u64, 0x0FFF, 0] {
            let p = RangeProcess::new(HashMap::new(), vec![Vad::new(0x1000, bad_end, "rw-", "weird")]);
            let mut calls = 0;
            let entries =
                dump_process(&p, |_, _| calls += 1, None, DEFAULT_DUMP_CHUNK, DEFAULT_PROBE_SIZE, PAGE_SIZE)
                    .unwrap();
            assert_eq!(calls, 0);
            assert!(!entries[0].error.is_empty() && entries[0].requested == 0);
        }
    }

    #[test]
    fn dump_rejects_zero_chunk() {
        let p = RangeProcess::new(HashMap::new(), vec![Vad::new(0x1000, 0x1004, "rw-", "x")]);
        let e = dump_process(&p, |_, _| {}, None, 0, DEFAULT_PROBE_SIZE, PAGE_SIZE).unwrap_err();
        assert!(format!("{e}").contains("чанк"));
    }

    #[test]
    fn dump_rejects_zero_hole_skip() {
        let p = RangeProcess::new(HashMap::new(), vec![Vad::new(0x1000, 0x1004, "rw-", "x")]);
        let e = dump_process(&p, |_, _| {}, None, DEFAULT_DUMP_CHUNK, DEFAULT_PROBE_SIZE, 0).unwrap_err();
        assert!(format!("{e}").contains("пропуск"));
    }

    #[test]
    fn dump_probe_skips_huge_unreadable_region_in_one_read() {
        let huge = Vad::new(0x1000, 0x1000 + 4 * 1024 * 1024 * 1024, "rw-", "reserved");
        let p = RangeProcess::new(HashMap::new(), vec![huge]);
        let mut calls = 0;
        let entries =
            dump_process(&p, |_, _| calls += 1, None, 4096, DEFAULT_PROBE_SIZE, PAGE_SIZE).unwrap();
        assert_eq!(p.read_calls.borrow().len(), 1); // только проба
        assert_eq!(calls, 0);
        assert!(entries[0].skipped() && entries[0].error.contains("проба"));
    }

    #[test]
    fn dump_probe_disabled_falls_back_to_chunk_scan() {
        let p = RangeProcess::new(HashMap::new(), vec![Vad::new(0x1000, 0x100C, "rw-", "x")]);
        let entries = dump_process(&p, |_, _| {}, None, 4, 0, 4).unwrap();
        assert_eq!(p.read_calls.borrow().len(), 3); // без пробы — перебор по страницам
        assert!(entries[0].skipped());
    }

    // --- DumpRegion флаги --- //
    #[test]
    fn dump_region_complete_flag() {
        let mk = |req, wr, err: &str| DumpRegion {
            start: 0x1000,
            end: 0x1004,
            requested: req,
            written: wr,
            error: err.to_string(),
        };
        assert!(mk(4, 4, "").complete());
        assert!(!mk(4, 2, "").complete());
        assert!(!mk(4, 4, "boom").complete());
    }

    #[test]
    fn dump_region_skipped_flag() {
        let mk = |wr| DumpRegion {
            start: 0x1000,
            end: 0x1004,
            requested: 4,
            written: wr,
            error: String::new(),
        };
        assert!(mk(0).skipped());
        assert!(!mk(1).skipped());
    }
}
