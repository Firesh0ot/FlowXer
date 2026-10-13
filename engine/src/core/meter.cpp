#include "core/meter.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace fxeng
{
namespace
{
// K-weighting at 48 kHz (BS.1770-4): a high shelf, then the RLB high pass.
constexpr double kB1[3] = {1.53512485958697, -2.69169618940638, 1.19839281085285};
constexpr double kA1[2] = {-1.69065929318241, 0.73248077421585};
constexpr double kB2[3] = {1.0, -2.0, 1.0};
constexpr double kA2[2] = {-1.99004745483398, 0.99007225036621};

// BS.1770-4 annex 2: the 48-tap interpolation filter as 4 phases of 12 taps.
constexpr float kTp[4][12] = {
    {0.0017089843750f, 0.0109863281250f, -0.0196533203125f, 0.0332031250000f, -0.0594482421875f, 0.1373291015625f, 0.9721679687500f,
        -0.1022949218750f, 0.0476074218750f, -0.0266113281250f, 0.0148925781250f, -0.0083007812500f},
    {-0.0291748046875f, 0.0292968750000f, -0.0517578125000f, 0.0891113281250f, -0.1665039062500f, 0.4650878906250f, 0.7797851562500f,
        -0.2003173828125f, 0.1015625000000f, -0.0582275390625f, 0.0330810546875f, -0.0189208984375f},
    {-0.0189208984375f, 0.0330810546875f, -0.0582275390625f, 0.1015625000000f, -0.2003173828125f, 0.7797851562500f, 0.4650878906250f,
        -0.1665039062500f, 0.0891113281250f, -0.0517578125000f, 0.0292968750000f, -0.0291748046875f},
    {-0.0083007812500f, 0.0148925781250f, -0.0266113281250f, 0.0476074218750f, -0.1022949218750f, 0.9721679687500f, 0.1373291015625f,
        -0.0594482421875f, 0.0332031250000f, -0.0196533203125f, 0.0109863281250f, 0.0017089843750f}};

// The integrated histogram: 0.1 LU bins from −70 to +10 LUFS (louder blocks in the last bin).
constexpr int kBins = 800;
constexpr double kGateAbsolute = -70.0;

double loudness(double meanSquare)
{
    return -0.691 + 10.0 * std::log10(meanSquare);
}
} // namespace

float levelDb(double v)
{
    if (!(v > 0.0))
    {
        return kMeterFloorDb;
    }
    return std::max(kMeterFloorDb, static_cast<float>(20.0 * std::log10(v)));
}

float powerDb(double p)
{
    if (!(p > 0.0))
    {
        return kMeterFloorDb;
    }
    return std::max(kMeterFloorDb, static_cast<float>(10.0 * std::log10(p)));
}

Meter::Meter(std::size_t channels, bool program)
    : channels_(std::max<std::size_t>(1, channels))
    , program_(program)
    , ch_(channels_)
{
    if (program_)
    {
        pairs_.resize((channels_ + 1) / 2);
        for (auto& pair : pairs_)
        {
            pair.power.assign(kBins, 0.0);
            pair.count.assign(kBins, 0);
        }
    }
    lastPeak_.assign(channels_, {});
    lastMs_.assign(channels_, {});
    lastTp_.assign(channels_, {});
    values_.peak.assign(channels_, 0.f);
    values_.rms.assign(channels_, 0.f);
    values_.peak1s.assign(channels_, 0.f);
    values_.rms1s.assign(channels_, 0.f);
    if (program_)
    {
        values_.truePeak.assign(channels_, 0.f);
        values_.truePeak1s.assign(channels_, 0.f);
        values_.loudness.assign(pairs_.size(), PairLoudness{});
    }
}

void Meter::resetLoudness()
{
    reset_ = true;
}

void Meter::run(std::size_t c, float const* x, std::size_t m)
{
    auto& cm = ch_[c];
    // Peak and squares in 8 lanes (they vectorise), then into the block's double sum.
    float pk[8] = {};
    float sq[8] = {};
    std::size_t s = 0;
    for (; s + 8 <= m; s += 8)
    {
        for (std::size_t l = 0; l < 8; ++l)
        {
            float const v = x[s + l];
            float const a = std::fabs(v);
            pk[l] = pk[l] > a ? pk[l] : a;
            sq[l] += v * v;
        }
    }
    double sum = 0.0;
    float peak = cm.peak;
    for (; s < m; ++s)
    {
        peak = std::max(peak, std::fabs(x[s]));
        sum += static_cast<double>(x[s]) * static_cast<double>(x[s]);
    }
    for (std::size_t l = 0; l < 8; ++l)
    {
        peak = std::max(peak, pk[l]);
        sum += static_cast<double>(sq[l]);
    }
    cm.peak = peak;
    cm.sum += sum;
    if (!program_)
    {
        return;
    }
    if (peak >= 1.f)
    {
        for (std::size_t i = 0; i < m; ++i)
        {
            overs_ += std::fabs(x[i]) >= 1.f ? 1 : 0;
        }
    }
    // K-weighting (transposed direct form II), serial per channel.
    double z0 = cm.z[0];
    double z1 = cm.z[1];
    double z2 = cm.z[2];
    double z3 = cm.z[3];
    double ks = 0.0;
    for (std::size_t i = 0; i < m; ++i)
    {
        double const in = x[i];
        double const y1 = kB1[0] * in + z0;
        z0 = kB1[1] * in - kA1[0] * y1 + z1;
        z1 = kB1[2] * in - kA1[1] * y1;
        double const y2 = kB2[0] * y1 + z2;
        z2 = kB2[1] * y1 - kA2[0] * y2 + z3;
        z3 = kB2[2] * y1 - kA2[1] * y2;
        ks += y2 * y2;
    }
    cm.z = {z0, z1, z2, z3};
    cm.ksum += ks;
    // True peak: the 4 phases of the interpolation filter as block filters (one pass per tap).
    scratch_.resize(m + 11);
    acc_.resize(m);
    std::memcpy(scratch_.data(), cm.history.data(), 11 * sizeof(float));
    std::memcpy(scratch_.data() + 11, x, m * sizeof(float));
    float tl[8] = {};
    float tp = cm.truePeak;
    for (auto const& phase : kTp)
    {
        std::fill(acc_.begin(), acc_.begin() + static_cast<long>(m), 0.f);
        for (int tap = 0; tap < 12; ++tap)
        {
            float const cf = phase[tap];
            float const* b = scratch_.data() + 11 - tap;
            float* a = acc_.data();
            for (std::size_t i = 0; i < m; ++i)
            {
                a[i] += cf * b[i];
            }
        }
        std::size_t i = 0;
        for (; i + 8 <= m; i += 8)
        {
            for (std::size_t l = 0; l < 8; ++l)
            {
                float const v = std::fabs(acc_[i + l]);
                tl[l] = tl[l] > v ? tl[l] : v;
            }
        }
        for (; i < m; ++i)
        {
            tp = std::max(tp, std::fabs(acc_[i]));
        }
    }
    for (float v : tl)
    {
        tp = std::max(tp, v);
    }
    // The true peak is never below the sample peak.
    cm.truePeak = std::max(tp, peak);
    std::memcpy(cm.history.data(), scratch_.data() + m, 11 * sizeof(float));
}

bool Meter::add(float const* x, std::size_t n, std::uint64_t first)
{
    if (!started_ || first != next_)
    {
        // The start, or a jump in the sample index: a new block from here.
        started_ = true;
        filled_ = 0;
        for (auto& cm : ch_)
        {
            cm = Channel{};
        }
    }
    static std::vector<float> const zeros(4096, 0.f);
    bool finished = false;
    std::size_t done = 0;
    while (done < n)
    {
        auto const pos = static_cast<std::size_t>((first + done) % kMeterBlock);
        std::size_t const m = std::min(n - done, kMeterBlock - pos);
        for (std::size_t c = 0; c < channels_; ++c)
        {
            if (x != nullptr)
            {
                run(c, x + c * n + done, m);
            }
            else
            {
                for (std::size_t k = 0; k < m; k += zeros.size())
                {
                    run(c, zeros.data(), std::min(zeros.size(), m - k));
                }
            }
        }
        filled_ += m;
        done += m;
        if ((first + done) % kMeterBlock == 0)
        {
            values_.block = (first + done) / kMeterBlock - 1;
            endBlock();
            finished = true;
        }
    }
    next_ = first + n;
    return finished;
}

void Meter::endBlock()
{
    double const count = static_cast<double>(std::max<std::size_t>(1, filled_));
    std::size_t const slot = static_cast<std::size_t>(blocks_ % 10);
    for (std::size_t c = 0; c < channels_; ++c)
    {
        auto& cm = ch_[c];
        double const ms = cm.sum / count;
        values_.peak[c] = cm.peak;
        values_.rms[c] = static_cast<float>(std::sqrt(ms));
        lastPeak_[c][slot] = cm.peak;
        lastMs_[c][slot] = ms;
        if (program_)
        {
            values_.truePeak[c] = cm.truePeak;
            lastTp_[c][slot] = cm.truePeak;
        }
        std::size_t const used = static_cast<std::size_t>(std::min<std::uint64_t>(blocks_ + 1, 10));
        float p = 0.f;
        float t = 0.f;
        double sum = 0.0;
        for (std::size_t k = 0; k < used; ++k)
        {
            p = std::max(p, lastPeak_[c][k]);
            t = std::max(t, lastTp_[c][k]);
            sum += lastMs_[c][k];
        }
        values_.peak1s[c] = p;
        values_.rms1s[c] = static_cast<float>(std::sqrt(sum / static_cast<double>(used)));
        if (program_)
        {
            values_.truePeak1s[c] = t;
        }
        cm.peak = 0.f;
        cm.truePeak = 0.f;
        cm.sum = 0.0;
    }
    if (program_)
    {
        for (std::size_t p = 0; p < pairs_.size(); ++p)
        {
            auto& pair = pairs_[p];
            double z = ch_[2 * p].ksum / count;
            if (2 * p + 1 < channels_)
            {
                z += ch_[2 * p + 1].ksum / count;
            }
            pair.blocks[static_cast<std::size_t>(blocks_ % 30)] = z;
            double m4 = 0.0;
            double m30 = 0.0;
            for (std::size_t k = 0; k < 30; ++k)
            {
                m30 += pair.blocks[k];
                if ((blocks_ + 30 - k) % 30 < 4)
                {
                    m4 += pair.blocks[k];
                }
            }
            auto& out = values_.loudness[p];
            out.momentary = powerDb(m4 / 4.0) - 0.691f;
            out.shortTerm = powerDb(m30 / 30.0) - 0.691f;
            out.momentary = std::max(kMeterFloorDb, out.momentary);
            out.shortTerm = std::max(kMeterFloorDb, out.shortTerm);
            if (reset_)
            {
                // From the next block on; this one still holds samples from before.
                std::fill(pair.power.begin(), pair.power.end(), 0.0);
                std::fill(pair.count.begin(), pair.count.end(), 0u);
                pair.since = 0;
            }
            else
            {
                ++pair.since;
            }
            if (pair.since >= 4 && m4 > 0.0)
            {
                // A 400 ms gating block (75 % overlap: one per 100 ms block).
                double const level = loudness(m4 / 4.0);
                if (level > kGateAbsolute)
                {
                    int const bin = std::clamp(static_cast<int>((level - kGateAbsolute) * 10.0), 0, kBins - 1);
                    pair.power[static_cast<std::size_t>(bin)] += m4 / 4.0;
                    ++pair.count[static_cast<std::size_t>(bin)];
                }
            }
            // Integrated: the blocks above −70 LUFS, then those above their loudness − 10 LU.
            double power = 0.0;
            double blocks = 0.0;
            for (int b = 0; b < kBins; ++b)
            {
                power += pair.power[static_cast<std::size_t>(b)];
                blocks += pair.count[static_cast<std::size_t>(b)];
            }
            out.integrated = kMeterFloorDb;
            if (blocks > 0.0)
            {
                double const gate = loudness(power / blocks) - 10.0;
                power = 0.0;
                blocks = 0.0;
                for (int b = 0; b < kBins; ++b)
                {
                    if (kGateAbsolute + (static_cast<double>(b) + 0.5) / 10.0 > gate)
                    {
                        power += pair.power[static_cast<std::size_t>(b)];
                        blocks += pair.count[static_cast<std::size_t>(b)];
                    }
                }
                if (blocks > 0.0)
                {
                    out.integrated = std::max(kMeterFloorDb, static_cast<float>(loudness(power / blocks)));
                }
            }
        }
        reset_ = false;
        values_.overs = overs_;
    }
    for (auto& cm : ch_)
    {
        cm.ksum = 0.0;
    }
    filled_ = 0;
    ++blocks_;
}
} // namespace fxeng
