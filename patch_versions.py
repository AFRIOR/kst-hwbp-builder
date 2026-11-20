#!/usr/bin/env python3
"""patch_versions.py - 用设备实测值修正 .ko

两个功能：
1. __versions 段：CRC 覆盖成设备内核真实值
   （源码 5.10.81 vs 设备 5.10.236，genksyms 结果可能不同）
2. .modinfo 段：vermagic 覆盖成设备内核实际值
   （版本号/本地后缀不同会导致 "version magic ... should be ..." 拒载）

设备实测基准（Xiaomi Pad 6 Pro / 5.10.236-android12-9）：
  uname -r = 5.10.236-android12-9-00003-gfb24cf99ad97-ab14313284
"""
import struct
import sys
import os

DEVICE_VERMAGIC = (
    "5.10.236-android12-9-00003-gfb24cf99ad97-ab14313284"
    " SMP preempt mod_unload modversions aarch64"
)

DEVICE_CRC = {
    "module_layout":                    0x7c24b32d,
    "printk":                           0xc5850110,
    "_printk":                          0xc5850110,
    "perf_event_create_kernel_counter": 0xbcfefbf3,
    "perf_event_enable":                0x085a0bc8,
    "perf_event_disable":               0x7d609757,
    "perf_event_release_kernel":        0x4ec8ccb5,
    "find_task_by_vpid":                0x02f92131,
    "__rcu_read_lock":                  0x8d522714,
    "__rcu_read_unlock":                0x2469810f,
    "vmalloc":                          0xd6ee688f,
    "vfree":                            0x999e8297,
    "memset":                           0xdcb764ad,
    "memcpy":                           0x4829a47e,
    "strncmp":                          0x5a921311,
    "snprintf":                         0x656e4a6e,
    "kfree":                            0x037a0cba,
    "__kmalloc":                        0xeb233a45,
    "msleep":                           0xf9a482f9,
    "__stack_chk_fail":                 0x98a9d10c,
    "kstrdup":                          0x2d39b0a7,
    "strsep":                           0x85df9b6c,
    "jiffies":                          0x15ba50a6,
    "complete":                         0xa6257a2f,
    "wait_for_completion":              0x25974000,
    # task helpers (Xiaomi signature takes a real task_struct *)
    "__put_task_struct":                0x9a80f9a6,
    "get_pid_task":                     0xd15a2984,
    "find_vpid":                        0x12aec6f2,
    "pid_task":                         0x918779bc,
}


class Elf:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.d = bytearray(f.read())
        if self.d[:4] != b"\x7fELF":
            raise ValueError("not ELF")
        self.shoff = struct.unpack_from("<Q", self.d, 0x28)[0]
        self.shent = struct.unpack_from("<H", self.d, 0x3a)[0]
        self.shnum = struct.unpack_from("<H", self.d, 0x3c)[0]
        self.shstrndx = struct.unpack_from("<H", self.d, 0x3e)[0]
        st = self.sh(self.shstrndx)
        self.shstr_off = st[4]

    def sh(self, i):
        return struct.unpack_from("<IIQQQQIIQQ", self.d, self.shoff + i * self.shent)

    def set_sh(self, i, vals):
        struct.pack_into("<IIQQQQIIQQ", self.d, self.shoff + i * self.shent, *vals)

    def sec_name(self, off):
        e = self.d.index(b"\x00", self.shstr_off + off)
        return self.d[self.shstr_off + off:e].decode("utf-8", "replace")

    def find(self, name):
        for i in range(self.shnum):
            s = self.sh(i)
            if self.sec_name(s[0]) == name:
                return i, s
        return None, None

    def save(self):
        with open(self.path, "wb") as f:
            f.write(self.d)


def patch_crc(e):
    idx, s = e.find("__versions")
    if idx is None:
        print("    no __versions section")
        return 0
    base, size = s[4], s[5]
    cnt = size // 64
    print("    __versions: %d entries" % cnt)
    n = 0
    for k in range(cnt):
        eo = base + k * 64
        crc = struct.unpack_from("<Q", e.d, eo)[0]
        ne = e.d.index(b"\x00", eo + 8)
        name = e.d[eo + 8:ne].decode("utf-8", "replace")
        if name in DEVICE_CRC:
            new = DEVICE_CRC[name]
            if crc != new:
                struct.pack_into("<Q", e.d, eo, new)
                n += 1
                print("      PATCH %-38s 0x%016x -> 0x%08x" % (name, crc, new))
            else:
                print("      OK    %-38s 0x%08x" % (name, crc))
        else:
            print("      KEEP  %-38s 0x%016x" % (name, crc))
    return n


def patch_vermagic(e):
    idx, s = e.find(".modinfo")
    if idx is None:
        print("    no .modinfo section")
        return False
    base, size = s[4], s[5]
    blob = bytes(e.d[base:base + size])
    entries = [x for x in blob.split(b"\x00") if x]
    new_entries = []
    found = False
    for ent in entries:
        if ent.startswith(b"vermagic="):
            old = ent[len(b"vermagic="):].decode("utf-8", "replace")
            new_entries.append(b"vermagic=" + DEVICE_VERMAGIC.encode())
            found = True
            if old != DEVICE_VERMAGIC:
                print("      PATCH vermagic")
                print("        old: %s" % old)
                print("        new: %s" % DEVICE_VERMAGIC)
        else:
            new_entries.append(ent)
    if not found:
        print("      no vermagic entry found, appending")
        new_entries.append(b"vermagic=" + DEVICE_VERMAGIC.encode())

    newblob = b"\x00".join(new_entries) + b"\x00"
    if newblob == blob:
        print("    .modinfo vermagic already correct")
        return False

    # 追加到文件末尾（对齐 1），更新 section header，避免移动其它段
    while len(e.d) % 1:
        e.d += b"\x00"
    newoff = len(e.d)
    e.d += newblob
    vals = list(s)
    vals[4] = newoff
    vals[5] = len(newblob)
    e.set_sh(idx, vals)
    print("    .modinfo relocated: offset=0x%x size=%d" % (newoff, len(newblob)))
    return True


def patch(path):
    print("  [%s]" % os.path.basename(path))
    e = Elf(path)
    n1 = patch_crc(e)
    n2 = patch_vermagic(e)
    e.save()
    print("    => crc patched %d, vermagic changed %s" % (n1, n2))
    return n1 + (1 if n2 else 0)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: patch_versions.py <file.ko> [...]")
        sys.exit(1)
    total = 0
    for p in sys.argv[1:]:
        total += patch(p)
    print("TOTAL patches applied: %d" % total)
