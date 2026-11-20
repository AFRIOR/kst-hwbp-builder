// ksthwbp.c - KST hardware breakpoint tracer
//
// Device: Xiaomi Pad 6 Pro (liuqin), 5.10.236-android12-9
//
// IMPORTANT vendor deviation (discovered from build error + source):
//   perf_event_create_kernel_counter(attr, cpu, struct task_struct *task, cb, ctx)
//                                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//   The stock kernel takes `int pid` here; Xiaomi/Qualcomm changed it to a
//   task_struct pointer. Passing a tid would dereference a small integer and
//   panic the kernel. That is why earlier KPM attempts crashed.
//
// Not exported on this kernel (cannot be referenced):
//   register_user_hw_breakpoint, unregister_hw_breakpoint, find_get_task_by_vpid
// Exported and usable:
//   find_task_by_vpid, __put_task_struct, perf_event_create_kernel_counter,
//   perf_event_enable, perf_event_disable, perf_event_release_kernel, printk
//
// Usage:
//   insmod ksthwbp.ko "so_base=0x7xx tids=100,101 offsets=2ad290,2b1288,..."

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/perf_event.h>
#include <linux/hw_breakpoint.h>
#include <linux/moduleparam.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/err.h>
#include <linux/sched.h>
#include <linux/sched/task.h>
#include <linux/pid.h>
#include <linux/rcupdate.h>
#include <asm/ptrace.h>

#define MAX_EV   4096
#define MAX_NUM  256

static struct perf_event *g_ev[MAX_EV];
static unsigned long g_off_key[MAX_EV];
static int g_n;

static char *tids = "";
module_param(tids, charp, 0644);
MODULE_PARM_DESC(tids, "comma separated thread ids");

static char *offsets = "";
module_param(offsets, charp, 0644);
MODULE_PARM_DESC(offsets, "comma separated hex offsets");

static unsigned long so_base;
module_param(so_base, ulong, 0644);
MODULE_PARM_DESC(so_base, "runtime base of target .so");

static char *soname = "libtiger_tally.so";
module_param(soname, charp, 0644);
MODULE_PARM_DESC(soname, "target .so name (log only)");

/* ---------- hand-rolled parsers (kstrtou64 is not exported) ---------- */
static unsigned long hex_parse(const char *s, const char **endp)
{
	unsigned long v = 0;
	int n = 0;

	while (*s == ' ' || *s == '\t')
		s++;
	if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X'))
		s += 2;
	for (;;) {
		char c = *s;
		unsigned long d;

		if (c >= '0' && c <= '9')
			d = c - '0';
		else if (c >= 'a' && c <= 'f')
			d = c - 'a' + 10;
		else if (c >= 'A' && c <= 'F')
			d = c - 'A' + 10;
		else
			break;
		v = (v << 4) | d;
		s++;
		if (++n > 16)
			break;
	}
	if (endp)
		*endp = s;
	return v;
}

static int dec_parse(const char *s, const char **endp)
{
	int v = 0, n = 0;

	while (*s == ' ' || *s == '\t')
		s++;
	while (*s >= '0' && *s <= '9') {
		v = v * 10 + (*s - '0');
		s++;
		if (++n > 9)
			break;
	}
	if (endp)
		*endp = s;
	return v;
}

/* ---------- breakpoint callback ---------- */
static void kst_cb(struct perf_event *bp, struct perf_sample_data *data,
		   struct pt_regs *regs)
{
	int i;
	unsigned long key = 0;

	for (i = 0; i < g_n; i++) {
		if (g_ev[i] == bp) {
			key = g_off_key[i];
			break;
		}
	}
	pr_info("KSTHWBP HIT off=%lx pc=%llx lr=%llx x0=%llx x1=%llx x2=%llx x3=%llx x4=%llx x5=%llx\n",
		key,
		(u64)regs->pc, (u64)regs->regs[30],
		(u64)regs->regs[0], (u64)regs->regs[1],
		(u64)regs->regs[2], (u64)regs->regs[3],
		(u64)regs->regs[4], (u64)regs->regs[5]);
}

/* ---------- install one breakpoint ---------- */
static int hook_one(int tid, unsigned long off)
{
	struct perf_event_attr attr = { };
	struct perf_event *ev;
	struct task_struct *task;
	perf_overflow_handler_t cb = kst_cb;

	if (g_n >= MAX_EV)
		return -ENOSPC;

	hw_breakpoint_init(&attr);
	attr.bp_addr = so_base + off;
	attr.bp_len  = HW_BREAKPOINT_LEN_8;
	attr.bp_type = HW_BREAKPOINT_X;

	/* Xiaomi signature needs a real task_struct pointer */
	rcu_read_lock();
	task = find_task_by_vpid(tid);
	if (task)
		get_task_struct(task);
	rcu_read_unlock();

	if (!task)
		return -ESRCH;

	ev = perf_event_create_kernel_counter(&attr, -1, task, cb, NULL);
	put_task_struct(task);

	if (!ev)
		return -ENOMEM;
	if (IS_ERR(ev))
		return (int)PTR_ERR(ev);

	perf_event_enable(ev);
	g_off_key[g_n] = off;
	g_ev[g_n++] = ev;
	return 0;
}

/* ---------- init ---------- */
static int __init kst_init(void)
{
	static unsigned long offs[MAX_NUM];
	static int tlist[MAX_NUM];
	int noff = 0, ntid = 0, i, j, ok = 0, fail = 0;
	const char *p;

	pr_info("KSTHWBP INIT soname=%s base=0x%lx\n", soname ? soname : "?", so_base);
	pr_info("KSTHWBP INIT tids=%s\n", tids ? tids : "?");
	pr_info("KSTHWBP INIT offsets=%s\n", offsets ? offsets : "?");

	if (!so_base) {
		pr_info("KSTHWBP INIT missing so_base\n");
		return -EINVAL;
	}

	if (tids && *tids) {
		p = tids;
		while (*p && ntid < MAX_NUM) {
			while (*p == ',' || *p == ' ')
				p++;
			if (!*p)
				break;
			tlist[ntid++] = dec_parse(p, &p);
		}
	}

	if (offsets && *offsets) {
		p = offsets;
		while (*p && noff < MAX_NUM) {
			while (*p == ',' || *p == ' ')
				p++;
			if (!*p)
				break;
			offs[noff++] = hex_parse(p, &p);
		}
	}

	pr_info("KSTHWBP INIT parsed tids=%d offsets=%d\n", ntid, noff);
	if (!ntid || !noff) {
		pr_info("KSTHWBP INIT nothing to hook\n");
		return -EINVAL;
	}

	for (i = 0; i < ntid; i++) {
		for (j = 0; j < noff; j++) {
			int r = hook_one(tlist[i], offs[j]);

			if (r == 0) {
				ok++;
			} else {
				fail++;
				if (fail <= 5)
					pr_info("KSTHWBP INIT hook tid=%d off=%lx failed %d\n",
						tlist[i], offs[j], r);
			}
		}
	}
	pr_info("KSTHWBP INIT done ok=%d fail=%d nodes=%d\n", ok, fail, g_n);
	return 0;
}

static void __exit kst_exit(void)
{
	int i;

	for (i = 0; i < g_n; i++) {
		if (g_ev[i]) {
			perf_event_disable(g_ev[i]);
			perf_event_release_kernel(g_ev[i]);
			g_ev[i] = NULL;
		}
	}
	pr_info("KSTHWBP EXIT removed=%d\n", g_n);
	g_n = 0;
}

module_init(kst_init);
module_exit(kst_exit);

MODULE_LICENSE("GPL v2");
MODULE_AUTHOR("kst");
MODULE_DESCRIPTION("KST hardware breakpoint tracer");
