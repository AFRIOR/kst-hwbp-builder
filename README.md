# KST HWBP Module Builder

用 GitHub Actions 编译 KST 硬件断点追踪内核模块（Xiaomi Pad 6 Pro / liuqin）。

## 为什么用 Actions

- 内核源码 1.5 GB，本地网络太慢
- 需要精确匹配设备的 **clang 12.0.5** + **内核配置**，才能生成正确的符号 CRC / CFI 元数据
- Actions 在 GitHub 数据中心，克隆秒级完成

## 使用方法

### 1. 创建仓库并推送

```bash
cd gh-repo
git init
git add -A
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<你的用户名>/kst-hwbp-builder.git
git push -u origin main
```

> 注意 `.github/workflows/` 是隐藏目录，必须用 git 推送（网页上传会漏掉）。

### 2. 运行 workflow

仓库页面 → **Actions** → 左侧 **Build KST HWBP Module** → **Run workflow** → **Run workflow**

预计 20-40 分钟（克隆内核 + modules_prepare 占大部分）。

### 3. 下载产物

运行完成后，进入该次运行页面底部 **Artifacts** → 下载 `ksthwbp-module`（几十 KB）。
解压得到 `ksthwbp.ko`。

## 设备上的用法

```bash
# 1. 找到目标进程和 libtiger_tally.so 基址
PKG=io.dcloud.H55BDF6BE
PID=$(pidof $PKG | awk '{print $1}')
BASE=$(grep -m1 libtiger_tally.so /proc/$PID/maps | cut -d'-' -f1)
echo "pid=$PID base=0x$BASE"

# 2. 收集该进程所有线程 tid
TIDS=$(ls /proc/$PID/task | tr '\n' ',' | sed 's/,$//')
echo "tids=$TIDS"

# 3. 加载模块（一次加载，同时给所有线程装 6 个断点）
insmod /data/local/tmp/ksthwbp.ko \
  "so_base=0x$BASE" \
  "tids=$TIDS" \
  "soname=libtiger_tally.so" \
  "offsets=2ad290,2b1288,2b13d8,2b1330,2af278,2aa618"

# 4. 看命中
dmesg | grep KSTHWBP

# 5. 卸载
rmmod ksthwbp
```

## 参数说明

| 参数 | 说明 |
|---|---|
| `so_base` | `libtiger_tally.so` 运行时基址（十六进制，带 `0x`）|
| `tids` | 逗号分隔的线程 ID 列表（`ls /proc/PID/task`）|
| `soname` | 目标 so 名字（仅日志）|
| `offsets` | 断点偏移，逗号分隔十六进制（不带 `0x`）|

默认 6 个偏移 = nt1/nt9/nt10/nt11/nt12/nt13：

```
2ad290 = nt1
2b1288 = nt9
2b13d8 = nt10
2b1330 = nt11
2af278 = nt12
2aa618 = nt13
```

> **ARM64 硬件断点上限 = 6 个/线程**，所以只能同时监控 6 个函数。

## 输出格式

```
KSTHWBP HIT off=2ad290 pc=7b1f58d290 lr=7b1f58d294 x0=... x1=... x2=... x3=... x4=... x5=...
KSTHWBP INIT done ok=N fail=0 nodes=N
```

- `off` = 命中的 nt 偏移（直接对应 nt1/nt9/nt10...）
- `x0`-`x5` = 该函数的入参 / 局部变量

## 设计说明

### 只用已导出符号

从设备实测，以下符号在本内核**未导出**（`ksymtab=0`），模块不能引用：

```
register_user_hw_breakpoint    unregister_hw_breakpoint
get_task_struct                put_task_struct
kstrtou64                      _printk
```

所以模块只用：

```
perf_event_create_kernel_counter   0xbcfefbf3   ✓ 导出
perf_event_enable                  0x085a0bc8   ✓
perf_event_disable                 0x7d609757   ✓
perf_event_release_kernel          0x4ec8ccb5   ✓
kstrdup                            0x2d39b0a7   ✓
strsep                             0x85df9b6c   ✓
kfree                              0x037a0cba   ✓
printk                             0xc5850110   ✓
```

`perf_event_create_kernel_counter(attr, cpu=-1, pid=tid, cb, ctx)` 内部会自己
`find_get_task_by_vpid`，因此不需要 `get_task_struct`。

### CRC 覆盖

源码是 **5.10.81**，设备运行 **5.10.236**（差 155 个 stable 更新）。
`genksyms` 根据头文件算出的 CRC 可能与设备不同 → 模块加载报
`disagrees about version of symbol X`。

因此 workflow 在编译后用 `patch_versions.py` 把 `__versions` 段的 CRC
**覆盖成设备实测值**（从设备内核 `__kcrctab` 直接读出）。

### 内核配置

`device_defconfig` 是从设备 `/proc/config.gz` 直接提取的 **精确配置**（6,726 行）。

workflow 会把它放为 `out/.config` 然后 `make olddefconfig` 适配源码版本。

## 注意

- 模块**无注入**，不修改用户态内存
- 会给传入的所有 tid 每个装 6 个断点（N 个线程 × 6 = 6N 个 event）
- 若 `INIT done ok=0 fail=N`，看 `hook tid=... failed <errno>`：
  - `-28` (ENOSPC) = 硬件断点用尽
  - `-22` (EINVAL) = 地址无效（`so_base` 或 `offsets` 错）
- 需要 SELinux Permissive 或相应策略（`setenforce 0`）
