//! Thin system-call wrappers (glibc symbols declared directly; no libc crate).

use std::io;

#[repr(C)]
struct Timespec {
    tv_sec: i64,
    tv_nsec: i64,
}

extern "C" {
    fn clock_settime(clk: i32, tp: *const Timespec) -> i32;
    fn geteuid() -> u32;
    fn setpriority(which: i32, who: u32, prio: i32) -> i32;
    fn getpriority(which: i32, who: u32) -> i32;
    fn syscall(num: i64, ...) -> i64;
    fn sched_setscheduler(pid: i32, policy: i32, param: *const SchedParam) -> i32;
    fn sched_getscheduler(pid: i32) -> i32;
    fn kill(pid: i32, sig: i32) -> i32;
    fn __errno_location() -> *mut i32;
    fn getrlimit(res: i32, rl: *mut Rlimit) -> i32;
    fn setrlimit(res: i32, rl: *const Rlimit) -> i32;
}

#[repr(C)]
struct Rlimit {
    cur: u64,
    max: u64,
}

const RLIMIT_NOFILE: i32 = 7;

/// Raise the soft open-file limit to the hard limit (esync needs many eventfds). Returns (old, new).
pub fn raise_nofile() -> io::Result<(u64, u64)> {
    let mut rl = Rlimit { cur: 0, max: 0 };
    if unsafe { getrlimit(RLIMIT_NOFILE, &mut rl) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let old = rl.cur;
    if rl.cur < rl.max {
        let new = Rlimit { cur: rl.max, max: rl.max };
        if unsafe { setrlimit(RLIMIT_NOFILE, &new) } != 0 {
            return Err(io::Error::last_os_error());
        }
        return Ok((old, rl.max));
    }
    Ok((old, old))
}

#[repr(C)]
struct SchedParam {
    sched_priority: i32,
}

const CLOCK_REALTIME: i32 = 0;
const PRIO_PROCESS: i32 = 0;
const SYS_IOPRIO_SET: i64 = 251; // x86_64
const SYS_IOPRIO_GET: i64 = 252;
const IOPRIO_WHO_PROCESS: i64 = 1;

pub const SCHED_OTHER: i32 = 0;
pub const SCHED_BATCH: i32 = 3;
pub const SCHED_IDLE: i32 = 5;

fn errno() -> i32 {
    unsafe { *__errno_location() }
}

pub fn is_root() -> bool {
    unsafe { geteuid() == 0 }
}

/// Set CLOCK_REALTIME (root only).
pub fn set_realtime(sec: i64, nsec: i64) -> io::Result<()> {
    let ts = Timespec { tv_sec: sec, tv_nsec: nsec };
    if unsafe { clock_settime(CLOCK_REALTIME, &ts) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

pub fn now_unix() -> (i64, i64) {
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    (d.as_secs() as i64, d.subsec_nanos() as i64)
}

pub fn set_nice(pid: u32, nice: i32) -> io::Result<()> {
    if unsafe { setpriority(PRIO_PROCESS, pid, nice) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

pub fn get_nice(pid: u32) -> io::Result<i32> {
    unsafe { *__errno_location() = 0 };
    let v = unsafe { getpriority(PRIO_PROCESS, pid) };
    if v == -1 && errno() != 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(v)
    }
}

/// ioprio: class 1=RT, 2=best-effort, 3=idle; data 0 (high) .. 7 (low)
pub fn set_ioprio(pid: u32, class: u32, data: u32) -> io::Result<()> {
    let v = ((class as i64) << 13) | (data as i64 & 7);
    if unsafe { syscall(SYS_IOPRIO_SET, IOPRIO_WHO_PROCESS, pid as i64, v) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

pub fn get_ioprio(pid: u32) -> io::Result<(u32, u32)> {
    let v = unsafe { syscall(SYS_IOPRIO_GET, IOPRIO_WHO_PROCESS, pid as i64) };
    if v < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok((((v >> 13) & 7) as u32, (v & 7) as u32))
    }
}

pub fn set_policy(pid: u32, policy: i32) -> io::Result<()> {
    let prm = SchedParam { sched_priority: 0 };
    if unsafe { sched_setscheduler(pid as i32, policy, &prm) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

pub fn get_policy(pid: u32) -> io::Result<i32> {
    let v = unsafe { sched_getscheduler(pid as i32) };
    if v < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(v)
    }
}

pub fn process_alive(pid: u32) -> bool {
    unsafe { kill(pid as i32, 0) == 0 || errno() == 1 /* EPERM: exists */ }
}

/// "2026-10-26T14:00:00Z" for a unix time (UTC, civil-from-days algorithm).
pub fn iso_utc(sec: i64) -> String {
    let days = sec.div_euclid(86400);
    let rem = sec.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", y, m, d, rem / 3600, (rem / 60) % 60, rem % 60)
}

pub fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

pub fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = if m > 2 { m - 3 } else { m + 9 } as i64;
    let doy = (153 * mp + 2) / 5 + d as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// Parse "YYYY-MM-DD[ T]HH:MM[:SS][Z]" (UTC) or "@<unix>".
pub fn parse_utc(s: &str) -> Option<i64> {
    let s = s.trim();
    if let Some(n) = s.strip_prefix('@') {
        return n.parse().ok();
    }
    let s = s.trim_end_matches('Z');
    let (date, time) = s.split_once(['T', ' ']).unwrap_or((s, "00:00:00"));
    let dp: Vec<&str> = date.split('-').collect();
    let tp: Vec<&str> = time.split(':').collect();
    if dp.len() != 3 || tp.len() < 2 {
        return None;
    }
    let (y, mo, d) = (dp[0].parse().ok()?, dp[1].parse().ok()?, dp[2].parse().ok()?);
    let (h, mi): (i64, i64) = (tp[0].parse().ok()?, tp[1].parse().ok()?);
    let se: i64 = tp.get(2).map(|x| x.parse().ok()).unwrap_or(Some(0))?;
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || se > 60 {
        return None;
    }
    Some(days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + se)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates() {
        assert_eq!(iso_utc(0), "1970-01-01T00:00:00Z");
        assert_eq!(iso_utc(1577836800), "2020-01-01T00:00:00Z");
        assert_eq!(parse_utc("2026-10-26T21:09:00Z"), Some(1793048940));
        assert_eq!(iso_utc(1793048940), "2026-10-26T21:09:00Z");
        assert_eq!(parse_utc("@1234"), Some(1234));
        assert_eq!(parse_utc("2026-13-01 00:00"), None);
        for t in [0i64, 951782400, 4102444799, 1709251199] {
            assert_eq!(parse_utc(&iso_utc(t)), Some(t));
        }
    }
}
