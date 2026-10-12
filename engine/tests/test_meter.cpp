// P5 meters (docs/engine-p5.md §7): peak and RMS, true peak, overs, and BS.1770-4 / EBU R 128
// loudness against EBU Tech 3341 signals generated here (stereo 1 kHz sines; momentary,
// short-term and integrated within ±0.1 LU), true-peak signals within +0.2 / −0.4 dB.
#include "test.hpp"

#include "core/meter.hpp"

#include <cmath>
#include <functional>
#include <vector>

using namespace fxeng;

namespace
{
constexpr double kPi = 3.14159265358979323846;

struct Seg
{
    double hz;
    double dbfs;  // below −200: silence
    double seconds;
    double phaseDeg = 0.0;
};

// Feeds a stereo signal to `meter` in grains of `grain` samples (or 800/801 alternating with 0),
// from sample index `start`; `each` after every finished block.
void feed(Meter& meter, std::vector<Seg> const& segs, std::size_t grain = 960, std::uint64_t start = 960'000'000ull,
    std::function<void(MeterValues const&, double)> const& each = {}, double amplitudeOverride = -1.0)
{
    std::vector<double> mono;
    for (auto const& s : segs)
    {
        auto const n = static_cast<std::size_t>(std::llround(s.seconds * 48000.0));
        double const amp = amplitudeOverride >= 0.0 ? amplitudeOverride : (s.dbfs < -200.0 ? 0.0 : std::pow(10.0, s.dbfs / 20.0));
        for (std::size_t k = 0; k < n; ++k)
        {
            double const t = static_cast<double>(mono.size());
            mono.push_back(amp * std::sin(2.0 * kPi * s.hz * t / 48000.0 + s.phaseDeg * kPi / 180.0));
        }
    }
    std::size_t const ch = meter.channels();
    std::vector<float> buffer;
    std::size_t done = 0;
    std::uint64_t at = start;
    int toggle = 0;
    while (done < mono.size())
    {
        std::size_t n = grain;
        if (grain == 0)
        {
            // 59.94p: 800 or 801 samples per grain.
            n = (toggle++ % 5 == 4) ? 801 : 800;
        }
        n = std::min(n, mono.size() - done);
        buffer.assign(ch * n, 0.f);
        for (std::size_t c = 0; c < ch; ++c)
        {
            for (std::size_t s = 0; s < n; ++s)
            {
                buffer[c * n + s] = static_cast<float>(mono[done + s]);
            }
        }
        if (meter.add(buffer.data(), n, at) && each)
        {
            each(meter.values(), static_cast<double>(done + n) / 48000.0);
        }
        done += n;
        at += n;
    }
}

bool near(double value, double expected, double tolerance)
{
    return std::abs(value - expected) <= tolerance;
}
} // namespace

TEST(meter_peak_rms_and_the_block_grid)
{
    // A 1 kHz sine at 0.5: peak 0.5 (a sample at 90°), RMS 0.5 / √2, on 100 ms blocks of the index.
    Meter meter(2, false);
    std::vector<std::uint64_t> blocks;
    feed(meter, {{1000.0, 20.0 * std::log10(0.5), 1.0}}, 960, 960'000'000ull, [&](MeterValues const& v, double) { blocks.push_back(v.block); });
    auto const& v = meter.values();
    CHECK(v.peak[0] == 0.5f && v.peak[1] == 0.5f);
    CHECK(near(v.rms[0], 0.5 / std::sqrt(2.0), 1e-5) && near(v.rms1s[1], 0.5 / std::sqrt(2.0), 1e-5));
    CHECK(v.truePeak.empty() && v.loudness.empty());
    CHECK_EQ(blocks.size(), 10u);
    CHECK_EQ(blocks.front(), 960'000'000ull / 4800);
    for (std::size_t k = 1; k < blocks.size(); ++k)
    {
        CHECK_EQ(blocks[k], blocks[k - 1] + 1);
    }
    // 59.94p grains (800 / 801 samples) on the same grid give the same values.
    Meter ntsc(2, false);
    std::size_t count = 0;
    feed(ntsc, {{1000.0, 20.0 * std::log10(0.5), 1.0}}, 0, 960'000'000ull, [&](MeterValues const&, double) { ++count; });
    CHECK(count >= 9 && ntsc.values().peak[0] == 0.5f && near(ntsc.values().rms[0], 0.5 / std::sqrt(2.0), 1e-5));
    // Silence is the floor.
    Meter quiet(2, true);
    std::vector<float> zeros(2 * 4800, 0.f);
    CHECK(quiet.add(zeros.data(), 4800, 4800));
    CHECK(levelDb(quiet.values().peak[0]) == kMeterFloorDb && quiet.values().loudness[0].momentary == kMeterFloorDb);
    CHECK(quiet.values().block == 1u && quiet.add(nullptr, 4800, 9600) && quiet.values().block == 2u);
    CHECK(levelDb(0.5) > -6.03f && levelDb(0.5) < -6.02f && powerDb(0.0) == kMeterFloorDb);
}

TEST(meter_ebu_tech_3341_momentary_short_term_integrated)
{
    // Cases 1 and 2: stereo 1 kHz at −23 and −33 dBFS, 20 s: M, S and I at the level ±0.1 LU.
    for (double level : {-23.0, -33.0})
    {
        Meter m(2, true);
        feed(m, {{1000.0, level, 20.0}});
        auto const& l = m.values().loudness[0];
        CHECK(near(l.momentary, level, 0.1) && near(l.shortTerm, level, 0.1) && near(l.integrated, level, 0.1));
    }
    // Case 3: −36 / −23 / −36 dBFS for 10 / 60 / 10 s: I −23 ±0.1 (the relative gate).
    {
        Meter m(2, true);
        feed(m, {{1000.0, -36.0, 10.0}, {1000.0, -23.0, 60.0}, {1000.0, -36.0, 10.0}});
        CHECK(near(m.values().loudness[0].integrated, -23.0, 0.1));
    }
    // Case 4: −72 / −36 / −23 / −36 / −72 dBFS for 10 / 10 / 60 / 10 / 10 s (both gates).
    {
        Meter m(2, true);
        feed(m, {{1000.0, -72.0, 10.0}, {1000.0, -36.0, 10.0}, {1000.0, -23.0, 60.0}, {1000.0, -36.0, 10.0}, {1000.0, -72.0, 10.0}});
        CHECK(near(m.values().loudness[0].integrated, -23.0, 0.1));
    }
    // Case 5: −26 / −20 / −26 dBFS for 20 / 20.1 / 20 s: I −23 ±0.1.
    {
        Meter m(2, true);
        feed(m, {{1000.0, -26.0, 20.0}, {1000.0, -20.0, 20.1}, {1000.0, -26.0, 20.0}});
        CHECK(near(m.values().loudness[0].integrated, -23.0, 0.1));
    }
    // Case 9: 1.34 s at −20 dBFS, 1.66 s at −30 dBFS, repeated: S constant −23 ±0.1 after 3 s.
    {
        Meter m(2, true);
        std::vector<Seg> segs;
        for (int k = 0; k < 10; ++k)
        {
            segs.push_back({1000.0, -20.0, 1.34});
            segs.push_back({1000.0, -30.0, 1.66});
        }
        bool ok = true;
        int checked = 0;
        feed(m, segs, 960, 960'000'000ull, [&](MeterValues const& v, double t) {
            if (t > 3.05)
            {
                ok = ok && near(v.loudness[0].shortTerm, -23.0, 0.1);
                ++checked;
            }
        });
        CHECK(ok && checked > 200);
    }
    // Momentary: 0.18 s at −20 dBFS, 0.22 s at −30 dBFS, repeated: M constant −23 ±0.1 after 0.4 s.
    {
        Meter m(2, true);
        std::vector<Seg> segs;
        for (int k = 0; k < 25; ++k)
        {
            segs.push_back({1000.0, -20.0, 0.18});
            segs.push_back({1000.0, -30.0, 0.22});
        }
        bool ok = true;
        int checked = 0;
        feed(m, segs, 0, 960'000'000ull, [&](MeterValues const& v, double t) {
            if (t > 0.45)
            {
                ok = ok && near(v.loudness[0].momentary, -23.0, 0.1);
                ++checked;
            }
        });
        CHECK(ok && checked > 90);
    }
}

TEST(meter_loudness_pairs_reset_and_k_weighting)
{
    // Pair 2 of a 4-channel PGM carries its own programme.
    Meter m(4, true);
    std::vector<float> x(4 * 960);
    std::uint64_t at = 48'000'000;
    for (int g = 0; g < 500; ++g, at += 960)
    {
        for (std::size_t s = 0; s < 960; ++s)
        {
            double const t = static_cast<double>(at + s) / 48000.0;
            float const a = static_cast<float>(std::pow(10.0, -23.0 / 20.0) * std::sin(2.0 * kPi * 1000.0 * t));
            float const b = static_cast<float>(std::pow(10.0, -30.0 / 20.0) * std::sin(2.0 * kPi * 1000.0 * t));
            x[s] = a;
            x[960 + s] = a;
            x[2 * 960 + s] = b;
            x[3 * 960 + s] = b;
        }
        m.add(x.data(), 960, at);
    }
    CHECK(m.values().loudness.size() == 2u && near(m.values().loudness[0].integrated, -23.0, 0.1) && near(m.values().loudness[1].integrated, -30.0, 0.1));
    // A reset: integrated from then on only (20 s at −30, a reset, 20 s at −20).
    Meter r(2, true);
    feed(r, {{1000.0, -30.0, 20.0}}, 960, 4800ull * 1000);
    CHECK(near(r.values().loudness[0].integrated, -30.0, 0.1));
    r.resetLoudness();
    feed(r, {{1000.0, -20.0, 20.0}}, 960, 4800ull * 1000 + 20 * 48000);
    CHECK(near(r.values().loudness[0].integrated, -20.0, 0.1));
    // K-weighting: a 100 Hz sine reads lower than 1 kHz (the high pass), 10 kHz higher (the shelf).
    Meter low(2, true);
    Meter high(2, true);
    feed(low, {{100.0, -23.0, 5.0}});
    feed(high, {{10000.0, -23.0, 5.0}});
    CHECK(low.values().loudness[0].shortTerm < -23.5f && high.values().loudness[0].shortTerm > -22.0f && high.values().loudness[0].shortTerm < -18.f);
}

TEST(meter_true_peak_and_overs)
{
    // Sines whose samples miss their peak: the true peak within +0.2 / −0.4 dB of the amplitude.
    struct Case
    {
        double hz;
        double phase;
        double dbfs;
    };
    for (auto const& c : {Case{12000.0, 45.0, -6.0}, Case{8000.0, 0.0, -6.0}, Case{6000.0, 22.5, -6.0}, Case{12000.0, 45.0, 0.0}, Case{1000.0, 0.0, -20.0},
             Case{997.0, 0.0, -1.0}})
    {
        Meter m(2, true);
        feed(m, {{c.hz, c.dbfs, 1.0, c.phase}});
        float const tp = levelDb(m.values().truePeak1s[0]);
        float const sample = levelDb(m.values().peak1s[0]);
        CHECK(tp >= c.dbfs - 0.4 && tp <= c.dbfs + 0.2);
        CHECK(tp >= sample);
        if (c.hz == 12000.0)
        {
            // fs/4 at 45°: the samples are 3 dB below the peak.
            CHECK(near(sample, c.dbfs - 3.01, 0.05));
        }
    }
    // Overs: samples at or above 0 dBFS.
    Meter loud(2, true);
    feed(loud, {{1000.0, 1.0, 0.5}});
    CHECK(loud.values().overs > 0u);
    Meter fine(2, true);
    feed(fine, {{1000.0, -0.1, 0.5}});
    CHECK_EQ(fine.values().overs, 0ull);
    // A jump in the sample index starts the block again.
    Meter jump(1, false);
    std::vector<float> half(2400, 0.25f);
    CHECK(!jump.add(half.data(), 2400, 0));
    CHECK(!jump.add(half.data(), 2400, 10'000));
    CHECK(jump.add(half.data(), 2400, 12'400) && jump.values().block == 2u && jump.values().rms[0] == 0.25f);
}
