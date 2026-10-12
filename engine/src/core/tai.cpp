#include "core/tai.hpp"

#include <algorithm>

#include <nlohmann/json.hpp>

namespace fxeng
{
std::uint64_t timestampToIndex(Rate rate, std::uint64_t ns)
{
    if (rate.num <= 0 || rate.den <= 0)
    {
        return UINT64_MAX;
    }
    auto const num = static_cast<__int128>(ns) * rate.num + static_cast<__int128>(500'000'000) * rate.den;
    return static_cast<std::uint64_t>(num / (static_cast<__int128>(1'000'000'000) * rate.den));
}

std::uint64_t indexToTimestamp(Rate rate, std::uint64_t index)
{
    if (rate.num <= 0 || rate.den <= 0)
    {
        return UINT64_MAX;
    }
    auto const num = static_cast<__int128>(index) * rate.den * 1'000'000'000 + rate.num / 2;
    return static_cast<std::uint64_t>(num / rate.num);
}

std::uint64_t windowStart(Rate rate, std::uint64_t index)
{
    if (rate.num <= 0 || rate.den <= 0 || index == 0)
    {
        return 0;
    }
    // Smallest t with t*num + 5e8*den >= index*1e9*den.
    auto const need = static_cast<__int128>(index) * 1'000'000'000 * rate.den - static_cast<__int128>(500'000'000) * rate.den;
    return static_cast<std::uint64_t>((need + rate.num - 1) / rate.num);
}

std::uint64_t periodNs(Rate rate)
{
    if (rate.num <= 0 || rate.den <= 0)
    {
        return 0;
    }
    return static_cast<std::uint64_t>(static_cast<__int128>(1'000'000'000) * rate.den / rate.num);
}

int recommendedLatency(nlohmann::json const& status, double periodMs)
{
    double worst = 0;
    auto const take = [&](nlohmann::json const& item, char const* window) {
        if (item.value("state", "") == "running" && item.contains(window))
        {
            worst = std::max(worst, item[window].value("peak", 0.0));
        }
    };
    for (auto const& input : status.value("inputs", nlohmann::json::array()))
    {
        take(input, "arrival_ms");
        if (input.contains("audio"))
        {
            take(input["audio"], "lag_ms");
        }
    }
    for (auto const& key : status.value("key_flows", nlohmann::json::array()))
    {
        take(key, "arrival_ms");
    }
    return latencyFor(periodMs > 0 ? worst / periodMs : 0.0);
}

NextIndex nextOutputIndex(std::uint64_t next, std::uint64_t current)
{
    if (next == 0 || next > current + 1)
    {
        return NextIndex{current, 0};
    }
    if (current > next)
    {
        return NextIndex{current, current - next};
    }
    return NextIndex{next, 0};
}
} // namespace fxeng
