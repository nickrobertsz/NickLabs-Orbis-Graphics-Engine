"""UMR-style GPU register inspection through amdgpu debugfs (amdgpu_regs), root only.

amdgpu's debugfs register file is the same interface AMD's UMR uses: reading 4 bytes at offset
(dword index * 4) returns that MMIO register. UMR has no Liverpool ASIC table, so the GFX7
(Bonaire/Kaveri family) offsets below come from the kernel's own headers (gca/gfx_7_2_d.h,
oss/oss_2_0_d.h, gmc/gmc_7_1_d.h, bif/bif_4_1_d.h, uvd/uvd_4_2_d.h) and are decoded with the
kernel's bit masks. Reads only; nothing here writes a register."""
from __future__ import annotations

import os
import struct

from orbis_gfx import sysfs

REGS = {  # name: dword offset
    "GRBM_STATUS": 0x2004, "GRBM_STATUS2": 0x2002, "GRBM_STATUS_SE0": 0x2005, "GRBM_STATUS_SE1": 0x2006,
    "CP_STAT": 0x21A0, "CP_CPF_STATUS": 0x2087, "CP_CPC_STATUS": 0x2084, "RLC_STAT": 0x30C4,
    "CP_RB0_RPTR": 0x21C0, "CP_RB0_WPTR": 0x3045,
    "SRBM_STATUS": 0x394, "SRBM_STATUS2": 0x393, "SDMA0_STATUS_REG": 0x340D, "SDMA1_STATUS_REG": 0x360D,
    "IH_RB_RPTR": 0xF82, "IH_RB_WPTR": 0xF83, "IH_STATUS": 0xF88,
    "VM_CONTEXT1_PROTECTION_FAULT_STATUS": 0x537, "VM_CONTEXT1_PROTECTION_FAULT_ADDR": 0x53F,
    "VM_CONTEXT1_PROTECTION_FAULT_MCCLIENT": 0x539, "VM_INVALIDATE_RESPONSE": 0x51F,
    "MC_VM_FB_LOCATION": 0x809, "MC_VM_FB_OFFSET": 0x81A, "CONFIG_MEMSIZE": 0x150A,
    "UVD_STATUS": 0x3DAF, "UVD_RBC_RB_RPTR": 0x3DA4, "UVD_RBC_RB_WPTR": 0x3DA5, "UVD_CONTEXT_ID": 0x3DBD,
}

GRBM_BITS = {
    "GUI_ACTIVE": 0x80000000, "CB_BUSY": 0x40000000, "CP_BUSY": 0x20000000, "CP_COHERENCY_BUSY": 0x10000000,
    "DB_BUSY": 0x4000000, "PA_BUSY": 0x2000000, "SC_BUSY": 0x1000000, "BCI_BUSY": 0x800000, "SPI_BUSY": 0x400000,
    "WD_BUSY": 0x200000, "SX_BUSY": 0x100000, "IA_BUSY": 0x80000, "VGT_BUSY": 0x20000, "GDS_BUSY": 0x8000,
    "TA_BUSY": 0x4000, "CB_CLEAN": 0x2000, "DB_CLEAN": 0x1000,
}


def regs_file():
    g = sysfs.primary_gpu()
    if not g or not g.debugfs:
        return None
    f = g.debugfs / "amdgpu_regs"
    return f if f.exists() else None


def read_reg(fd: int, dword: int) -> int:
    data = os.pread(fd, 4, dword * 4)
    if len(data) != 4:
        raise OSError(f"short read at 0x{dword:x}")
    return struct.unpack("<I", data)[0]


def decode(name: str, v: int) -> dict:
    if name == "GRBM_STATUS":
        return {"busy_blocks": [k for k, m in GRBM_BITS.items() if v & m and not k.endswith("CLEAN")],
                "cb_clean": bool(v & GRBM_BITS["CB_CLEAN"]), "db_clean": bool(v & GRBM_BITS["DB_CLEAN"])}
    if name == "VM_CONTEXT1_PROTECTION_FAULT_STATUS":
        return {"protections": v & 0xFF, "memory_client_id": (v & 0x1FF000) >> 12,
                "rw": "write" if v & 0x1000000 else "read", "vmid": (v & 0x1E000000) >> 25, "fault": v != 0}
    if name == "VM_CONTEXT1_PROTECTION_FAULT_ADDR":
        return {"page_address": hex(v << 12)}
    if name == "VM_CONTEXT1_PROTECTION_FAULT_MCCLIENT":
        return {"client": struct.pack(">I", v).decode("ascii", "replace").strip("\x00")}
    if name == "MC_VM_FB_LOCATION":
        return {"fb_start_mb": (v & 0xFFFF) << 24 >> 20, "fb_top_mb": ((v >> 16) << 24 | 0xFFFFFF) >> 20}
    if name == "CONFIG_MEMSIZE":
        return {"vram_mb": v}
    if name == "SRBM_STATUS2":
        return {"sdma0_busy": bool(v & 0x20), "sdma1_busy": bool(v & 0x40)}
    if name == "SRBM_STATUS":
        return {"uvd_busy": bool(v & 0x80000), "vmc_busy": bool(v & 0x100), "mcb_busy": bool(v & 0x200)}
    return {}


def dump(names: list[str] | None = None) -> dict:
    f = regs_file()
    if not f:
        return {"available": False, "why": "debugfs amdgpu_regs not available (needs root and debugfs)"}
    out = {"available": True, "source": str(f), "registers": {}}
    fd = os.open(f, os.O_RDONLY)
    try:
        for name in names or list(REGS):
            off = REGS[name]
            try:
                v = read_reg(fd, off)
                out["registers"][name] = {"offset": hex(off), "value": f"0x{v:08x}", **decode(name, v)}
            except OSError as e:
                out["registers"][name] = {"offset": hex(off), "error": str(e)}
    finally:
        os.close(fd)
    return out
