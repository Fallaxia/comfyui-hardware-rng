# ComfyUI Hardware RNG

Replace PyTorch's pseudo-random noise with **true physical entropy** from your
CPU — or with a **reproducible entropy pool file** that lets you replay exact
seeds later.

Works on Linux (kernel module) and Windows (helper DLL). The pool-file mode
works everywhere, including on machines with no supported hardware RNG.

> **🚀 Quick Start / Assets**  
> Don't want to compile anything? Download the pre-compiled **Windows DLL** (`rdseed_win.dll`) and a ready-to-use **2GB Zhaoxin Entropy Pool** (`entropie_hwrng_zhaoxin.dat`) directly from the [Releases](../../releases) page!

---

## Table of contents

- [What this actually does](#what-this-actually-does)
- [Will this improve my images?](#will-this-improve-my-images)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Step 1 — Install the ComfyUI node](#step-1--install-the-comfyui-node-all-platforms)
  - [Step 2a — Linux: build the kernel module](#step-2a--linux-build-the-kernel-module)
  - [Step 2b — Windows: get or build the helper DLL](#step-2b--windows-get-or-build-the-helper-dll)
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

**File mode**
- Nothing special. Works on any machine, including AMD, ARM, or virtualised
  systems with no hardware RNG at all.

---

## Installation

### Step 1 — Install the ComfyUI node (all platforms)

Navigate to your ComfyUI `custom_nodes` folder and clone this repository:

```bash
cd ComfyUI/custom_nodes/
git clone [https://github.com/Fallaxia/comfyui-hardware-rng.git](https://github.com/Fallaxia/comfyui-hardware-rng.git)