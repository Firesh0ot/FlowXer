// PGM audio: audio follows video, with an equal-power crossfade during a Mix.
#pragma once

#include "core/tai.hpp"

#include <cstddef>
#include <cstdint>

namespace fxeng
{
constexpr std::int64_t kAudioRate = 48000;

struct CrossfadeGains
{
    float a;
    float b;
};

// Equal power: cos/sin of t·π/2, so two unrelated sources keep their loudness through the Mix
// (a² + b² = 1). t = 0 is all a, t = 1 all b.
CrossfadeGains crossfade(float t);

// One channel: out[s] = a[s]·ga + b[s]·gb with t moving linearly from t0 (before the first
// sample) to t1 (at the last), so each grain continues where the previous one ended.
void crossfadeBlock(float const* a, float const* b, float* out, std::size_t samples, float t0, float t1);

// The 48 kHz sample range of output grain `index`: [first, end). It matches the grain's TAI
// span; MXL continuous flows index samples on the same TAI clock.
struct SampleRange
{
    std::uint64_t first = 0;
    std::uint64_t end = 0;
};
SampleRange grainSamples(Rate rate, std::uint64_t index);
} // namespace fxeng
