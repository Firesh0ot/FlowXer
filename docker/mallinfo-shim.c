/*
 * Saturating mallinfo() for the CEF browser that cefsrc runs inside the mixer
 * process. The image preloads it (LD_PRELOAD).
 *
 * libcef.so is built against an older glibc and reads the malloc totals with
 * the legacy mallinfo(), whose fields are int. glibc 2.33 and later fill them
 * by truncating the size_t totals of mallinfo2(), so they wrap once the process
 * holds 2 GiB or more from malloc. Chromium's memory dumps (MallocDumpProvider,
 * from time to time) check them: checked_cast<size_t>(info.arena + info.hblkhd)
 * and checked_cast<size_t>(info.uordblks). A negative value stops the whole
 * mixer with SIGILL (exit 132).
 *
 * This mallinfo() reports the same totals capped at INT_MAX, and caps
 * arena + hblkhd as a pair because Chromium adds them as int.
 */
#include <limits.h>
#include <malloc.h>

static int cap(size_t value)
{
    return value > INT_MAX ? INT_MAX : (int)value;
}

struct mallinfo mallinfo(void)
{
    struct mallinfo2 m = mallinfo2();
    struct mallinfo r = {
        .arena = cap(m.arena),
        .ordblks = cap(m.ordblks),
        .smblks = cap(m.smblks),
        .hblks = cap(m.hblks),
        .hblkhd = cap(m.hblkhd),
        .usmblks = cap(m.usmblks),
        .fsmblks = cap(m.fsmblks),
        .uordblks = cap(m.uordblks),
        .fordblks = cap(m.fordblks),
        .keepcost = cap(m.keepcost),
    };

    if (r.hblkhd > INT_MAX - r.arena)
        r.hblkhd = INT_MAX - r.arena;
    return r;
}
