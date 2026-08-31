# ComfyUI Hardware RNG

Replace PyTorch's pseudo-random noise with **true physical entropy** from your
CPU — or with a **reproducible entropy pool file** that lets you replay exact
seeds later.

Works on Linux (kernel module) and Windows (helper DLL). The pool-file mode
works everywhere, including on machines with no supported hardware RNG.

---

## Table of contents

- [What this actually does](#what-this-actually-does)
- [Will this improve my images?](#will-this-improve-my-images)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Step 1 — Install the ComfyUI node](#step-1--install-the-comfyui-node-all-platforms)
  - [Step 2a — Linux: build the kernel module](#step-2a--linux-build-the-kernel-module)
  - [Step 2b — Windows: build the helper DLL](#step-2b--windows-build-the-helper-dll)
  - [Step 3 — Get an entropy pool file](#step-3--get-an-entropy-pool-file)
- [Using the node](#using-the-node)
- [Reproducible workflow: replaying good seeds](#reproducible-workflow-replaying-good-seeds)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [License](#license)

---

## What this actually does

Every image a diffusion model generates starts as a field of random noise. That
noise normally comes from PyTorch's pseudo-random number generator (PRNG): a
deterministic algorithm that *looks* random but is fully determined by a seed
value.

This node replaces that noise with one of two alternatives:

| Mode | Source | Reproducible? |
|---|---|---|
| **Hardware** | Physical entropy from the CPU (thermal / quantum noise) | No — new noise every run, by design |
| **File** | A pre-generated pool file, read at a seed-derived offset | Yes — same seed, same image |

Both feed the same conversion (Box–Muller) that turns uniformly distributed
integers into the normal distribution the sampler expects.

## Will this improve my images?

**Honest answer: nobody has verified that it does.**

The theory behind using true entropy is that a PRNG can produce subtly
correlated noise fields across nearby seeds, which could show up as "clustering"
— runs of similar or similarly-flawed images. Whether that measurably affects
diffusion output has, to this project's knowledge, never been demonstrated with
controlled experiments. Modern PRNGs are extremely good.

So treat the hardware modes as **an experiment worth running**, not as a
guaranteed quality upgrade. If you compare and find a difference, that result
would be genuinely interesting — please open an issue.

The **File mode**, on the other hand, has a concrete and immediate practical
benefit that does not depend on any of the above: it gives you an entropy source
that is byte-for-byte reproducible across machines, so a seed you share with
someone else produces the identical image on their computer.

## Requirements

**All platforms**
- A working ComfyUI installation
- Python packages `numpy` and `torch` (both already ship with ComfyUI)

**Linux hardware mode**
- An x86 CPU with either RDSEED (most Intel/AMD CPUs from ~2015 onward) or a
  Zhaoxin/VIA CPU with the PadLock RNG engine
- Kernel headers for your running kernel, plus `make` and `gcc`
- Root access, once, to build and load the module

**Windows hardware mode**
- An x86 CPU with RDSEED
- A C compiler (Visual Studio Build Tools or MinGW-w64) to build the DLL

**File mode**
- Nothing special. Works on any machine, including AMD, ARM, or virtualised
  systems with no hardware RNG at all.

---

## Installation

### Step 1 — Install the ComfyUI node (all platforms)

Copy the `ComfyUI-HardwareRNG` folder into your ComfyUI custom nodes directory:

```
ComfyUI/custom_nodes/ComfyUI-HardwareRNG/
    __init__.py
    xeon_rng_node.py
```

Then **restart ComfyUI completely** — restart the server process, not just the
browser tab. ComfyUI only reads custom nodes at startup.

On startup you should see a line in the console like:

```
[HardwareRNG] loaded: XeonRDSEEDNoise v5 (...)
```

If you see that line, the node is installed. It appears in the node menu under
**Hardware/RNG → Hardware RNG Noise (RDSEED/HWRNG/File)**.

At this point **File mode already works**. The hardware modes need step 2.

---

### Step 2a — Linux: build the kernel module

> A kernel module is a small driver that gives normal programs access to a CPU
> feature the operating system does not otherwise expose. You build it once; it
> then needs to be loaded after each reboot unless you make it permanent
> (see below).

**1. Install the build prerequisites.**

Debian / Ubuntu:
```bash
sudo apt update
sudo apt install build-essential linux-headers-$(uname -r)
```

Fedora / RHEL:
```bash
sudo dnf install kernel-devel kernel-headers gcc make
```

Arch:
```bash
sudo pacman -S base-devel linux-headers
```

**2. Build it.**

```bash
cd Device
make
```

This produces `raw_hw_rng.ko`. If the build fails complaining about missing
headers, your installed headers do not match your running kernel — reboot after
installing them, or install the version matching `uname -r`.

**3. Load it.**

```bash
sudo insmod raw_hw_rng.ko
```

**4. Check that it worked.**

```bash
dmesg | tail -5
ls -l /dev/raw_*
```

You should see one of:

```
raw_hw_rng: Intel/AMD RDSEED TRNG active -> /dev/raw_rdseed
raw_hw_rng: Zhaoxin/VIA PadLock TRNG active -> /dev/raw_hwrng
```

If instead you see `no supported CPU TRNG found`, your CPU does not have a
usable hardware RNG — use File mode.

**5. Make it load automatically at boot (optional).**

```bash
sudo make install
sudo depmod -a
echo raw_hw_rng | sudo tee /etc/modules-load.d/raw_hw_rng.conf
```

> **Note on secure boot:** if your system has Secure Boot enabled, unsigned
> modules are rejected. Either sign the module yourself or disable Secure Boot
> in your firmware. If `insmod` reports
> `Required key not available`, that is what happened.

**Permissions.** By default the device is world-readable (mode `0444`) so
ComfyUI can read it without extra setup. On a machine with untrusted local
users, load the module with tighter permissions instead and grant access via a
group:

```bash
sudo insmod raw_hw_rng.ko dev_mode=0440
sudo groupadd -f hwrng
sudo usermod -aG hwrng $USER
echo 'KERNEL=="raw_rdseed|raw_hwrng", GROUP="hwrng", MODE="0440"' \
    | sudo tee /etc/udev/rules.d/99-raw-hw-rng.rules
sudo udevadm control --reload && sudo udevadm trigger
```

You have to log out and back in for the new group membership to apply.

---

### Step 2b — Windows: build the helper DLL

Python cannot execute CPU instructions directly, so a small DLL does it.

**With Visual Studio Build Tools** (open the "Developer Command Prompt"):
```cmd
cd Windows
cl /O2 /LD rdseed_win.c /Fe:rdseed_win.dll
```

**With MinGW-w64:**
```cmd
cd Windows
gcc -O2 -shared -o rdseed_win.dll rdseed_win.c
```

Then copy `rdseed_win.dll` next to the node file:

```
ComfyUI/custom_nodes/ComfyUI-HardwareRNG/rdseed_win.dll
```

Restart ComfyUI. If the DLL is missing or fails to load, the console prints a
warning and the hardware modes are disabled — File mode still works.

> On Windows only the RDSEED backend exists. The Zhaoxin/PadLock backend is
> Linux-only.

---

### Step 3 — Get an entropy pool file

Needed only for **File (Deterministic)** mode.

**Option A — generate your own (recommended).**

```bash
# From your hardware RNG (Linux, after step 2a)
python3 tools/generate_pool.py --size-gb 1 --source /dev/raw_rdseed --out pool.dat

# From the operating system CSPRNG (any platform, fast)
python3 tools/generate_pool.py --size-gb 1 --source urandom --out pool.dat
```

**Option B — download the prepared Zhaoxin pool** from this project's
[Releases](../../releases) page. It contains entropy harvested from a Zhaoxin
PadLock engine, so users without that hardware can try that source and compare
it against their own.

**How large should the pool be?** One 768×576 latent consumes about 432 KB. A
rule of thumb is at least 100× that, so even 256 MB is comfortable and 1 GB is
generous. A larger pool mainly reduces how often different seeds read
overlapping regions — which is harmless in practice, but a bigger pool costs
nothing but disk space.

**Where to put it.** Either enter an absolute path in the node's `file_path`
field, or set an environment variable before starting ComfyUI:

```bash
export COMFYUI_ENTROPY_FILE=/data/entropy/pool.dat
```

The environment variable acts as a safety net: if the widget value is ever lost,
the node still finds the right file.

> **Important:** the pool must contain data that is uniformly distributed across
> the full 32-bit range. Both generation methods above satisfy that. Do not feed
> it text, images, or compressed archives — the Box–Muller conversion relies on
> uniformity and will produce badly scaled noise otherwise.

---

## Using the node

Add **Hardware RNG Noise** and connect its `NOISE` output to a
`SamplerCustomAdvanced` node's `noise` input, replacing `RandomNoise`.

### Inputs

| Input | Meaning |
|---|---|
| `noise_seed` | Seed for File mode. Ignored by the hardware modes. |
| `source` | Which entropy source to use — see below. |
| `noise_scale` | Multiplies the standard deviation of the noise. Leave at `1.00` unless you know you need otherwise. |
| `file_path` | Path to the entropy pool. Absolute paths are safest. |
| `seed_string` *(optional)* | Seed supplied as text. Connect a node such as "Text Load Line from File" here to drive a batch from a seed list. Overrides `noise_seed` when non-empty. |
| `device_path` *(optional)* | Read from a specific character device instead of the auto-detected one. Leave empty normally. |

### Choosing a source

- **`Auto-Detect Hardware`** — uses whichever device the kernel module created.
  Good default on Linux.
- **`HW: RDSEED (Thermal)`** — forces `/dev/raw_rdseed` (Intel/AMD).
- **`HW: HWRNG (Standard/Quantum)`** — forces `/dev/raw_hwrng` (Zhaoxin/VIA).
- **`File (Deterministic)`** — reads the pool file. **The only reproducible
  mode.**

> This node deliberately never touches `/dev/hwrng`. That name belongs to the
> Linux kernel's own `hw_random` framework and is usually already taken by a TPM
> or virtio-rng device, which are far slower and would silently give you a
> different source than you asked for.

---

## Reproducible workflow: replaying good seeds

A practical use case: generate many candidates, keep the good ones, then
regenerate only those.

1. Set `source` to **File (Deterministic)** and pick your pool file.
   *This is essential — in hardware modes the seed does nothing, so there is
   nothing to replay.*
2. Generate a batch, varying `noise_seed`.
3. Review the results and delete the failures. ComfyUI writes the workflow —
   including the seed — into each PNG's metadata, so you can extract the seeds
   of the keepers with a script.
4. Put those seeds into a plain text file, one number per line.
5. Connect a "Text Load Line from File" node to the node's `seed_string` input
   and run the batch again.

The seed parser tolerates stray whitespace, quotes and prefixes such as
`seed: 12345` or `12345 # keeper`, taking the first number on each line.

> **Caveat worth knowing:** a seed only reproduces an image when *everything
> else* matches too — same model, sampler, scheduler, step count, resolution and
> prompt. If your first pass used fewer steps or a speed-up LoRA, the same seed
> will not give the same picture in the final pass.

---

## Troubleshooting

**"No hardware RNG device found"**
The kernel module is not loaded. Run `sudo insmod raw_hw_rng.ko` from the
`Device` folder and check `dmesg | tail`. Modules do not survive a reboot unless
you set them up to load automatically (step 2a.5).

**"No read permission for /dev/raw_rdseed"**
The device permissions are tighter than your user account. See the permissions
section in step 2a.

**"Short read from /dev/raw_..."**
The hardware entropy source could not keep up. Wait a moment and retry, or
switch to File mode. On a busy machine, RDSEED can temporarily run dry.

**"Entropy file not found"**
The error message lists every path that was checked. Use an absolute path in
`file_path`, or set `COMFYUI_ENTROPY_FILE`.

**"RDSEED delivered only N of M values" (Windows)**
Either the CPU pool is genuinely exhausted (retry), or you are using an outdated
`rdseed_win.dll`. The current DLL returns the number of values written; older
builds returned 1/0. Rebuild it.

**A widget keeps resetting to its default value**
Delete the node from the canvas and add it again rather than reusing the old
instance. ComfyUI stores widget values positionally, so a node whose input list
has changed between versions can end up misaligned.

**The image looks identical every run in hardware mode**
That should not happen — the node forces re-execution in hardware modes. If it
does, check that `source` is really not set to File mode.

---

## How it works

1. **Collect raw entropy.** Either read bytes from the kernel device (which
   executes `RDSEED` or PadLock `xstore` and hands the results to userspace), or
   read a block from the pool file starting at an offset derived from the seed.
2. **Convert to a normal distribution.** Diffusion samplers expect noise drawn
   from *N*(0, 1), but raw entropy is uniformly distributed. The Box–Muller
   transform converts pairs of uniform values into pairs of normally distributed
   ones.
3. **Hand it to the sampler** reshaped to the latent's dimensions.

A few implementation details that matter in practice:

- The pool file is read in bounded blocks, so peak memory stays at roughly the
  size of one latent regardless of whether the pool is 256 MB or 200 GB.
- Pool values are read as explicit little-endian, so the same pool and seed
  produce the same noise on any machine.
- Normalisation happens in float64: float32 has only a 24-bit mantissa and would
  discard part of the 32-bit entropy before it is used.

## License

GPL-2.0. The Linux kernel module must be GPL-licensed to use kernel APIs, and
the rest of the project follows suit for consistency. See [LICENSE](LICENSE).
