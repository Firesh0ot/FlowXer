#include "core/audio.hpp"

#include <cmath>

namespace fxeng
{
CrossfadeGains crossfade(float t)
{
    if (t <= 0.f)
    {
        return {1.f, 0.f};
    }
    if (t >= 1.f)
    {
        return {0.f, 1.f};
    }
    float const angle = t * 1.57079632679f;
    return {std::cos(angle), std::sin(angle)};
}

void crossfadeBlock(float const* a, float const* b, float* out, std::size_t samples, float t0, float t1)
{
    for (std::size_t s = 0; s < samples; ++s)
    {
        float const t = t0 + (t1 - t0) * static_cast<float>(s + 1) / static_cast<float>(samples);
        auto const g = crossfade(t);
        out[s] = a[s] * g.a + b[s] * g.b;
    }
}

SampleRange grainSamples(Rate rate, std::uint64_t index)
{
    Rate const audio{kAudioRate, 1};
    return SampleRange{timestampToIndex(audio, indexToTimestamp(rate, index)), timestampToIndex(audio, indexToTimestamp(rate, index + 1))};
}
} // namespace fxeng
