// SPDX-License-Identifier: GPL-2.0
/*
 * raw_hw_rng - direct, high-speed access to CPU true random number generators
 *
 * Auto-detects and exposes exactly one backend:
 *   Zhaoxin / VIA PadLock (xstore)  ->  /dev/raw_hwrng
 *   Intel / AMD RDSEED             ->  /dev/raw_rdseed
 *
 * Why not /dev/hwrng?
 *   That name belongs to the kernel's own hw_random framework and is very
 *   often already taken by a TPM or virtio-rng device. Registering it here
 *   would either fail or shadow a device that userspace expects to behave
 *   differently - and consumers could not tell which source they got. The
 *   unique names above make the backend unambiguous.
 */

#include <linux/module.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/kernel.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/processor.h>
#include <linux/delay.h>         /* usleep_range */
#include <linux/sched.h>         /* cond_resched */
#include <linux/sched/signal.h>  /* signal_pending */
#include <asm/processor.h>       /* cpuid(), cpuid_count() */

MODULE_LICENSE("GPL");
MODULE_AUTHOR("J.C.A. Greve");
MODULE_DESCRIPTION("Unified High-Speed Hardware TRNG Device (Zhaoxin PadLock & Intel/AMD RDSEED)");
MODULE_VERSION("2.0");

#define RNG_BACKEND_NONE     0
#define RNG_BACKEND_RDSEED   1
#define RNG_BACKEND_XSTORE   2

/*
 * Bytes assembled in kernel space before a single copy_to_user().
 * The previous version issued one copy_to_user() per 8 bytes, which meant
 * ~55000 crossings for a single 442 KB latent. 512 bytes cuts that to ~865
 * while staying well inside the 16 KB kernel stack.
 */
#define RNG_CHUNK_BYTES      512

/* Inner spin attempts before yielding the CPU */
#define RNG_SPIN_RETRIES     100

static int active_rng_type = RNG_BACKEND_NONE;

/* Read-only introspection: /sys/module/raw_hw_rng/parameters/active_backend */
static char *active_backend = "none";
module_param(active_backend, charp, 0444);
MODULE_PARM_DESC(active_backend, "Active TRNG backend (read-only)");

/*
 * Permissions of the created device node.
 * 0444 keeps the module drop-in usable for unprivileged services such as
 * ComfyUI. Note that this lets ANY local user drain the CPU entropy source,
 * which is a (mild) denial-of-service surface. On multi-user machines load
 * with dev_mode=0440 and grant access via a group + udev rule instead.
 */
static int dev_mode = 0444;
module_param(dev_mode, int, 0444);
MODULE_PARM_DESC(dev_mode, "Permission bits for the device node (default 0444)");

/* =========================================================================
 * Zhaoxin / VIA PadLock (xstore)
 * ========================================================================= */

/*
 * Pull up to sizeof(buf) bytes out of the PadLock engine.
 * Returns the number of bytes written, 0 if the engine stayed empty.
 */
static unsigned int xstore_fill(u8 *buf, unsigned int buf_len)
{
	unsigned int generated = 0;
	int retries = RNG_SPIN_RETRIES;

	do {
		asm volatile(
			".byte 0x0F, 0xA7, 0xC0\n\t"
			: "=a" (generated), "=m" (*buf)
			: "D" (buf), "d" (0)
			: "memory"
		);

		/* EAX[4:0] holds the byte count; mask off the status bits. */
		generated &= 0x1F;

		/*
		 * Hard clamp. Real PadLock silicon returns at most 8 here, but
		 * the mask permits 31 - without this bound a misbehaving or
		 * emulated CPU would make the caller copy past the end of the
		 * 16-byte buffer and leak kernel stack to userspace.
		 */
		if (generated > buf_len)
			generated = buf_len;

		if (generated > 0)
			return generated;

		cpu_relax();
	} while (--retries);

	return 0;
}

static ssize_t zhaoxin_rng_read(struct file *file, char __user *buf,
				size_t count, loff_t *ppos)
{
	size_t bytes_read = 0;
	u8 chunk[RNG_CHUNK_BYTES];
	u8 rand_buf[16] __aligned(16);
	ssize_t ret = 0;

	while (bytes_read < count) {
		size_t want = min(count - bytes_read, sizeof(chunk));
		size_t chunk_len = 0;

		if (signal_pending(current)) {
			ret = bytes_read ? (ssize_t)bytes_read : -ERESTARTSYS;
			goto out;
		}

		/*
		 * Large reads spin here for a long time. Without this the
		 * kernel can trip soft-lockup / RCU-stall detection on
		 * non-preemptible builds.
		 */
		cond_resched();

		while (chunk_len < want) {
			unsigned int generated = xstore_fill(rand_buf, sizeof(rand_buf));
			size_t take;

			if (generated == 0)
				break;

			take = min_t(size_t, want - chunk_len, generated);
			memcpy(chunk + chunk_len, rand_buf, take);
			chunk_len += take;
		}

		if (chunk_len == 0) {
			if (file->f_flags & O_NONBLOCK) {
				ret = bytes_read ? (ssize_t)bytes_read : -EAGAIN;
				goto out;
			}
			usleep_range(10, 50);
			continue;
		}

		if (copy_to_user(buf + bytes_read, chunk, chunk_len)) {
			ret = -EFAULT;
			goto out;
		}
		bytes_read += chunk_len;
	}

	ret = bytes_read;
out:
	/* Never leave harvested entropy sitting on the kernel stack. */
	memzero_explicit(chunk, sizeof(chunk));
	memzero_explicit(rand_buf, sizeof(rand_buf));
	return ret;
}

/* =========================================================================
 * Intel / AMD RDSEED
 * ========================================================================= */

/* Returns 1 and fills *out on success, 0 if the DRNG stayed empty. */
static int rdseed_one(unsigned long *out)
{
	int retries = RNG_SPIN_RETRIES;
	unsigned char success = 0;

	do {
		asm volatile("rdseed %0; setc %1"
			     : "=r" (*out), "=qm" (success)
			     :
			     : "cc");
		if (success)
			return 1;
		cpu_relax();
	} while (--retries);

	return 0;
}

static ssize_t rdseed_rng_read(struct file *file, char __user *buf,
			       size_t count, loff_t *ppos)
{
	size_t bytes_read = 0;
	u8 chunk[RNG_CHUNK_BYTES];
	unsigned long rand_val = 0;
	ssize_t ret = 0;

	while (bytes_read < count) {
		size_t want = min(count - bytes_read, sizeof(chunk));
		size_t chunk_len = 0;

		if (signal_pending(current)) {
			ret = bytes_read ? (ssize_t)bytes_read : -ERESTARTSYS;
			goto out;
		}

		cond_resched();

		while (chunk_len < want) {
			size_t take;

			if (!rdseed_one(&rand_val))
				break;

			/* min() also covers a trailing partial word. */
			take = min(want - chunk_len, sizeof(rand_val));
			memcpy(chunk + chunk_len, &rand_val, take);
			chunk_len += take;
		}

		if (chunk_len == 0) {
			if (file->f_flags & O_NONBLOCK) {
				ret = bytes_read ? (ssize_t)bytes_read : -EAGAIN;
				goto out;
			}
			usleep_range(10, 50);
			continue;
		}

		if (copy_to_user(buf + bytes_read, chunk, chunk_len)) {
			ret = -EFAULT;
			goto out;
		}
		bytes_read += chunk_len;
	}

	ret = bytes_read;
out:
	memzero_explicit(chunk, sizeof(chunk));
	memzero_explicit(&rand_val, sizeof(rand_val));
	return ret;
}

/* ========================================================================= */

static const struct file_operations zhaoxin_fops = {
	.owner  = THIS_MODULE,
	.read   = zhaoxin_rng_read,
	.llseek = noop_llseek,
};

static const struct file_operations rdseed_fops = {
	.owner  = THIS_MODULE,
	.read   = rdseed_rng_read,
	.llseek = noop_llseek,
};

static struct miscdevice rng_misc = {
	.minor = MISC_DYNAMIC_MINOR,
	/* .name, .fops and .mode are assigned in init() */
};

/*
 * CPUID leaf 0xC0000000 is Centaur-specific. On other vendors it is an
 * invalid leaf whose return value is undefined - Intel echoes the highest
 * standard leaf, which happens to be small enough to fail the comparison
 * below, but relying on that is fragile. Gate on the vendor string first.
 */
static bool cpu_is_centaur_family(void)
{
	unsigned int eax, ebx, ecx, edx;
	char vendor[13];

	cpuid(0, &eax, &ebx, &ecx, &edx);
	memcpy(vendor + 0, &ebx, 4);
	memcpy(vendor + 4, &edx, 4);
	memcpy(vendor + 8, &ecx, 4);
	vendor[12] = '\0';

	return !strcmp(vendor, "CentaurHauls") || !strcmp(vendor, "  Shanghai  ");
}

static int __init raw_hw_rng_init(void)
{
	unsigned int eax, ebx, ecx, edx;
	int ret;

	/* 1. Zhaoxin / VIA PadLock RNG - the faster engine, so it wins. */
	if (cpu_is_centaur_family()) {
		cpuid(0xC0000000, &eax, &ebx, &ecx, &edx);
		if (eax >= 0xC0000001) {
			cpuid(0xC0000001, &eax, &ebx, &ecx, &edx);
			/* EDX bit 2 = RNG present, bit 3 = RNG enabled */
			if ((edx & (1 << 2)) && (edx & (1 << 3))) {
				active_rng_type = RNG_BACKEND_XSTORE;
				active_backend  = "zhaoxin-padlock";
				rng_misc.name   = "raw_hwrng";
				rng_misc.fops   = &zhaoxin_fops;
				pr_info("raw_hw_rng: Zhaoxin/VIA PadLock TRNG active -> /dev/raw_hwrng\n");
			} else {
				pr_info("raw_hw_rng: Centaur CPU found but PadLock RNG is absent or disabled in BIOS\n");
			}
		}
	}

	/* 2. Fallback: Intel / AMD RDSEED */
	if (active_rng_type == RNG_BACKEND_NONE) {
		cpuid_count(7, 0, &eax, &ebx, &ecx, &edx);
		if (ebx & (1 << 18)) {  /* EBX bit 18 = RDSEED */
			active_rng_type = RNG_BACKEND_RDSEED;
			active_backend  = "intel-amd-rdseed";
			rng_misc.name   = "raw_rdseed";
			rng_misc.fops   = &rdseed_fops;
			pr_info("raw_hw_rng: Intel/AMD RDSEED TRNG active -> /dev/raw_rdseed\n");
		}
	}

	if (active_rng_type == RNG_BACKEND_NONE) {
		pr_err("raw_hw_rng: no supported CPU TRNG found (needs Zhaoxin PadLock or RDSEED)\n");
		return -ENODEV;
	}

	rng_misc.mode = dev_mode & 0777;

	ret = misc_register(&rng_misc);
	if (ret) {
		pr_err("raw_hw_rng: failed to register /dev/%s (%d). "
		       "Is another module already using that name?\n",
		       rng_misc.name, ret);
		active_rng_type = RNG_BACKEND_NONE;
		active_backend  = "none";
	}
	return ret;
}

static void __exit raw_hw_rng_exit(void)
{
	if (active_rng_type != RNG_BACKEND_NONE)
		misc_deregister(&rng_misc);
	pr_info("raw_hw_rng: module unloaded.\n");
}

module_init(raw_hw_rng_init);
module_exit(raw_hw_rng_exit);
