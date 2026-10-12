// TAI grain arithmetic of the output loop. The index formulas are MXL's
// (dmf-mxl/mxl lib/internal/include/mxl-internal/IndexConversion.hpp, Apache-2.0), so the
// engine and libmxl agree on every index. The loop rule (one grain per period on the
// TAI grid, never slide) follows the playout loop of mxl-replay (same author).
#pragma once

#include <cstdint>

#include <nlohmann/json_fwd.hpp>

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

// The latency L (grains) that covers an input arriving `lagGrains` after its TAI time: output
// grain i is rendered from half a grain before its TAI time and needs input grain i - L, so
// L >= lag + 0.5; a tenth of a grain of margin, at least 1.
inline int latencyFor(double lagGrains)
{
    double const need = (lagGrains < 0 ? 0.0 : lagGrains) + 0.5 + 0.1;
    int latency = static_cast<int>(need);
    if (static_cast<double>(latency) < need)
    {
        ++latency;
    }
    return latency < 1 ? 1 : latency;
}

// The latency that covers the latest arrival seen (each source's `peak` since it was routed, ms)
// of what delivers, from the engine's status: each input's video and its audio while they run
// (`inputs[].arrival_ms`, `inputs[].audio.lag_ms`) and each key flow in use while it runs
// (`key_flows[].arrival_ms`). Unrouted or silent inputs and key flows, clip players and stinger
// players do not count.
int recommendedLatency(nlohmann::json const& status, double periodMs);

// The input grain that output grain `output` is composed from (fixed latency L).
inline std::uint64_t inputIndexFor(std::uint64_t output, int latency)
{
    auto const l = static_cast<std::uint64_t>(latency < 0 ? 0 : latency);
    return output >= l ? output - l : 0;
}
} // namespace fxeng
