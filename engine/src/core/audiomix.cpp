#include "core/audiomix.hpp"

#include "core/audio.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <utility>

namespace fxeng
{
float dbToGain(float db)
{
    if (!(db > kFaderOffDb))
    {
        return 0.f;
    }
    if (db == 0.f)
    {
        return 1.f;
    }
    return static_cast<float>(std::pow(10.0, static_cast<double>(db) / 20.0));
}

float gainToDb(float gain)
{
    if (!(gain > 0.f))
    {
        return kFaderOffDb;
    }
    auto const db = static_cast<float>(20.0 * std::log10(static_cast<double>(gain)));
    return db < kFaderOffDb ? kFaderOffDb : db;
}

void linearRamp(float g0, float g1, std::size_t n, float* out)
{
    float const d = g1 - g0;
    auto const fn = static_cast<float>(n);
    for (std::size_t s = 0; s < n; ++s)
    {
        out[s] = g0 + d * static_cast<float>(s + 1) / fn;
    }
}

void transitionCurve(float tPrev, float t, std::size_t n, float* a, float* b)
{
    // P4's crossfadeBlock, the gains once per grain (for every channel).
    float const d = t - tPrev;
    auto const fn = static_cast<float>(n);
    for (std::size_t s = 0; s < n; ++s)
    {
        auto const g = crossfade(tPrev + d * static_cast<float>(s + 1) / fn);
        a[s] = g.a;
        b[s] = g.b;
    }
}

void cutCurve(float w0, float w1, std::size_t n, float* out)
{
    if (n == 0)
    {
        return;
    }
    if (w0 == w1)
    {
        std::fill(out, out + n, w1);
        return;
    }
    auto const fn = static_cast<float>(n);
    for (std::size_t s = 0; s + 1 < n; ++s)
    {
        auto const g = crossfade(static_cast<float>(s + 1) / fn);
        out[s] = w1 < w0 ? w1 + (w0 - w1) * g.a : w0 + (w1 - w0) * g.b;
    }
    out[n - 1] = w1;
}

void crossfadeLinear(float const* from, float const* to, std::size_t n, float* out)
{
    auto const fn = static_cast<float>(n);
    for (std::size_t s = 0; s < n; ++s)
    {
        out[s] = from[s] + (to[s] - from[s]) * static_cast<float>(s + 1) / fn;
    }
}

std::optional<StripRef> parseStrip(std::string const& text)
{
    if (text == "stinger")
    {
        return StripRef{StripRef::Kind::Stinger, 0};
    }
    for (auto const& [prefix, kind] : {std::pair{"in", StripRef::Kind::Input}, std::pair{"cp", StripRef::Kind::Player}, std::pair{"me", StripRef::Kind::Me}})
    {
        std::string const p = prefix;
        if (text.size() > p.size() && text.size() <= p.size() + 2 && text.compare(0, p.size(), p) == 0 &&
            text.find_first_not_of("0123456789", p.size()) == std::string::npos && text[p.size()] != '0')
        {
            return StripRef{kind, std::stoi(text.substr(p.size()))};
        }
    }
    return std::nullopt;
}

std::string stripName(StripRef strip)
{
    switch (strip.kind)
    {
    case StripRef::Kind::Input:
        return "in" + std::to_string(strip.index);
    case StripRef::Kind::Player:
        return "cp" + std::to_string(strip.index);
    case StripRef::Kind::Me:
        return "me" + std::to_string(strip.index);
    case StripRef::Kind::Stinger:
        break;
    }
    return "stinger";
}

SampleRange delayedSamples(Rate rate, std::uint64_t index, int latency, std::uint64_t delay)
{
    auto const span = grainSamples(rate, inputIndexFor(index, latency));
    return SampleRange{span.first > delay ? span.first - delay : 0, span.end > delay ? span.end - delay : 0};
}

std::vector<int> channelMap(std::vector<int> const& map, std::size_t pgmChannels, std::size_t inputChannels)
{
    std::vector<int> out(pgmChannels, 0);
    for (std::size_t c = 0; c < pgmChannels; ++c)
    {
        int v = 0;
        if (map.empty())
        {
            v = inputChannels == 1 ? 1 : static_cast<int>(c + 1);
        }
        else if (c < map.size())
        {
            v = map[c];
        }
        out[c] = v >= 1 && static_cast<std::size_t>(v) <= inputChannels ? v : 0;
    }
    return out;
}

// ---- DelayLine ----------------------------------------------------------------------------

void DelayLine::reset(std::size_t channels, std::size_t length)
{
    channels_ = channels;
    length_ = length;
    data_.assign(channels * length, 0.f);
    start_ = 0;
    end_ = 0;
    started_ = false;
}

void DelayLine::begin(std::uint64_t from)
{
    if (!started_ || from > end_ || from < start_)
    {
        start_ = from;
        end_ = from;
        started_ = true;
    }
}

void DelayLine::put(std::size_t channel, std::uint64_t index, float const* data, std::size_t count)
{
    if (channel >= channels_ || length_ == 0)
    {
        return;
    }
    if (count > length_)
    {
        // Only the newest length_ samples stay.
        data += count - length_;
        index += count - length_;
        count = length_;
    }
    float* ring = data_.data() + channel * length_;
    auto const pos = static_cast<std::size_t>(index % length_);
    std::size_t const first = std::min(count, length_ - pos);
    std::memcpy(ring + pos, data, first * sizeof(float));
    std::memcpy(ring, data + first, (count - first) * sizeof(float));
}

void DelayLine::commit(std::uint64_t end)
{
    if (!started_ || end <= end_)
    {
        return;
    }
    end_ = end;
    if (end_ - start_ > length_)
    {
        start_ = end_ - length_;
    }
}

bool DelayLine::holds(std::uint64_t first, std::uint64_t end) const
{
    return started_ && first <= end && first >= start_ && end <= end_;
}

void DelayLine::read(std::size_t channel, std::uint64_t first, std::size_t count, float* out) const
{
    std::uint64_t const last = first + count;
    std::uint64_t const from = std::max(first, start_);
    std::uint64_t const to = std::min(last, end_);
    if (!started_ || channel >= channels_ || from >= to)
    {
        std::fill(out, out + count, 0.f);
        return;
    }
    auto const head = static_cast<std::size_t>(from - first);
    auto const held = static_cast<std::size_t>(to - from);
    std::fill(out, out + head, 0.f);
    std::fill(out + head + held, out + count, 0.f);
    float const* ring = data_.data() + channel * length_;
    auto const pos = static_cast<std::size_t>(from % length_);
    std::size_t const part = std::min(held, length_ - pos);
    std::memcpy(out + head, ring + pos, part * sizeof(float));
    std::memcpy(out + head + part, ring, (held - part) * sizeof(float));
}

void DelayLine::readMapped(std::vector<int> const& map, std::uint64_t first, std::size_t n, float* out) const
{
    for (std::size_t c = 0; c < map.size(); ++c)
    {
        int const source = map[c];
        if (source >= 1 && static_cast<std::size_t>(source) <= channels_)
        {
            read(static_cast<std::size_t>(source - 1), first, n, out + c * n);
        }
        else
        {
            std::fill(out + c * n, out + (c + 1) * n, 0.f);
        }
    }
}

// ---- AudioMix -----------------------------------------------------------------------------

AudioMix::AudioMix(int mes, int inputs, int players, std::size_t channels)
    : mes_(std::max(1, mes))
    , inputs_(std::max(0, inputs))
    , players_(std::max(0, players))
    , channels_(std::max<std::size_t>(1, channels))
    , slots_(static_cast<std::size_t>(inputs_ + players_ + mes_ + 1))
{
    settings_.assign(static_cast<std::size_t>(mes_), MeSettings{0.f, std::vector<StripSettings>(slots_)});
    for (auto& me : settings_)
    {
        // The stinger strip has no AFV: its sound is heard while its stinger runs.
        me.strips.back().afv = false;
    }
    run_.resize(static_cast<std::size_t>(mes_));
    published_.resize(static_cast<std::size_t>(mes_));
    for (std::size_t m = 0; m < run_.size(); ++m)
    {
        auto& r = run_[m];
        r.level.assign(slots_, 1.f);
        r.afv.assign(slots_, 1.f);
        r.weight.assign(slots_, 0.f);
        r.states.assign(slots_, StripState{});
        published_[m].assign(slots_, StripState{});
    }
}

int AudioMix::slot(int me, StripRef strip) const
{
    if (me < 1 || me > mes_)
    {
        return -1;
    }
    switch (strip.kind)
    {
    case StripRef::Kind::Input:
        return strip.index >= 1 && strip.index <= inputs_ ? strip.index - 1 : -1;
    case StripRef::Kind::Player:
        return strip.index >= 1 && strip.index <= players_ ? inputs_ + strip.index - 1 : -1;
    case StripRef::Kind::Me:
        // Only the Programs of higher MEs (re-entry n > m).
        return strip.index > me && strip.index <= mes_ ? inputs_ + players_ + strip.index - 1 : -1;
    case StripRef::Kind::Stinger:
        return static_cast<int>(slots_) - 1;
    }
    return -1;
}

int AudioMix::sourceSlot(int me, Source source) const
{
    switch (source.kind)
    {
    case Source::Kind::Input:
        return slot(me, StripRef{StripRef::Kind::Input, source.index});
    case Source::Kind::Player:
        return slot(me, StripRef{StripRef::Kind::Player, source.index});
    case Source::Kind::Me:
        return slot(me, StripRef{StripRef::Kind::Me, source.index});
    case Source::Kind::Black:
        break;
    }
    return -1;
}

StripRef AudioMix::refOf(int s) const
{
    if (s < inputs_)
    {
        return StripRef{StripRef::Kind::Input, s + 1};
    }
    if (s < inputs_ + players_)
    {
        return StripRef{StripRef::Kind::Player, s - inputs_ + 1};
    }
    if (s < inputs_ + players_ + mes_)
    {
        return StripRef{StripRef::Kind::Me, s - inputs_ - players_ + 1};
    }
    return StripRef{StripRef::Kind::Stinger, 0};
}

AudioMix::Result AudioMix::setMaster(int me, float db)
{
    if (me < 1 || me > mes_)
    {
        return Result::BadMe;
    }
    if (!(db >= kFaderOffDb - 1e-3f && db <= kFaderMaxDb + 1e-3f))
    {
        return Result::BadValue;
    }
    std::lock_guard lock{mu_};
    settings_[static_cast<std::size_t>(me - 1)].masterDb = std::clamp(db, kFaderOffDb, kFaderMaxDb);
    return Result::Ok;
}

AudioMix::Result AudioMix::setStrip(int me, StripRef strip, StripSettings const& settings)
{
    if (me < 1 || me > mes_)
    {
        return Result::BadMe;
    }
    int const s = slot(me, strip);
    if (s < 0)
    {
        return Result::NoStrip;
    }
    if (!(settings.faderDb >= kFaderOffDb - 1e-3f && settings.faderDb <= kFaderMaxDb + 1e-3f))
    {
        return Result::BadValue;
    }
    if (strip.kind == StripRef::Kind::Stinger && settings.afv)
    {
        return Result::AfvOnStinger;
    }
    std::lock_guard lock{mu_};
    auto& target = settings_[static_cast<std::size_t>(me - 1)].strips[static_cast<std::size_t>(s)];
    target = settings;
    target.faderDb = std::clamp(settings.faderDb, kFaderOffDb, kFaderMaxDb);
    return Result::Ok;
}

bool AudioMix::hasStrip(int me, StripRef strip) const
{
    return slot(me, strip) >= 0;
}

float AudioMix::master(int me) const
{
    if (me < 1 || me > mes_)
    {
        return 0.f;
    }
    std::lock_guard lock{mu_};
    return settings_[static_cast<std::size_t>(me - 1)].masterDb;
}

std::optional<StripSettings> AudioMix::strip(int me, StripRef strip) const
{
    int const s = slot(me, strip);
    if (s < 0)
    {
        return std::nullopt;
    }
    std::lock_guard lock{mu_};
    return settings_[static_cast<std::size_t>(me - 1)].strips[static_cast<std::size_t>(s)];
}

std::vector<StripRef> AudioMix::strips(int me) const
{
    std::vector<StripRef> out;
    for (int s = 0; s < static_cast<int>(slots_); ++s)
    {
        auto const ref = refOf(s);
        if (slot(me, ref) == s)
        {
            out.push_back(ref);
        }
    }
    return out;
}

std::vector<StripState> AudioMix::stripStates(int me) const
{
    std::vector<StripState> out;
    if (me < 1 || me > mes_)
    {
        return out;
    }
    std::lock_guard lock{stateMu_};
    auto const& states = published_[static_cast<std::size_t>(me - 1)];
    for (int s = 0; s < static_cast<int>(slots_); ++s)
    {
        if (slot(me, refOf(s)) == s)
        {
            out.push_back(states[static_cast<std::size_t>(s)]);
        }
    }
    return out;
}

std::vector<bool> AudioMix::heardInputs(std::vector<MePlan> const& plans) const
{
    std::vector<bool> out(static_cast<std::size_t>(inputs_), false);
    std::lock_guard lock{mu_};
    for (auto const& plan : plans)
    {
        if (plan.me < 1 || plan.me > mes_)
        {
            continue;
        }
        auto const& set = settings_[static_cast<std::size_t>(plan.me - 1)];
        auto const& run = run_[static_cast<std::size_t>(plan.me - 1)];
        bool const transition = plan.mixing || plan.stinger > 0;
        for (int k = 0; k < inputs_; ++k)
        {
            auto const& strip = set.strips[static_cast<std::size_t>(k)];
            Source const source{Source::Kind::Input, k + 1};
            bool const level = !strip.mute && strip.faderDb > kFaderOffDb;
            bool const onAir = plan.a == source || (transition && plan.b == source);
            if ((level && (!strip.afv || onAir)) || run.states[static_cast<std::size_t>(k)].gain > 0.f)
            {
                out[static_cast<std::size_t>(k)] = true;
            }
        }
    }
    return out;
}

void AudioMix::grain(std::vector<MePlan> const& plans, MixSources const& sources, std::size_t n, std::vector<std::vector<float>>& out)
{
    std::vector<MeSettings> targets;
    {
        std::lock_guard lock{mu_};
        targets = settings_;
    }
    std::size_t const ch = channels_;
    out.resize(static_cast<std::size_t>(mes_));
    for (auto* buffer : {&curveA_, &curveB_, &ramp_, &blend_, &weight_, &gain_})
    {
        buffer->resize(n);
    }
    if (zeros_.size() != ch * n)
    {
        zeros_.assign(ch * n, 0.f);
    }
    for (auto const& plan : plans)
    {
        if (plan.me < 1 || plan.me > mes_)
        {
            continue;
        }
        int const m = plan.me;
        auto& run = run_[static_cast<std::size_t>(m - 1)];
        auto const& set = targets[static_cast<std::size_t>(m - 1)];
        auto& dst = out[static_cast<std::size_t>(m - 1)];
        dst.resize(ch * n);
        // A Mix, Wipe or T-bar, or a stinger (its background crossfade): A and B on P4's curve.
        bool const transition = plan.mixing || plan.stinger > 0;
        int const slotA = sourceSlot(m, plan.a);
        int const slotB = transition ? sourceSlot(m, plan.b) : -1;
        if (transition)
        {
            transitionCurve(plan.tPrev, plan.t, n, curveA_.data(), curveB_.data());
        }
        if (!run.primed)
        {
            // The first grain starts where its settings and plan are: no ramp, no fade in.
            run.primed = true;
            run.master = dbToGain(set.masterDb);
            for (std::size_t s = 0; s < slots_; ++s)
            {
                auto const& strip = set.strips[s];
                run.level[s] = strip.mute ? 0.f : dbToGain(strip.faderDb);
                run.afv[s] = strip.afv ? 1.f : 0.f;
                run.weight[s] = 0.f;
            }
            if (slotA >= 0)
            {
                run.weight[static_cast<std::size_t>(slotA)] = transition ? crossfade(plan.tPrev).a : 1.f;
            }
            if (slotB >= 0)
            {
                run.weight[static_cast<std::size_t>(slotB)] += crossfade(plan.tPrev).b;
            }
        }
        bool first = true;
        // dst = x · g for the first term, dst += x · g for the others (per sample g, or gc).
        auto const add = [&](float const* x, float const* g, float gc) {
            for (std::size_t c = 0; c < ch; ++c)
            {
                float* d = dst.data() + c * n;
                float const* xs = x + c * n;
                if (first && g != nullptr)
                {
                    for (std::size_t s = 0; s < n; ++s)
                    {
                        d[s] = xs[s] * g[s];
                    }
                }
                else if (first)
                {
                    for (std::size_t s = 0; s < n; ++s)
                    {
                        d[s] = xs[s] * gc;
                    }
                }
                else if (g != nullptr)
                {
                    for (std::size_t s = 0; s < n; ++s)
                    {
                        d[s] += xs[s] * g[s];
                    }
                }
                else
                {
                    for (std::size_t s = 0; s < n; ++s)
                    {
                        d[s] += xs[s] * gc;
                    }
                }
            }
            first = false;
        };
        auto const sourceOf = [&](std::size_t s) -> float const* {
            auto const ref = refOf(static_cast<int>(s));
            auto const k = static_cast<std::size_t>(ref.index - 1);
            switch (ref.kind)
            {
            case StripRef::Kind::Input:
                return k < sources.inputs.size() ? sources.inputs[k] : nullptr;
            case StripRef::Kind::Player:
                return k < sources.players.size() ? sources.players[k] : nullptr;
            case StripRef::Kind::Me:
                // ME n's PGM of this grain (it mixed before ME m).
                return out[k].size() == ch * n ? out[k].data() : nullptr;
            case StripRef::Kind::Stinger:
                break;
            }
            return nullptr;
        };
        // A strip's gain over the grain: level (fader, mute) × its AFV blend of `weight` (wc when
        // weight is null). Per sample into gain_ (returned), or a constant into gc (null returned).
        auto const stripGain = [&](std::size_t s, float const* weight, float wc, float& gc) -> float const* {
            auto const& strip = set.strips[s];
            float const l0 = run.level[s];
            float const l1 = strip.mute ? 0.f : dbToGain(strip.faderDb);
            float const a0 = run.afv[s];
            float const a1 = strip.afv ? 1.f : 0.f;
            if (l0 == l1 && a0 == a1 && weight == nullptr)
            {
                gc = l1 * (a1 == 1.f ? wc : a1 == 0.f ? 1.f : a1 * wc + (1.f - a1));
                return nullptr;
            }
            if (l0 != l1)
            {
                linearRamp(l0, l1, n, ramp_.data());
            }
            if (a0 != a1)
            {
                linearRamp(a0, a1, n, blend_.data());
            }
            for (std::size_t i = 0; i < n; ++i)
            {
                float const l = l0 != l1 ? ramp_[i] : l1;
                float const w = weight != nullptr ? weight[i] : wc;
                float const a = a0 != a1 ? blend_[i] : a1;
                gain_[i] = l * (a == 1.f ? w : a == 0.f ? 1.f : a * w + (1.f - a));
            }
            return gain_.data();
        };
        auto const finish = [&](std::size_t s, float wEnd) {
            auto const& strip = set.strips[s];
            run.level[s] = strip.mute ? 0.f : dbToGain(strip.faderDb);
            run.afv[s] = strip.afv ? 1.f : 0.f;
            run.weight[s] = wEnd;
            float const gain = run.level[s] * (run.afv[s] == 1.f ? wEnd : 1.f);
            run.states[s] = StripState{gain, gain > 0.f};
        };
        // 1. A transition's A and B, always both (P4 sums a · cos + b · sin; Black is silence).
        if (transition)
        {
            for (int role = 0; role < 2; ++role)
            {
                int const s = role == 0 ? slotA : slotB;
                float const* curve = role == 0 ? curveA_.data() : curveB_.data();
                float const* x = s >= 0 ? sourceOf(static_cast<std::size_t>(s)) : nullptr;
                float gc = 0.f;
                float const* g = s >= 0 ? stripGain(static_cast<std::size_t>(s), curve, 0.f, gc) : curve;
                add(x != nullptr ? x : zeros_.data(), g, gc);
            }
            auto const ends = crossfade(plan.t);
            if (slotA >= 0)
            {
                finish(static_cast<std::size_t>(slotA), slotA == slotB ? ends.a + ends.b : ends.a);
            }
            if (slotB >= 0 && slotB != slotA)
            {
                finish(static_cast<std::size_t>(slotB), ends.b);
            }
        }
        // 2. Every other strip: AFV off, or AFV on with a weight that moves (a Cut) or stays.
        for (std::size_t s = 0; s + 1 < slots_; ++s)
        {
            if (slot(m, refOf(static_cast<int>(s))) != static_cast<int>(s) ||
                (transition && (static_cast<int>(s) == slotA || static_cast<int>(s) == slotB)))
            {
                continue;
            }
            float const w0 = run.weight[s];
            // Without a transition Program's strip goes to 1 and every other to 0 (a Cut: the
            // equal-power curve over this grain); in a transition the others fade out.
            float const w1 = !transition && static_cast<int>(s) == slotA ? 1.f : 0.f;
            float const* weight = nullptr;
            if (w0 != w1)
            {
                cutCurve(w0, w1, n, weight_.data());
                weight = weight_.data();
            }
            float gc = 0.f;
            float const* g = stripGain(s, weight, w1, gc);
            float const* x = sourceOf(s);
            finish(s, w1);
            if (x == nullptr || (g == nullptr && gc == 0.f))
            {
                continue;
            }
            add(x, g, gc);
        }
        if (first)
        {
            // Nothing heard: silence (P4 copied a silent source).
            std::fill(dst.begin(), dst.end(), 0.f);
            first = false;
        }
        // 3. The stinger's own sound, scaled by the stinger strip (no AFV).
        {
            std::size_t const s = slots_ - 1;
            auto const& strip = set.strips[s];
            float const l0 = run.level[s];
            float const l1 = strip.mute ? 0.f : dbToGain(strip.faderDb);
            auto const k = static_cast<std::size_t>(plan.stinger - 1);
            float const* sting = plan.stinger > 0 && k < sources.stingers.size() ? sources.stingers[k] : nullptr;
            if (sting != nullptr && (l0 != 0.f || l1 != 0.f))
            {
                if (l0 != l1)
                {
                    linearRamp(l0, l1, n, ramp_.data());
                }
                add(sting, l0 != l1 ? ramp_.data() : nullptr, l1);
            }
            run.level[s] = l1;
            run.afv[s] = 0.f;
            run.states[s] = StripState{l1, sting != nullptr && l1 > 0.f};
        }
        // 4. The master, then Fade to Black (P4's ramp within the grain), over the whole sound.
        float const m0 = run.master;
        float const m1 = dbToGain(set.masterDb);
        if (m0 != 1.f || m1 != 1.f)
        {
            if (m0 != m1)
            {
                linearRamp(m0, m1, n, ramp_.data());
            }
            for (std::size_t c = 0; c < ch; ++c)
            {
                float* d = dst.data() + c * n;
                for (std::size_t s = 0; s < n; ++s)
                {
                    d[s] *= m0 != m1 ? ramp_[s] : m1;
                }
            }
        }
        run.master = m1;
        if (plan.ftb > 0.f || plan.ftbPrev > 0.f)
        {
            linearRamp(plan.ftbPrev, plan.ftb, n, ramp_.data());
            for (std::size_t c = 0; c < ch; ++c)
            {
                float* d = dst.data() + c * n;
                for (std::size_t s = 0; s < n; ++s)
                {
                    d[s] *= 1.f - ramp_[s];
                }
            }
        }
    }
    std::lock_guard lock{stateMu_};
    for (std::size_t m = 0; m < run_.size(); ++m)
    {
        published_[m] = run_[m].states;
    }
}
} // namespace fxeng
