/*
 * rdseed_win - minimal RDSEED helper DLL for the ComfyUI Hardware RNG node
 *
 * Python cannot issue CPU instructions directly, so this small library
 * exposes RDSEED to ctypes.
 *
 * Build (Visual Studio Developer Command Prompt):
 *     cl /O2 /LD rdseed_win.c /Fe:rdseed_win.dll
 * Build (MinGW-w64):
 *     gcc -O2 -shared -o rdseed_win.dll rdseed_win.c
 *
 * API CHANGE IN v2 - REBUILD REQUIRED
 * -----------------------------------
 * get_rdseed_buffer() now returns the NUMBER OF VALUES WRITTEN (0..count)
 * instead of a 1/0 success flag. That lets the caller report partial fills
 * precisely instead of guessing. The Python node checks for an exact match,
 * so an old DLL fails loudly with a clear message rather than silently
 * handing back a half-filled buffer.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <immintrin.h>
#include <intrin.h>
#include <stdint.h>

/*
 * Retry policy.
 * Intel's guidance is to retry RDSEED a bounded number of times with a PAUSE
 * in between, then back off and let the hardware reseed. The thermal source
 * needs real wall-clock time, so pure spinning cannot help once the pool is
 * drained.
 *
 * Sleep(0) only yields the remaining time slice and is cheap. Sleep(1) is
 * NOT one millisecond - with the default Windows timer resolution it parks
 * the thread for roughly 15.6 ms. The old version called Sleep(1) up to 1000
 * times PER VALUE, i.e. a worst case of about 15 seconds for a single 32-bit
 * word. The staged back-off below caps the worst case per value at well
 * under a second while still giving the DRNG time to recover.
 */
#define RDSEED_SPIN_RETRIES   100   /* pause-loop attempts per cycle       */
#define RDSEED_YIELD_CYCLES    16   /* cheap Sleep(0) cycles first         */
#define RDSEED_TOTAL_CYCLES    48   /* remaining cycles use Sleep(1)       */

static int g_rdseed_supported = -1;  /* -1 = not probed yet */

static int probe_rdseed_support(void)
{
    int regs[4];

    __cpuid(regs, 0);
    if (regs[0] < 7)
        return 0;                       /* leaf 7 not available at all */

    __cpuidex(regs, 7, 0);
    return (regs[1] & (1 << 18)) != 0;  /* EBX bit 18 = RDSEED */
}

/*
 * Exported so the caller can give a helpful error instead of crashing with
 * an illegal-instruction fault on a CPU without RDSEED.
 */
__declspec(dllexport) int rdseed_available(void)
{
    if (g_rdseed_supported < 0)
        g_rdseed_supported = probe_rdseed_support();
    return g_rdseed_supported;
}

static int rdseed32_retry(uint32_t *out)
{
    int cycle, i;

    for (cycle = 0; cycle < RDSEED_TOTAL_CYCLES; cycle++) {
        for (i = 0; i < RDSEED_SPIN_RETRIES; i++) {
            if (_rdseed32_step(out))
                return 1;
            _mm_pause();  /* avoids pipeline stalls while polling */
        }
        Sleep(cycle < RDSEED_YIELD_CYCLES ? 0 : 1);
    }
    return 0;
}

#if defined(_M_X64) || defined(__x86_64__)
static int rdseed64_retry(unsigned __int64 *out)
{
    int cycle, i;

    for (cycle = 0; cycle < RDSEED_TOTAL_CYCLES; cycle++) {
        for (i = 0; i < RDSEED_SPIN_RETRIES; i++) {
            if (_rdseed64_step(out))
                return 1;
            _mm_pause();
        }
        Sleep(cycle < RDSEED_YIELD_CYCLES ? 0 : 1);
    }
    return 0;
}
#endif

/*
 * Fills buffer with `count` 32-bit random values.
 * Returns the number of values actually written (== count on success).
 */
__declspec(dllexport) int get_rdseed_buffer(uint32_t *buffer, int count)
{
    int filled = 0;

    if (buffer == NULL || count <= 0)
        return 0;
    if (!rdseed_available())
        return 0;

#if defined(_M_X64) || defined(__x86_64__)
    /*
     * On 64-bit, harvest 64 bits per instruction. This halves the number of
     * RDSEED executions, which matters: a single 768x576 latent needs about
     * 110000 values.
     */
    while (filled + 2 <= count) {
        unsigned __int64 val64;

        if (!rdseed64_retry(&val64))
            return filled;              /* partial fill, caller decides */

        buffer[filled++] = (uint32_t)(val64 & 0xFFFFFFFFu);
        buffer[filled++] = (uint32_t)(val64 >> 32);
    }
#endif

    /* Remainder (and the whole job on 32-bit builds) */
    while (filled < count) {
        uint32_t val32;

        if (!rdseed32_retry(&val32))
            return filled;

        buffer[filled++] = val32;
    }

    return filled;
}
