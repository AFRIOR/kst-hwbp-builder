#!/usr/bin/env python3
"""stub_missing_kconfig.py - 为缺失的 Kconfig 引用创建空 stub

Xiaomi OSS 内核发布不完整：drivers/misc/Kconfig 里 source 了
drivers/misc/hwid/Kconfig，但该目录没有发布（厂商专有驱动被剥离）。

本脚本扫描整棵树的 Kconfig，把任何缺失的 source "path" 创建为空 stub，
让 make olddefconfig 能跑下去。
"""
import os
import re
import sys

SOURCES = re.compile(r'^\s*(?:source|rsource|osource)\s+"([^"]+)"', re.M)
KFILES = ("Kconfig",)


def is_kconfig(fn):
    return fn == "Kconfig" or fn.startswith("Kconfig.")


def main(root):
    created = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过 .git 和 out
        dirnames[:] = [d for d in dirnames if d not in (".git", "out", ".repo")]
        for fn in filenames:
            if not is_kconfig(fn):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            scanned += 1
            for m in SOURCES.finditer(txt):
                rel = m.group(1)
                tgt = os.path.normpath(os.path.join(dirpath, rel))
                if os.path.exists(tgt):
                    continue
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
                with open(tgt, "w", encoding="utf-8") as f:
                    f.write("# stub: target missing in Xiaomi OSS release\n")
                created.append(os.path.relpath(tgt, root))

    print("scanned %d Kconfig files" % scanned)
    print("created %d stubs" % len(created))
    for c in created[:100]:
        print("  + %s" % c)
    return len(created)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: stub_missing_kconfig.py <kernel_root>")
        sys.exit(1)
    main(sys.argv[1])
