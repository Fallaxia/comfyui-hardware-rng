"""
Hardware RNG Noise - ComfyUI custom node
========================================
Replaces PyTorch's pseudo-RNG with true physical entropy from the CPU
(Intel/AMD RDSEED or Zhaoxin/VIA PadLock), or with reproducible entropy
read from a pre-generated pool file.

REPRODUCIBILITY - READ THIS FIRST
---------------------------------
The seed controls the "File (Deterministic)" mode ONLY. In the hardware
modes the CPU delivers fresh entropy on every run, so the same seed will
NOT reproduce the same image. That is the entire point of a true RNG, not
a malfunction. If you want to replay known-good seeds later, you must
generate AND replay in "File (Deterministic)" mode.

DEVICE NAMES
------------
This node deliberately does NOT touch /dev/hwrng. That name belongs to the
kernel's own hw_random framework and is commonly already occupied by a TPM
or virtio-rng device, which are orders of magnitude slower. The companion
kernel module registers unique names instead:
    /dev/raw_hwrng   - Zhaoxin / VIA PadLock backend
    /dev/raw_rdseed  - Intel / AMD RDSEED backend
Use the optional device_path input to point at any other character device.
"""

import os
import re
import sys
import ctypes

import numpy as np
import torch

IS_WINDOWS = sys.platform == "win32"
_rdseed_lib = None

NODE_VERSION = "XeonRDSEEDNoise v5 (2026-08-31: OOM fix restored, unique device names, hardened reads)"
print(f"[HardwareRNG] loaded: {NODE_VERSION}")

# Explicit little-endian instead of np.uint32 (= native byte order).
# Bit-identical on x86/ARM, but it makes a pool file portable: the same
# seed yields the same noise even on a big-endian machine. For a publicly
# shared node that is the cleaner contract.
DTYPE_LE = np.dtype("<u4")

ENTROPY_ENV_VAR = "COMFYUI_ENTROPY_FILE"

SRC_AUTO = "Auto-Detect Hardware"
SRC_RDSEED = "HW: RDSEED (Thermal)"
SRC_HWRNG = "HW: HWRNG (Standard/Quantum)"
SRC_FILE = "File (Deterministic)"

# Preference order for auto-detection: the dedicated PadLock engine is
# faster than RDSEED, so it wins when both are present.
LINUX_DEVICE_CANDIDATES = ("/dev/raw_hwrng", "/dev/raw_rdseed")
DEVICE_FOR_SOURCE = {
    SRC_HWRNG: "/dev/raw_hwrng",
    SRC_RDSEED: "/dev/raw_rdseed",
}

# =============================================================================
# Windows: RDSEED via a small compiled helper DLL, since Python has no direct
# access to CPU instructions.
# =============================================================================
if IS_WINDOWS:
    _dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rdseed_win.dll")
    try:
        _rdseed_lib = ctypes.CDLL(_dll_path)
        # int get_rdseed_buffer(uint32_t* buffer, int count);
        # Returns the NUMBER of uint32 values actually written (0..count).
        _rdseed_lib.get_rdseed_buffer.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
        _rdseed_lib.get_rdseed_buffer.restype = ctypes.c_int
        # int rdseed_available(void);  (added in the v2 DLL)
        if hasattr(_rdseed_lib, "rdseed_available"):
            _rdseed_lib.rdseed_available.argtypes = []
            _rdseed_lib.rdseed_available.restype = ctypes.c_int
    except OSError as exc:
        print(f"[HardwareRNG] Warning: could not load {_dll_path} ({exc}). "
              f"Hardware RNG disabled on Windows, use 'File (Deterministic)'.")
        _rdseed_lib = None


def resolve_entropy_path(file_path):
    """
    Resolve the entropy pool path robustly and return the first existing hit.

    Priority:
      1. Absolute path from the widget (explicit user intent wins)
      2. Environment variable COMFYUI_ENTROPY_FILE
      3. Relative widget path, resolved against THIS file's directory
      4. Relative widget path, resolved against the working directory

    Step 2 exists because some ComfyUI frontend versions can reset a widget
    to its default; the environment variable keeps the workflow running with
    the correct pool anyway:
        export COMFYUI_ENTROPY_FILE=/data/entropy/pool_40gb.dat
    """
    fp = (file_path or "").strip().strip('"').strip("'")
    candidates = []

    if fp and os.path.isabs(fp):
        candidates.append(fp)

    env_path = os.environ.get(ENTROPY_ENV_VAR, "").strip()
    if env_path:
        candidates.append(os.path.abspath(os.path.expanduser(env_path)))

    if fp and not os.path.isabs(fp):
        node_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(node_dir, fp))
        candidates.append(os.path.abspath(fp))

    # De-duplicate while preserving order (node dir and cwd may be identical)
    seen = set()
    unique = []
    for cand in candidates:
        if cand not in seen:
            seen.add(cand)
            unique.append(cand)
    candidates = unique

    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    raise FileNotFoundError(
        "Entropy file not found. Checked locations:\n  "
        + "\n  ".join(candidates if candidates else ["(no path provided)"])
        + f"\nTip: enter an absolute path in the widget, or set the environment "
          f"variable {ENTROPY_ENV_VAR}."
    )


def parse_seed_string(text):
    """
    Turn a text line (e.g. from 'Text Load Line from File') into an integer
    seed. Returns None when nothing was supplied, in which case the
    noise_seed widget value is used.

    Tolerates leading/trailing whitespace, line breaks, quotes and prefixes
    such as "seed: 12345" or "12345 # comment" by taking the FIRST number in
    the line. Ideally the file holds one bare number per line.
    """
    if text is None:
        return None
    s = str(text).strip().strip('"').strip("'").strip()
    if not s:
        return None

    # Fast path: the line already is a bare number
    try:
        return int(s) % (2 ** 64)
    except ValueError:
        pass

    m = re.search(r"\d+", s)
    if m is None:
        raise ValueError(
            f"[HardwareRNG] No number found in seed line: {s!r}. "
            f"The text file should contain exactly one numeric seed per line."
        )
    return int(m.group(0)) % (2 ** 64)


class XeonRDSEEDNoise:
    """Hardware-backed NOISE provider for SamplerCustomAdvanced & friends."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "source": ([SRC_AUTO, SRC_RDSEED, SRC_HWRNG, SRC_FILE], {"default": SRC_AUTO}),
                # Scales the standard deviation of the noise. Useful for
                # checkpoints that were not trained on a perfect N(0,1).
                "noise_scale": ("FLOAT", {"default": 1.00, "min": 0.50, "max": 1.50, "step": 0.01}),
                "file_path": ("STRING", {
                    "default": "entropie.dat",
                    "tooltip": "Path to the entropy pool file. Absolute paths are safest. "
                               "Alternatively set COMFYUI_ENTROPY_FILE, which acts as a fallback "
                               "if this field ever resets to its default."
                }),
            },
            "optional": {
                # Deliberately WITHOUT "forceInput": True. Depending on the
                # frontend version forceInput creates a socket without a
                # matching widgets_values entry while other versions reserve
                # the slot anyway. Save and load then disagree about the
                # widget count, the positional mapping shifts, and the last
                # widget silently reverts to its default. As a plain widget
                # the count is stable, and a link can still be dropped
                # directly onto the field.
                #
                # NOTE FOR FUTURE EDITS: always append new inputs at the END.
                "seed_string": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": "Optional seed as text. Connect 'Text Load Line from File' here. "
                               "Non-empty content takes precedence over noise_seed."
                }),
                "device_path": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": "Optional explicit character device to read from, e.g. /dev/raw_rdseed. "
                               "Leave empty to use the device implied by 'source'. Ignored on Windows."
                }),
            },
        }

    RETURN_TYPES = ("NOISE",)
    FUNCTION = "get_noise"
    CATEGORY = "Hardware/RNG"

    @classmethod
    def IS_CHANGED(cls, noise_seed, source, noise_scale, file_path,
                   seed_string="", device_path=""):
        # Hardware modes produce new entropy on every run. Without this,
        # ComfyUI would cache the result and hand back the same image when
        # no widget value changed.
        if source != SRC_FILE:
            return float("nan")
        return f"{noise_seed}|{source}|{noise_scale}|{file_path}|{seed_string}|{device_path}"

    def get_noise(self, noise_seed, source, noise_scale, file_path,
                  seed_string="", device_path=""):

        # A non-empty string seed overrides the widget value
        parsed = parse_seed_string(seed_string)
        effective_seed = parsed if parsed is not None else noise_seed
        if parsed is not None:
            print(f"[HardwareRNG] Seed applied from string: {effective_seed}")
            if source != SRC_FILE:
                print(f"[HardwareRNG] WARNING: source='{source}' ignores the seed entirely. "
                      f"Switch to '{SRC_FILE}' for reproducible images.")

        class HardwareNoise:
            def __init__(self, seed, src, scale, path, dev_path):
                self.seed = seed
                self.source = src
                self.scale = scale
                self.file_path = path
                self.device_path = (dev_path or "").strip()

            # -- ComfyUI calls this once per sampler run -------------------
            def generate_noise(self, input_latent):
                latent_tensor = input_latent["samples"]
                shape = latent_tensor.shape
                num_elements = int(np.prod(shape))

                # Box-Muller consumes value PAIRS. For an odd element count
                # we request one extra value and drop it afterwards.
                is_odd = num_elements % 2 != 0
                gen_elements = num_elements + 1 if is_odd else num_elements

                if self.source == SRC_FILE:
                    uint32_array = self._read_from_file(gen_elements)
                else:
                    uint32_array = self._read_from_hardware(gen_elements, self.source)

                noise_1d = self._apply_box_muller(uint32_array)

                if is_odd:
                    noise_1d = noise_1d[:-1]

                return torch.from_numpy(noise_1d).reshape(shape).to(
                    device=latent_tensor.device,
                    dtype=latent_tensor.dtype,
                )

            # -- hardware paths -------------------------------------------
            def _pick_linux_device(self, source_type):
                if self.device_path:
                    return self.device_path

                if source_type in DEVICE_FOR_SOURCE:
                    return DEVICE_FOR_SOURCE[source_type]

                # Auto-detect: use whichever device the kernel module created.
                for cand in LINUX_DEVICE_CANDIDATES:
                    if os.path.exists(cand):
                        return cand

                raise RuntimeError(
                    "No hardware RNG device found. Expected one of: "
                    + ", ".join(LINUX_DEVICE_CANDIDATES)
                    + ". Load the raw_hw_rng kernel module (check dmesg), or set "
                      "device_path explicitly, or switch to 'File (Deterministic)'.\n"
                    "Note: /dev/hwrng is intentionally not used - that name belongs to "
                    "the kernel's own hw_random framework and is often a much slower "
                    "TPM or virtio-rng device."
                )

            def _read_from_hardware(self, num_elements, source_type):
                uint32_array = np.zeros(num_elements, dtype=np.uint32)

                if IS_WINDOWS:
                    if source_type == SRC_HWRNG:
                        raise RuntimeError(
                            "The Zhaoxin/PadLock backend is Linux-only. On Windows choose "
                            f"'{SRC_RDSEED}', '{SRC_AUTO}' or '{SRC_FILE}'."
                        )
                    if _rdseed_lib is None:
                        raise RuntimeError(
                            "rdseed_win.dll is not loaded. Place it next to this node file, "
                            f"or use '{SRC_FILE}'."
                        )
                    if hasattr(_rdseed_lib, "rdseed_available") and not _rdseed_lib.rdseed_available():
                        raise RuntimeError(
                            "This CPU does not support the RDSEED instruction. "
                            f"Use '{SRC_FILE}' instead."
                        )

                    c_buffer = uint32_array.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32))
                    filled = _rdseed_lib.get_rdseed_buffer(c_buffer, num_elements)
                    if filled != num_elements:
                        raise RuntimeError(
                            f"RDSEED delivered only {filled} of {num_elements} values. "
                            f"Either the CPU entropy pool is exhausted (retry shortly), or an "
                            f"outdated rdseed_win.dll is in use - the current DLL returns the "
                            f"number of values written, older ones returned 1/0. Please rebuild it."
                        )
                    return uint32_array

                dev_path = self._pick_linux_device(source_type)
                self._read_linux_device(dev_path, uint32_array)
                return uint32_array

            def _read_linux_device(self, device_path, target_array):
                bytes_needed = target_array.size * 4
                try:
                    with open(device_path, "rb") as f:
                        raw_bytes = f.read(bytes_needed)
                except PermissionError:
                    raise RuntimeError(
                        f"No read permission for {device_path}. Add a udev rule or a group "
                        f"membership for it (see README)."
                    )
                except BlockingIOError:
                    raise RuntimeError(
                        f"{device_path} would block. Is rngd holding the device exclusively?"
                    )
                except FileNotFoundError:
                    raise RuntimeError(
                        f"{device_path} does not exist. Is the raw_hw_rng kernel module loaded? "
                        f"Check dmesg."
                    )
                except OSError as exc:
                    raise RuntimeError(f"Error reading {device_path}: {exc}")

                # The driver may legitimately return a short read (signal,
                # O_NONBLOCK). Without this check the assignment below would
                # fail with an opaque shape error.
                if len(raw_bytes) != bytes_needed:
                    raise RuntimeError(
                        f"Short read from {device_path}: got {len(raw_bytes)} of {bytes_needed} "
                        f"bytes. The entropy source may be saturated - retry, or use "
                        f"'{SRC_FILE}'."
                    )

                target_array[:] = np.frombuffer(raw_bytes, dtype=np.uint32)

            # -- deterministic file path -----------------------------------
            def _read_from_file(self, num_elements):
                """
                Reads entropy from a pre-generated pool file, starting at a
                position derived from the seed. Same seed -> same noise.
                """
                target_path = resolve_entropy_path(self.file_path)

                file_size = os.path.getsize(target_path)
                total_uint32 = file_size // 4
                if total_uint32 == 0:
                    raise ValueError(
                        f"{target_path} is empty or holds no complete 32-bit values."
                    )

                # Deterministic start offset. 9973 is prime, so consecutive
                # seeds never collide within the file.
                start_idx = (self.seed * 9973) % total_uint32

                if num_elements > total_uint32:
                    print(
                        f"[HardwareRNG] WARNING: pool holds only {total_uint32} values but "
                        f"{num_elements} are needed. The file will be tiled, which creates "
                        f"visible repeating noise patterns. Use a larger pool "
                        f"(rule of thumb: at least 100x one latent)."
                    )

                uint32_array = np.empty(num_elements, dtype=DTYPE_LE)

                # Block-wise read with wraparound.
                # An earlier version read the ENTIRE file in the wraparound
                # case (f.read(total_uint32 * 4)) just to take a few thousand
                # values from it - peak memory was twice the file size (bytes
                # object plus numpy copy). On a 200 GB pool that meant ~400 GB.
                # This loop keeps peak memory at num_elements * 4 bytes,
                # independent of pool size, and produces bit-identical results.
                with open(target_path, "rb") as f:
                    elements_read = 0
                    current_idx = start_idx
                    while elements_read < num_elements:
                        elements_available = total_uint32 - current_idx
                        elements_to_read = min(num_elements - elements_read, elements_available)

                        f.seek(current_idx * 4)
                        raw = f.read(elements_to_read * 4)
                        if len(raw) != elements_to_read * 4:
                            raise RuntimeError(
                                f"Short read in {target_path}: {len(raw)} instead of "
                                f"{elements_to_read * 4} bytes at offset {current_idx * 4}. "
                                f"Was the file modified while running?"
                            )

                        uint32_array[elements_read: elements_read + elements_to_read] = \
                            np.frombuffer(raw, dtype=DTYPE_LE)
                        elements_read += elements_to_read
                        current_idx = 0  # wrap to the beginning for further passes

                return uint32_array

            # -- uniform -> gaussian ---------------------------------------
            def _apply_box_muller(self, uint32_array):
                """
                Converts uniformly distributed 32-bit integers into N(0, 1),
                which is what diffusion samplers expect.

                Requires the source values to be uniform across the FULL
                uint32 range - that is what both the hardware devices and a
                properly generated pool file provide.
                """
                # float64, not float32: float32 has only a 24-bit mantissa and
                # would throw away part of the 32-bit entropy before use.
                uniform_floats = uint32_array.astype(np.float64) / (2 ** 32 - 1)

                # Clamp u1 away from zero to avoid log(0)
                u1 = np.maximum(uniform_floats[0::2], 1e-12)
                u2 = uniform_floats[1::2]

                radius = np.sqrt(-2.0 * np.log(u1))
                z0 = radius * np.cos(2.0 * np.pi * u2)
                z1 = radius * np.sin(2.0 * np.pi * u2)

                gaussian_array = np.empty_like(uniform_floats)
                gaussian_array[0::2] = z0
                gaussian_array[1::2] = z1

                # Scale the standard deviation only; the mean stays at 0.
                return (gaussian_array * self.scale).astype(np.float32)

        return (HardwareNoise(effective_seed, source, noise_scale, file_path, device_path),)


NODE_CLASS_MAPPINGS = {
    "XeonRDSEEDNoise": XeonRDSEEDNoise,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "XeonRDSEEDNoise": "Hardware RNG Noise (RDSEED/HWRNG/File)",
}
