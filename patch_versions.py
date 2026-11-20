#!/usr/bin/env python3
"""patch_versions.py - 用设备实测 CRC 覆盖 .ko 的 __versions 段

原因：源码是 5.10.81，设备运行 5.10.236。
genksyms 根据头文件计算 CRC，两个版本间可能不同。
我们已从设备内核 __kcrctab 直接读出真实 CRC，这里覆盖。
"""
import struct
import sys
import os

# 从设备实测（Xiaomi Pad 6 Pro / 5.10.236-android12-9）
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
}


def patch(path):
    with open(path, "rb") as f:
        d = bytearray(f.read())
    if d[:4] != b"\x7fELF":
        print("  [%s] not ELF" % path)
        return 0

    shoff = struct.unpack_from("<Q", d, 0x28)[0]
    shent = struct.unpack_from("<H", d, 0x3a)[0]
    shnum = struct.unpack_from("<H", d, 0x3c)[0]
    shstr = struct.unpack_from("<H", d, 0x3e)[0]

    def sh(i):
        return struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shent)

    st = sh(shstr)
    shstr_off = st[4]

    def nm(o):
        e = d.index(b"\x00", shstr_off + o)
        return d[shstr_off + o:e].decode("utf-8", "replace")

    npatched = 0
    for i in range(shnum):
        s = sh(i)
        if nm(s[0]) != "__versions":
            continue
        base, size = s[4], s[5]
        entsz = 64  # {unsigned long crc; char name[56];}
        cnt = size // entsz
        print("  [%s] __versions: %d entries @0x%x" % (os.path.basename(path), cnt, base))
        for k in range(cnt):
            eo = base + k * entsz
            crc = struct.unpack_from("<Q", d, eo)[0]
            ne = d.index(b"\x00", eo + 8)
            name = d[eo + 8:ne].decode("utf-8", "replace")
            if name in DEVICE_CRC:
                new = DEVICE_CRC[name]
                if crc != new:
                    struct.pack_into("<Q", d, eo, new)
                    npatched += 1
                    print("    PATCH %-38s 0x%016x -> 0x%08x" % (name, crc, new))
                else:
                    print("    OK    %-38s 0x%08x" % (name, crc))
            else:
                print("    KEEP  %-38s 0x%016x (no device value)" % (name, crc))
    if npatched:
        with open(path, "wb") as f:
            f.write(d)
    print("  [%s] patched %d entries" % (os.path.basename(path), npatched))
    return npatched


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: patch_versions.py <file.ko> [...]")
        sys.exit(1)
    total = 0
    for p in sys.argv[1:]:
        total += patch(p)
    print("TOTAL patched: %d" % total)
