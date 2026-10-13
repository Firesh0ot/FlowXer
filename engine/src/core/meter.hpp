// P5 meters (docs/engine-p5.md §7): per channel sample peak and RMS over 100 ms blocks of the 48 kHz
// sample index (4800 samples, the same grid at every frame rate); for a PGM also the true peak
// (ITU-R BS.1770-4 annex 2: 4× oversampling with its 48-tap filter), overs (samples at or above
// 0 dBFS) and per channel pair the loudness of BS.1770-4 / EBU R 128: K-weighting (two biquads in
// double), momentary (the last 4 blocks), short-term (the last 30) and integrated (400 ms gating
// blocks with 75 % overlap, gated at −70 LUFS and −10 LU relative, kept in a histogram of 0.1 LU
// bins so its cost stays the same after hours). CPU only, unit-tested with EBU Tech 3341 signals.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace fxeng
{
constexpr std::size_t kMeterBlock = 4800;
// Values below this are this (silence).
constexpr float kMeterFloorDb = -120.f;

// 20·log10(v) of a level, 10·log10 of a power, floored at kMeterFloorDb.
float levelDb(double v);
float powerDb(double p);

struct PairLoudness
{
    float momentary = kMeterFloorDb;
    float shortTerm = kMeterFloorDb;
    float integrated = kMeterFloorDb;
};

// One finished 100 ms block, and the last second (its 10 blocks: the maximum peaks, the RMS over
// them).
struct MeterValues
{
    // The block's number: its first sample index / 4800.
    std::uint64_t block = 0;
    std::vector<float> peak;
    std::vector<float> rms;
    std::vector<float> truePeak;
    std::vector<float> peak1s;
    std::vector<float> rms1s;
    std::vector<float> truePeak1s;
    std::vector<PairLoudness> loudness;
    std::uint64_t overs = 0;
};

class Meter
{
public:
    // `program`: true peak, overs and loudness too (a PGM); else peak and RMS (a source).
    Meter(std::size_t channels, bool program);

    // Samples [first, first + n) of every channel, channel-major (channels × n; nullptr: silence).
    // A jump in the sample index starts the block again. True when a block finished (values()).
    bool add(float const* x, std::size_t n, std::uint64_t first);
    [[nodiscard]] MeterValues const& values() const
    {
        return values_;
    }
    // Integrated loudness from the next block on (its first 400 ms gating block is the first one
    // without samples from before).
    void resetLoudness();
    [[nodiscard]] std::size_t channels() const
    {
        return channels_;
    }

private:
    struct Channel
    {
        float peak = 0.f;
        float truePeak = 0.f;
        double sum = 0.0;
        // K-weighting state (two biquads, transposed direct form II) and the block's weighted sum.
        std::array<double, 4> z{};
        double ksum = 0.0;
        // The last 11 samples, for the true-peak filter.
        std::array<float, 11> history{};
    };
    // A channel pair's loudness: the K-weighted mean square of the last 30 blocks, and the
    // integrated histogram (power sums and counts per 0.1 LU bin, −70 … +10 LUFS).
    struct Pair
    {
        std::array<double, 30> blocks{};
        std::vector<double> power;
        std::vector<std::uint32_t> count;
        // Blocks since the last reset (gating blocks need 4).
        int since = 0;
    };

    void run(std::size_t c, float const* x, std::size_t m);
    void endBlock();

    std::size_t channels_;
    bool program_;
    std::vector<Channel> ch_;
    std::vector<Pair> pairs_;
    std::size_t filled_ = 0;
    std::uint64_t next_ = 0;
    bool started_ = false;
    std::uint64_t blocks_ = 0;
    std::uint64_t overs_ = 0;
    bool reset_ = false;
    // The last 10 blocks per channel: peak, mean square, true peak.
    std::vector<std::array<float, 10>> lastPeak_;
    std::vector<std::array<double, 10>> lastMs_;
    std::vector<std::array<float, 10>> lastTp_;
    std::vector<float> scratch_;
    std::vector<float> acc_;
    MeterValues values_;
};
} // namespace fxeng
