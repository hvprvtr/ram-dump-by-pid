//! Адаптер поверх крейта `memprocfs` (нативные vmm.dll/leechcore.dll).
//! Компилируется только с фичей `backend`. Изолирует все вызовы memprocfs,
//! превращая их в объекты [`ProcessLike`], понятные чистой логике.

use memprocfs::*;
use rampidreader::{ProcessLike, Vad};

/// Открыть устройство физпамяти. `vmm_dll` — путь к нативной vmm.dll
/// (берём из установленного пакета memprocfs), `device` — строка `PMEM://...`.
pub fn open<'a>(vmm_dll: &str, device: &str) -> Result<Vmm<'a>, String> {
    let args = vec!["-device", device];
    Vmm::new(vmm_dll, &args).map_err(|e| format!("{e}"))
}

/// Обёртка над процессом memprocfs, реализующая [`ProcessLike`].
pub struct MppProcess<'a> {
    proc: VmmProcess<'a>,
    pid: u32,
    name: String,
}

impl<'a> ProcessLike for MppProcess<'a> {
    fn pid(&self) -> u32 {
        self.pid
    }

    fn name(&self) -> &str {
        &self.name
    }

    fn read(&self, addr: u64, size: usize) -> Option<Vec<u8>> {
        // ВАЖНО: mem_read_ex крейта возвращает полный буфер size, ИГНОРИРУЯ число
        // реально прочитанных байт (дыры приходят нулями) — это ломает и пробу, и
        // обнаружение дыр (дамп раздувается до гигабайт). mem_read_into отдаёт
        // фактическое cb_read, как нативный VMMDLL_MemReadEx, — берём его и режем
        // буфер по факту. FLAG_NOCACHE — для «живой» памяти.
        let mut buf = vec![0u8; size];
        match self.proc.mem_read_into(addr, FLAG_NOCACHE, &mut buf) {
            Ok(n) if n > 0 => {
                buf.truncate(n);
                Some(buf)
            }
            _ => None,
        }
    }

    fn module_base(&self, module: &str) -> Option<u64> {
        let mods = self.proc.map_module(false, false).ok()?;
        let target = module.to_lowercase();
        mods.into_iter()
            .find(|m| m.name.to_lowercase() == target)
            .map(|m| m.va_base)
    }

    fn vads(&self) -> Vec<Vad> {
        match self.proc.map_vad(true) {
            Ok(list) => list
                .into_iter()
                .map(|v| {
                    let prot = if v.is_mem_commit { "commit" } else { "reserve" };
                    Vad::new(v.va_start, v.va_end, prot, &v.info)
                })
                .collect(),
            Err(_) => Vec::new(),
        }
    }
}

/// Перечислить процессы устройства.
pub fn processes<'a>(vmm: &'a Vmm<'a>) -> Vec<MppProcess<'a>> {
    let mut out = Vec::new();
    if let Ok(list) = vmm.process_list() {
        for p in list {
            let pid = p.pid;
            let name = p.info().map(|i| i.name).unwrap_or_default();
            out.push(MppProcess { proc: p, pid, name });
        }
    }
    out
}
