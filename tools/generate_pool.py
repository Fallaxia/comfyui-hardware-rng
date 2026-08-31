#!/usr/bin/env python3
"""
Generate an entropy pool file for the "File (Deterministic)" mode.

The pool MUST be uniformly distributed across the full 32-bit range - the
Box-Muller transform in the node relies on that. Sequential or structured
data produces wildly wrong standard deviations.

Examples
--------
From the kernel module (true hardware entropy, recommended):
    ./generate_pool.py --size-gb 4 --source /dev/raw_rdseed --out pool.dat

From the OS CSPRNG (fast, still cryptographically strong):
    ./generate_pool.py --size-gb 4 --source urandom --out pool.dat

Rule of thumb: at least 100x the size of one latent. A 768x576 latent with
a 16-channel VAE needs ~432 KB, so even 1 GB gives plenty of headroom.
"""

import argparse
import os
import sys

CHUNK = 8 * 1024 * 1024  # 8 MiB per write


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output file")
    ap.add_argument("--size-gb", type=float, required=True, help="target size in GiB")
    ap.add_argument("--source", default="urandom",
                    help="'urandom' or a character device such as /dev/raw_rdseed")
    args = ap.parse_args()

    total = int(args.size_gb * 1024 ** 3)
    total -= total % 4  # whole 32-bit values only
    if total <= 0:
        sys.exit("size-gb too small")

    use_urandom = args.source == "urandom"
    if not use_urandom and not os.path.exists(args.source):
        sys.exit(f"source device not found: {args.source}")

    written = 0
    src = None if use_urandom else open(args.source, "rb")
    try:
        with open(args.out, "wb") as dst:
            while written < total:
                n = min(CHUNK, total - written)
                data = os.urandom(n) if use_urandom else src.read(n)
                if len(data) != n:
                    sys.exit(f"\nshort read from {args.source}: {len(data)} of {n} bytes")
                dst.write(data)
                written += n
                pct = 100.0 * written / total
                print(f"\r{written / 1024 ** 3:.2f} / {total / 1024 ** 3:.2f} GiB  ({pct:5.1f}%)",
                      end="", flush=True)
    finally:
        if src:
            src.close()

    print(f"\nDone: {args.out}")
    print("Verify the distribution before use:")
    print("  python3 -c \"import numpy as np;"
          "a=np.fromfile('%s',dtype='<u4',count=1000000);"
          "print('mean/2^32 =',a.mean()/2**32,'(expect ~0.5)')\"" % args.out)


if __name__ == "__main__":
    main()
