// TAI grain arithmetic of the output loop. The index formulas are MXL's
// (dmf-mxl/mxl lib/internal/include/mxl-internal/IndexConversion.hpp, Apache-2.0), so the
// engine and libmxl agree on every index. The loop rule (one grain per period on the
// TAI grid, never slide) follows the playout loop of mxl-replay (same author).
#pragma once

#include <cstdint>

namespace fxeng
{
struct Rate
{
    std::int64_t num = 50;
    std::int64_t den = 1;
};

// MXL rounds: grain i is the current grain from T(i) - half a period to T(i) + half a period.
std::uint64_t timestampToIndex(Rate rate, std::uint64_t ns);
std::uint64_t indexToTimestamp(Rate rate, std::uint64_t index);
// First nanosecond at which `index` is the current grain (index >= 1).
std::uint64_t windowStart(Rate rate, std::uint64_t index);
std::uint64_t periodNs(Rate rate);

struct NextIndex
{
    std::uint64_t index = 0;
    // Grains that were not rendered because TAI had already passed them.
    std::uint64_t skipped = 0;
};

// The output grain to render next. `next` (the grain after the last one rendered) unless the
// clock already passed it: then the current grain, and the grains in between count as skipped.
// The output never slides behind TAI. `next == 0` (start) and a clock step back restart at
// the current grain without counting.
NextIndex nextOutputIndex(std::uint64_t next, std::uint64_t current);

// The input grain that output grain `output` is composed from (fixed latency L).
inline std::uint64_t inputIndexFor(std::uint64_t output, int latency)
{
    auto const l = static_cast<std::uint64_t>(latency < 0 ? 0 : latency);
    return output >= l ? output - l : 0;
}
} // namespace fxeng
