// P5 audio (docs/engine-p5.md): the strips' gains and ramps, AFV, the Cut crossfade, the master and
// FTB, re-entry and the stinger strip, P4's PGM bit for bit with the defaults, delay lines and
// channel maps.
#include "test.hpp"

#include "core/audio.hpp"
#include "core/audiomix.hpp"
#include "core/mixer.hpp"
#include "core/tai.hpp"

#include <cmath>
#include <cstring>
#include <random>

using namespace fxeng;

namespace
{
Source in(int k)
{
    return Source{Source::Kind::Input, k};
}

Source cp(int k)
{
    return Source{Source::Kind::Player, k};
}

Source me(int k)
{
    return Source{Source::Kind::Me, k};
}

// A plan of ME m without a transition: Program a.
MePlan still(int m, Source a)
{
    MePlan p;
    p.me = m;
    p.a = a;
    p.b = a;
    return p;
}

// A channel-major buffer (ch × n) of one value per channel.
std::vector<float> flat(std::size_t ch, std::size_t n, std::vector<float> const& values)
{
    std::vector<float> out(ch * n);
    for (std::size_t c = 0; c < ch; ++c)
    {
        std::fill(out.begin() + static_cast<long>(c * n), out.begin() + static_cast<long>((c + 1) * n), values[c % values.size()]);
    }
    return out;
}

// P4's PGM audio of one grain (engine.cpp audioGrain before P5): Program's source, the
// equal-power crossfade of a transition or a stinger's background, the stinger's sound added,
// Fade to Black. `in`: per live input then per clip player (ch × n); `sting`: per stinger player.
void p4Mix(std::vector<MePlan> const& plans, std::vector<std::vector<float>> const& in, int inputs, std::vector<float const*> const& sting, std::size_t ch,
    std::size_t n, std::vector<std::vector<float>>& out)
{
    std::vector<float> const silence(ch * n, 0.f);
    out.resize(4);
    auto const sourceOf = [&](Source s) -> std::vector<float> const* {
        if (s.kind == Source::Kind::Me)
        {
            return &out[static_cast<std::size_t>(s.index - 1)];
        }
        if (s.kind == Source::Kind::Input)
        {
            return &in[static_cast<std::size_t>(s.index - 1)];
        }
        if (s.kind == Source::Kind::Player)
        {
            return &in[static_cast<std::size_t>(inputs + s.index - 1)];
        }
        return nullptr;
    };
    for (auto const& plan : plans)
    {
        auto& dst = out[static_cast<std::size_t>(plan.me - 1)];
        auto const* a = sourceOf(plan.a);
        a = a != nullptr && a->size() == ch * n ? a : &silence;
        if (plan.mixing || plan.stinger > 0)
        {
            auto const* b = sourceOf(plan.b);
            b = b != nullptr && b->size() == ch * n ? b : &silence;
            dst.resize(ch * n);
            for (std::size_t c = 0; c < ch; ++c)
            {
                crossfadeBlock(a->data() + c * n, b->data() + c * n, dst.data() + c * n, n, plan.tPrev, plan.t);
            }
        }
        else
        {
            dst = *a;
        }
        if (plan.stinger > 0 && sting[static_cast<std::size_t>(plan.stinger - 1)] != nullptr)
        {
            float const* s = sting[static_cast<std::size_t>(plan.stinger - 1)];
            for (std::size_t i = 0; i < dst.size(); ++i)
            {
                dst[i] += s[i];
            }
        }
        if (plan.ftb > 0.f || plan.ftbPrev > 0.f)
        {
            for (std::size_t c = 0; c < ch; ++c)
            {
                for (std::size_t s = 0; s < n; ++s)
                {
                    float const g = plan.ftbPrev + (plan.ftb - plan.ftbPrev) * static_cast<float>(s + 1) / static_cast<float>(n);
                    dst[c * n + s] *= 1.f - g;
                }
            }
        }
    }
}

bool sameBits(std::vector<float> const& a, std::vector<float> const& b)
{
    return a.size() == b.size() && std::memcmp(a.data(), b.data(), a.size() * sizeof(float)) == 0;
}
} // namespace

TEST(audio_gains_and_ramps)
{
    CHECK(dbToGain(0.f) == 1.f && dbToGain(-90.f) == 0.f && dbToGain(-120.f) == 0.f);
    CHECK(std::abs(dbToGain(-6.0206f) - 0.5f) < 1e-5f && std::abs(dbToGain(6.0206f) - 2.f) < 1e-4f);
    CHECK(std::abs(dbToGain(10.f) - 3.1622777f) < 1e-5f);
    CHECK(gainToDb(0.f) == -90.f && gainToDb(1.f) == 0.f && std::abs(gainToDb(0.5f) + 6.0206f) < 1e-3f);
    // g0 + (g1 − g0) · (s + 1) / n, exactly g1 at the last sample of a whole-number step.
    std::vector<float> r(960);
    linearRamp(1.f, 0.5f, r.size(), r.data());
    for (std::size_t s = 0; s < r.size(); ++s)
    {
        CHECK(r[s] == 1.f + (0.5f - 1.f) * static_cast<float>(s + 1) / 960.f);
    }
    CHECK(r.back() == 0.5f && r.front() < 1.f);
    // The Cut curve: equal power, ending exactly where it goes.
    std::vector<float> down(960);
    std::vector<float> up(960);
    cutCurve(1.f, 0.f, down.size(), down.data());
    cutCurve(0.f, 1.f, up.size(), up.data());
    CHECK(down.back() == 0.f && up.back() == 1.f);
    for (std::size_t s = 0; s < down.size(); ++s)
    {
        auto const g = crossfade(static_cast<float>(s + 1) / 960.f);
        CHECK(s + 1 == down.size() || (down[s] == g.a && up[s] == g.b));
        CHECK(std::abs(down[s] * down[s] + up[s] * up[s] - 1.f) < 1e-5f);
    }
    std::vector<float> flatCurve(10);
    cutCurve(0.25f, 0.25f, flatCurve.size(), flatCurve.data());
    CHECK(flatCurve.front() == 0.25f && flatCurve.back() == 0.25f);
    // A transition's curve is P4's crossfadeBlock.
    std::vector<float> a(960);
    std::vector<float> b(960);
    std::vector<float> ones(960, 1.f);
    std::vector<float> zeros(960, 0.f);
    std::vector<float> ref(960);
    transitionCurve(0.25f, 0.5f, a.size(), a.data(), b.data());
    crossfadeBlock(ones.data(), zeros.data(), ref.data(), ref.size(), 0.25f, 0.5f);
    CHECK(sameBits(a, ref));
    crossfadeBlock(zeros.data(), ones.data(), ref.data(), ref.size(), 0.25f, 0.5f);
    CHECK(sameBits(b, ref));
    // A linear crossfade (delay and map changes).
    std::vector<float> x(4, 1.f);
    std::vector<float> y(4, 3.f);
    std::vector<float> z(4);
    crossfadeLinear(x.data(), y.data(), 4, z.data());
    CHECK(z[0] == 1.5f && z[1] == 2.f && z[2] == 2.5f && z[3] == 3.f);
}

TEST(audio_strip_names_and_settings)
{
    CHECK(parseStrip("in3") && parseStrip("in3")->kind == StripRef::Kind::Input && parseStrip("in3")->index == 3);
    CHECK(parseStrip("cp1") && parseStrip("cp1")->kind == StripRef::Kind::Player);
    CHECK(parseStrip("me4") && parseStrip("me4")->kind == StripRef::Kind::Me && parseStrip("me4")->index == 4);
    CHECK(parseStrip("stinger") && parseStrip("stinger")->kind == StripRef::Kind::Stinger);
    CHECK(!parseStrip("in0") && !parseStrip("in") && !parseStrip("x1") && !parseStrip("in123") && !parseStrip("black"));
    CHECK(stripName(StripRef{StripRef::Kind::Player, 2}) == "cp2" && stripName(StripRef{StripRef::Kind::Stinger, 0}) == "stinger");
    // 3 MEs, 2 inputs, 1 player: ME 1 hears in1, in2, cp1, me2, me3, stinger; ME 3 no re-entry.
    AudioMix mix(3, 2, 1, 2);
    CHECK(mix.strips(1).size() == 6u && mix.strips(2).size() == 5u && mix.strips(3).size() == 4u);
    CHECK(mix.strips(1)[3] == (StripRef{StripRef::Kind::Me, 2}) && mix.strips(1).back() == (StripRef{StripRef::Kind::Stinger, 0}));
    CHECK(mix.hasStrip(1, StripRef{StripRef::Kind::Me, 3}) && !mix.hasStrip(3, StripRef{StripRef::Kind::Me, 3}) && !mix.hasStrip(2, StripRef{StripRef::Kind::Me, 1}));
    CHECK(mix.setStrip(2, StripRef{StripRef::Kind::Me, 2}, {}) == AudioMix::Result::NoStrip);
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 3}, {}) == AudioMix::Result::NoStrip);
    CHECK(mix.setStrip(5, StripRef{StripRef::Kind::Input, 1}, {}) == AudioMix::Result::BadMe);
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 1}, StripSettings{11.f, false, true}) == AudioMix::Result::BadValue);
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Stinger, 0}, StripSettings{-3.f, false, true}) == AudioMix::Result::AfvOnStinger);
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Stinger, 0}, StripSettings{-3.f, false, false}) == AudioMix::Result::Ok);
    CHECK(mix.strip(1, StripRef{StripRef::Kind::Stinger, 0})->faderDb == -3.f);
    CHECK(mix.setMaster(2, -95.f) == AudioMix::Result::BadValue && mix.setMaster(2, -90.f) == AudioMix::Result::Ok && mix.master(2) == -90.f);
    CHECK(mix.strip(1, StripRef{StripRef::Kind::Input, 1})->afv && mix.strip(1, StripRef{StripRef::Kind::Input, 1})->faderDb == 0.f);
}

TEST(audio_defaults_are_p4_bit_for_bit)
{
    // 2 MEs, 3 inputs, 1 clip player, 1 stinger player; random sound (with negative zeros) every
    // grain; Mixes, Wipes, the T-bar, a stinger, re-entry and Fade to Black. Every grain equals
    // P4's mix bit for bit, except a Cut's grain (P5 crossfades it).
    std::size_t const ch = 2;
    std::size_t const n = 960;
    Mixer mixer(2, 3, 1);
    AudioMix mix(2, 3, 1, ch);
    std::mt19937 rng(5);
    std::uniform_real_distribution<float> noise(-1.f, 1.f);
    std::vector<std::vector<float>> sound(4, std::vector<float>(ch * n));
    std::vector<float> sting(ch * n);
    std::vector<std::vector<float>> p4;
    std::vector<std::vector<float>> p5;
    std::vector<bool> const ready{true};
    std::uint64_t index = 1000;
    int compared = 0;
    int cuts = 0;
    int differ = 0;
    Source lastA = in(1);
    bool lastTransition = false;
    Source lastB = in(1);
    float lastT = 0.f;
    auto const run = [&](int grains) {
        for (int g = 0; g < grains; ++g, ++index)
        {
            for (auto& buffer : sound)
            {
                for (auto& v : buffer)
                {
                    v = noise(rng);
                }
                buffer[3] = -0.f;
            }
            for (auto& v : sting)
            {
                v = noise(rng) * 0.25f;
            }
            auto const plans = mixer.frame(index, ready).mes;
            MixSources sources;
            for (std::size_t k = 0; k < 3; ++k)
            {
                sources.inputs.push_back(sound[k].data());
            }
            sources.players.push_back(sound[3].data());
            sources.stingers.push_back(sting.data());
            mix.grain(plans, sources, n, p5);
            p4Mix(plans, sound, 3, {sting.data()}, ch, n, p4);
            for (auto const& plan : plans)
            {
                if (plan.me != 1)
                {
                    CHECK(sameBits(p4[1], p5[1]));
                    continue;
                }
                bool const transition = plan.mixing || plan.stinger > 0;
                // P4 switched hard here: Program changed outside a transition (not the end of one).
                bool const cut = !transition && plan.a != lastA && !(lastTransition && lastT >= 1.f && lastB == plan.a);
                if (cut)
                {
                    ++cuts;
                }
                else
                {
                    ++compared;
                    differ += sameBits(p4[0], p5[0]) ? 0 : 1;
                }
                lastA = plan.a;
                lastB = plan.b;
                lastTransition = transition;
                lastT = plan.t;
            }
        }
    };
    mixer.setProgram(2, in(3));
    mixer.setProgram(1, in(1));
    mixer.setPreview(1, in(2));
    run(3);
    CHECK(mixer.take(1, in(2), 6, std::nullopt) == MixResult::Ok);
    run(8);
    TransitionSettings wipe;
    wipe.type = Transition::Wipe;
    wipe.frames = 4;
    CHECK(mixer.setTransition(1, wipe) == MixResult::Ok);
    CHECK(mixer.take(1, me(2), 4, std::nullopt, Transition::Wipe) == MixResult::Ok);
    run(6);
    CHECK(mixer.setPreview(1, cp(1)) == MixResult::Ok);
    for (float t : {0.3f, 0.6f, 0.6f, 1.f})
    {
        CHECK(mixer.position(1, t) == MixResult::Ok);
        run(1);
    }
    run(2);
    CHECK(mixer.take(1, in(1), 0, std::nullopt, Transition::Stinger, Sting{1, 6, 3}) == MixResult::Ok);
    run(9);
    CHECK(mixer.ftbAuto(1, 5) == MixResult::Ok);
    run(7);
    CHECK(mixer.ftbAuto(1, 3) == MixResult::Ok);
    CHECK(mixer.take(2, cp(1), 5, std::nullopt) == MixResult::Ok);
    run(7);
    // Mix to Black and back (Black is silence in the crossfade).
    CHECK(mixer.take(1, Source{}, 3, std::nullopt) == MixResult::Ok);
    run(5);
    CHECK(mixer.take(1, in(3), 4, std::nullopt) == MixResult::Ok);
    run(6);
    // A Cut: P5 crossfades its grain; the grain after it is P4's again.
    CHECK(mixer.take(1, in(2), 0, std::nullopt, Transition::Cut) == MixResult::Ok);
    run(3);
    CHECK_EQ(compared, 59);
    CHECK_EQ(cuts, 1);
    CHECK_EQ(differ, 0);
}

TEST(audio_cut_crossfades_over_its_grain)
{
    std::size_t const ch = 2;
    std::size_t const n = 960;
    AudioMix mix(1, 2, 0, ch);
    auto const a = flat(ch, n, {1.f, -1.f});
    auto const b = flat(ch, n, {0.5f, 0.25f});
    MixSources sources{{a.data(), b.data()}, {}, {}};
    std::vector<std::vector<float>> out;
    mix.grain({still(1, in(1))}, sources, n, out);
    CHECK(out[0][0] == 1.f && out[0][n] == -1.f);
    mix.grain({still(1, in(2))}, sources, n, out);
    std::vector<float> down(n);
    std::vector<float> up(n);
    cutCurve(1.f, 0.f, n, down.data());
    cutCurve(0.f, 1.f, n, up.data());
    bool exact = true;
    for (std::size_t s = 0; s < n; ++s)
    {
        // Slot order: in1 then in2.
        exact = exact && out[0][s] == 1.f * down[s] + 0.5f * up[s] && out[0][n + s] == -1.f * down[s] + 0.25f * up[s];
    }
    CHECK(exact);
    CHECK(out[0][n - 1] == 0.5f);
    // The next grain is B alone, exactly.
    mix.grain({still(1, in(2))}, sources, n, out);
    CHECK(out[0][0] == 0.5f && out[0][n - 1] == 0.5f && out[0][n] == 0.25f);
    // A Cut to Black fades out on the same curve.
    mix.grain({still(1, Source{})}, sources, n, out);
    CHECK(out[0][0] == 0.5f * down[0] && out[0][n - 1] == 0.f);
    auto const states = mix.stripStates(1);
    CHECK(states.size() == 3u && !states[0].heard && !states[1].heard && states[1].gain == 0.f);
}

TEST(audio_fader_mute_and_afv)
{
    std::size_t const ch = 1;
    std::size_t const n = 960;
    AudioMix mix(1, 2, 0, ch);
    auto const a = flat(ch, n, {0.8f});
    auto const b = flat(ch, n, {0.1f});
    MixSources sources{{a.data(), b.data()}, {}, {}};
    std::vector<std::vector<float>> out;
    mix.grain({still(1, in(1))}, sources, n, out);
    // −6.0206 dB on Program's strip: one grain of ramp, then x · g exactly.
    float const g = dbToGain(-6.0206f);
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 1}, StripSettings{-6.0206f, false, true}) == AudioMix::Result::Ok);
    mix.grain({still(1, in(1))}, sources, n, out);
    bool ramp = true;
    for (std::size_t s = 0; s < n; ++s)
    {
        float const l = 1.f + (g - 1.f) * static_cast<float>(s + 1) / static_cast<float>(n);
        ramp = ramp && out[0][s] == 0.8f * (l * 1.f);
    }
    CHECK(ramp);
    mix.grain({still(1, in(1))}, sources, n, out);
    CHECK(out[0][0] == 0.8f * g && out[0][n - 1] == 0.8f * g);
    CHECK(std::abs(mix.stripStates(1)[0].gain - g) < 1e-7f && mix.stripStates(1)[0].heard);
    // Mute ramps to silence and keeps the fader.
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 1}, StripSettings{-6.0206f, true, true}) == AudioMix::Result::Ok);
    mix.grain({still(1, in(1))}, sources, n, out);
    CHECK(out[0][0] < 0.8f * g && out[0][0] > 0.79f * g && out[0][n - 1] == 0.f);
    mix.grain({still(1, in(1))}, sources, n, out);
    CHECK(out[0][0] == 0.f && !mix.stripStates(1)[0].heard && mix.strip(1, StripRef{StripRef::Kind::Input, 1})->faderDb == -6.0206f);
    // −90 dB is off.
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 1}, StripSettings{-90.f, false, true}) == AudioMix::Result::Ok);
    mix.grain({still(1, in(1))}, sources, n, out);
    CHECK(out[0][n - 1] == 0.f && mix.stripStates(1)[0].gain == 0.f);
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 1}, StripSettings{}) == AudioMix::Result::Ok);
    mix.grain({still(1, in(1))}, sources, n, out);
    mix.grain({still(1, in(1))}, sources, n, out);
    CHECK(out[0][0] == 0.8f);
    // AFV off on in2: in the mix at its fader whatever is on Program, ramping in over one grain.
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 2}, StripSettings{0.f, false, false}) == AudioMix::Result::Ok);
    mix.grain({still(1, in(1))}, sources, n, out);
    float const a0 = 1.f + (0.f - 1.f) * 1.f / 960.f;
    CHECK(out[0][0] == 0.8f + 0.1f * (1.f * (a0 * 0.f + (1.f - a0))) && out[0][n - 1] == 0.8f + 0.1f);
    mix.grain({still(1, in(1))}, sources, n, out);
    CHECK(out[0][0] == 0.8f + 0.1f && mix.stripStates(1)[1].heard);
    // A transition does not touch an AFV-off strip.
    MePlan p;
    p.me = 1;
    p.a = in(1);
    p.b = Source{};
    p.mixing = true;
    p.tPrev = 0.f;
    p.t = 0.5f;
    mix.grain({p}, sources, n, out);
    auto const half = crossfade(0.5f);
    CHECK(out[0][n - 1] == (0.8f * half.a + 0.f * half.b) + 0.1f);
    // AFV back on for in2 (not on Program): it ramps out linearly.
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 2}, StripSettings{0.f, false, true}) == AudioMix::Result::Ok);
    mix.grain({still(1, in(1))}, sources, n, out);
    CHECK(out[0][n - 1] == 0.8f && !mix.stripStates(1)[1].heard);
}

TEST(audio_master_ftb_reentry_and_stinger)
{
    std::size_t const ch = 2;
    std::size_t const n = 800;
    AudioMix mix(2, 1, 1, ch);
    auto const x = flat(ch, n, {0.5f, 0.25f});
    auto const clip = flat(ch, n, {0.125f, 0.125f});
    auto const sting = flat(ch, n, {0.0625f, 0.0625f});
    MixSources sources{{x.data()}, {clip.data()}, {sting.data()}};
    std::vector<std::vector<float>> out;
    // ME 2 Program the clip; ME 1 Program ME 2 (re-entry: the same grain).
    std::vector<MePlan> plans{still(2, cp(1)), still(1, me(2))};
    mix.grain(plans, sources, n, out);
    CHECK(out[1][0] == 0.125f && out[0][0] == 0.125f && out[0][n] == 0.125f);
    // ME 2's master at −6.0206 dB is heard on ME 1 too.
    CHECK(mix.setMaster(2, -6.0206f) == AudioMix::Result::Ok);
    mix.grain(plans, sources, n, out);
    mix.grain(plans, sources, n, out);
    CHECK(out[1][0] == 0.125f * dbToGain(-6.0206f) && out[0][0] == out[1][0]);
    // The stinger strip scales a stinger's sound (ME 1, stinger player 1, A = me2, B = in1).
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Stinger, 0}, StripSettings{-90.f, false, false}) == AudioMix::Result::Ok);
    mix.grain(plans, sources, n, out);
    MePlan sp = still(1, me(2));
    sp.b = in(1);
    sp.stinger = 1;
    mix.grain({still(2, cp(1)), sp}, sources, n, out);
    CHECK(out[0][0] == out[1][0] * 1.f + 0.5f * 0.f);
    CHECK(!mix.stripStates(1).back().heard);
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Stinger, 0}, StripSettings{0.f, false, false}) == AudioMix::Result::Ok);
    mix.grain({still(2, cp(1)), sp}, sources, n, out);
    mix.grain({still(2, cp(1)), sp}, sources, n, out);
    CHECK(out[0][0] == (out[1][0] * 1.f + 0.5f * 0.f) + 0.0625f && mix.stripStates(1).back().heard);
    // Master then FTB, over the whole sound (AFV-off strips too).
    CHECK(mix.setStrip(1, StripRef{StripRef::Kind::Input, 1}, StripSettings{0.f, false, false}) == AudioMix::Result::Ok);
    MePlan f = still(1, me(2));
    f.ftbPrev = 1.f;
    f.ftb = 1.f;
    mix.grain({still(2, cp(1)), f}, sources, n, out);
    CHECK(out[0][0] == 0.f && out[0][n - 1] == 0.f);
}

TEST(audio_delay_lines_and_channel_maps)
{
    // Defaults: 1:1; a 1-channel flow on every channel; missing channels silent.
    CHECK((channelMap({}, 2, 2) == std::vector<int>{1, 2}));
    CHECK((channelMap({}, 2, 16) == std::vector<int>{1, 2}));
    CHECK((channelMap({}, 2, 1) == std::vector<int>{1, 1}));
    CHECK((channelMap({}, 4, 2) == std::vector<int>{1, 2, 0, 0}));
    CHECK((channelMap({3, 4}, 2, 16) == std::vector<int>{3, 4}));
    CHECK((channelMap({3, 4}, 2, 2) == std::vector<int>{0, 0}));
    CHECK((channelMap({0, 1}, 2, 2) == std::vector<int>{0, 1}));
    // A ring of 2 channels, 1000 samples: the sample index is the value.
    DelayLine line;
    line.reset(2, 1000);
    CHECK(!line.started() && !line.holds(0, 0));
    std::vector<float> chunk(960);
    std::uint64_t at = 50'000;
    line.begin(at);
    for (int g = 0; g < 3; ++g, at += 960)
    {
        for (std::size_t s = 0; s < chunk.size(); ++s)
        {
            chunk[s] = static_cast<float>(at + s);
        }
        line.put(0, at, chunk.data(), chunk.size());
        for (auto& v : chunk)
        {
            v = -v;
        }
        line.put(1, at, chunk.data(), chunk.size());
        line.commit(at + 960);
    }
    // It holds the newest 1000 samples [52880 − 1000, 52880).
    CHECK(line.end() == 52'880u && line.start() == 51'880u && line.holds(51'880, 52'880) && !line.holds(51'879, 52'000));
    std::vector<float> out(960);
    line.read(0, 51'900, 960, out.data());
    CHECK(out[0] == 51'900.f && out[959] == 52'859.f);
    // Outside what it holds: silence.
    line.read(1, 51'800, 960, out.data());
    CHECK(out[0] == 0.f && out[79] == 0.f && out[80] == -51'880.f && out[959] == -52'759.f);
    line.read(0, 52'800, 960, out.data());
    CHECK(out[79] == 52'879.f && out[80] == 0.f);
    // Through a map: ch 2 = input 1, ch 1 = input 2.
    std::vector<float> mapped(2 * 4);
    line.readMapped({2, 1}, 52'000, 4, mapped.data());
    CHECK(mapped[0] == -52'000.f && mapped[4] == 52'000.f && mapped[7] == 52'003.f);
    line.readMapped({0, 3}, 52'000, 4, mapped.data());
    CHECK(mapped[0] == 0.f && mapped[7] == 0.f);
    // A gap: what it held before is gone.
    line.begin(60'000);
    CHECK(line.start() == 60'000u && !line.holds(52'000, 52'010));
    // The delayed span of an output grain: grain i − L, d earlier; consecutive grains tile.
    for (Rate rate : {Rate{50, 1}, Rate{60000, 1001}, Rate{25, 1}})
    {
        for (std::uint64_t d : {0ull, 48ull, 960ull, 48'000ull})
        {
            auto prev = delayedSamples(rate, 100'000, 4, d);
            CHECK(prev.first == grainSamples(rate, 99'996).first - d);
            for (std::uint64_t i = 100'001; i < 100'011; ++i)
            {
                auto const r = delayedSamples(rate, i, 4, d);
                CHECK(r.first == prev.end && r.end - r.first == grainSamples(rate, i - 4).end - grainSamples(rate, i - 4).first);
                prev = r;
            }
        }
    }
    // 59.94p: 800 or 801 samples per grain.
    auto const g1 = delayedSamples(Rate{60000, 1001}, 100'001, 4, 0);
    CHECK(g1.end - g1.first == 800u || g1.end - g1.first == 801u);
}
