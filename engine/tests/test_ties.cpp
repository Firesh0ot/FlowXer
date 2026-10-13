// Keyer ties (P4 M3, docs/engine-p4.md §5): tied keyers go on or off with the ME's Cut, Auto and
// T-bar; a fading keyer fades with the transition's t, a flying keyer flies along its keyframes
// squeezed into the transition; with a stinger they cut at its cut point.
#include "test.hpp"

#include "core/keyframes.hpp"
#include "core/mixer.hpp"

#include <vector>

using namespace fxeng;

namespace
{
Source in(int k)
{
    return Source{Source::Kind::Input, k};
}

DmeKeyframe key(float x, float size, int frames = 25, Ease ease = Ease::Linear)
{
    DmeKeyframe k;
    k.box.enabled = true;
    k.box.x = x;
    k.box.size = size;
    k.frames = frames;
    k.ease = ease;
    return k;
}

// Off screen left, on (ease-out), a little smaller (ease): a 16-grain timeline.
std::vector<DmeKeyframe> path()
{
    return {key(-0.3f, 0.5f), key(0.5f, 0.5f, 10, Ease::EaseOut), key(0.6f, 0.4f, 6, Ease::Ease)};
}

// ME 1: upstream keyers 1 (a fading pip) and 2 (a flying pip), DSK 3; ME 2: upstream keyer 4.
std::vector<KeyerSpec> const kKeyers = {KeyerSpec{1, true}, KeyerSpec{1, true}, KeyerSpec{1, false}, KeyerSpec{2, true}};

// ME 1 on input 1, Preview input 2; keyers 1 and 2 tied to its 8-grain Mix.
void tie(Mixer& mixer)
{
    mixer.setProgram(1, in(1));
    mixer.setPreview(1, in(2));
    for (int k : {1, 2})
    {
        KeySettings settings;
        settings.pip = true;
        settings.source = in(1);
        settings.fly = k == 2;
        mixer.keySettings(k, settings);
    }
    mixer.keyKeyframes(2, path());
    TransitionSettings next;
    next.frames = 8;
    next.ties = {1, 2};
    mixer.setTransition(1, next);
}

// ME 1's plan and keyers 1 and 2 of grain `index` (ME 1 renders last).
struct Grain
{
    MePlan me;
    KeyPlan fade;
    KeyPlan fly;
};

Grain grain(Mixer& mixer, std::uint64_t index, std::vector<bool> const& ready = {})
{
    auto const plan = mixer.frame(index, ready);
    return Grain{plan.mes[1], plan.keys[0], plan.keys[1]};
}

// Keyer 2's box at timeline position u.
DmeSettings flyAt(Mixer const& mixer, float u)
{
    return dmePathAt(path(), u, mixer.keyView()[1].settings.dme);
}
} // namespace

TEST(tie_fade_and_fly_with_an_auto)
{
    Mixer mixer(2, 2, 0, kKeyers);
    tie(mixer);
    float const total = dmeKeyTime(path(), 2);
    CHECK(mixer.autoTransition(1) == MixResult::Ok);
    auto view = mixer.keyView();
    CHECK(view[0].on && view[0].tie == 1 && view[0].transition && view[1].on && view[1].tie == 1 && view[2].tie == 0);
    CHECK(mixer.view()[0].tied == std::vector<int>({1, 2}));
    // Every grain: the fade's gain is the mix's t, the fly is at t of its timeline (on air).
    bool exact = true;
    for (std::uint64_t i = 100; i < 108; ++i)
    {
        auto const g = grain(mixer, i);
        float const t = g.me.t;
        exact = exact && g.me.mixing && t == static_cast<float>(i - 99) / 8.f && g.fade.gain == t;
        exact = exact && g.fly.gain == 1.f && dmeSameState(g.fly.settings.dme, flyAt(mixer, tieAt(0.f, total, t)));
    }
    CHECK(exact);
    // The last grain showed both in full; the ties end with the transition.
    view = mixer.keyView();
    CHECK(view[0].gain == 1.f && view[0].tie == 0 && !view[0].transition && view[1].tie == 0 && !view[1].transition);
    CHECK(dmeSameState(view[1].settings.dme, path()[2].box) && view[1].run.keyframe == 2 && view[1].run.onPath);
    CHECK(mixer.view()[0].program == in(2) && mixer.view()[0].tied.empty() && mixer.view()[0].start == 100);
    // Auto again: both go off; the fly is on air up to its last grain (keyframe 0), off after.
    CHECK(mixer.autoTransition(1) == MixResult::Ok);
    CHECK(!mixer.keyView()[0].on && !mixer.keyView()[1].on);
    exact = true;
    for (std::uint64_t i = 200; i < 208; ++i)
    {
        auto const g = grain(mixer, i);
        float const t = g.me.t;
        exact = exact && g.fade.gain == tieAt(1.f, 0.f, t) && g.fly.gain == 1.f && dmeSameState(g.fly.settings.dme, flyAt(mixer, tieAt(total, 0.f, t)));
    }
    CHECK(exact);
    auto const after = grain(mixer, 208);
    CHECK(after.fade.gain == 0.f && after.fly.gain == 0.f && dmeSameState(after.fly.settings.dme, path()[0].box) && after.me.a == in(1));
    // A wipe takes them along the same way; a take without ties does not.
    TransitionSettings wipe;
    wipe.type = Transition::Wipe;
    wipe.frames = 4;
    wipe.ties = {1};
    CHECK(mixer.setTransition(1, wipe) == MixResult::Ok);
    CHECK(mixer.autoTransition(1) == MixResult::Ok);
    auto g = grain(mixer, 300);
    CHECK(g.me.wiping && g.fade.gain == 0.25f && g.fly.gain == 0.f);
    CHECK(mixer.take(1, in(1), 4, std::nullopt, Transition::Wipe) == MixResult::Ok);
    g = grain(mixer, 301);
    CHECK(g.fade.gain == 1.f && mixer.keyView()[0].tie == 0 && g.me.t == 0.25f);
}

TEST(tie_follows_the_t_bar)
{
    Mixer mixer(2, 2, 0, kKeyers);
    tie(mixer);
    float const total = dmeKeyTime(path(), 2);
    CHECK(mixer.position(1, 0.25f) == MixResult::Ok);
    auto g = grain(mixer, 100);
    CHECK(g.me.mixing && g.fade.gain == 0.25f && g.fly.gain == 1.f && dmeSameState(g.fly.settings.dme, flyAt(mixer, tieAt(0.f, total, 0.25f))));
    mixer.position(1, 0.75f);
    g = grain(mixer, 101);
    CHECK(g.fade.gain == 0.75f && dmeSameState(g.fly.settings.dme, flyAt(mixer, 12.f)));
    // Back to 0: buses and keyers as they were (both off, the fly at keyframe 0).
    mixer.position(1, 0.f);
    g = grain(mixer, 102);
    CHECK(g.me.a == in(1) && g.fade.gain == 0.f && g.fly.gain == 0.f && !mixer.keyView()[0].on && !mixer.keyView()[1].on);
    CHECK(mixer.keyView()[0].tie == 0 && dmeSameState(g.fly.settings.dme, path()[0].box));
    // Half way, then Auto runs the rest (4 of 8 grains): the keyers go on with it.
    mixer.position(1, 0.5f);
    grain(mixer, 110);
    CHECK(mixer.autoTransition(1) == MixResult::Ok);
    bool exact = true;
    for (std::uint64_t i = 111; i < 115; ++i)
    {
        g = grain(mixer, i);
        exact = exact && g.fade.gain == g.me.t && dmeSameState(g.fly.settings.dme, flyAt(mixer, tieAt(0.f, total, g.me.t)));
    }
    CHECK(exact && g.me.t == 1.f && mixer.keyView()[0].on && mixer.keyView()[0].tie == 0 && mixer.view()[0].program == in(2));
    // To the bottom: complete (both off after the grain at t = 1).
    mixer.position(1, 1.f);
    g = grain(mixer, 120);
    CHECK(g.fade.gain == 0.f && g.fly.gain == 1.f && dmeSameState(g.fly.settings.dme, path()[0].box));
    g = grain(mixer, 121);
    CHECK(g.fly.gain == 0.f && !mixer.keyView()[1].on && mixer.view()[0].program == in(1));
}

TEST(tie_cuts_at_the_stingers_cut_point)
{
    Mixer mixer(2, 2, 0, kKeyers);
    tie(mixer);
    std::vector<bool> const ready{true};
    std::vector<bool> const notReady{false};
    CHECK(mixer.take(1, in(2), 0, in(1), Transition::Stinger, Sting{1, 10, 4}, true) == MixResult::Ok);
    CHECK(mixer.keyView()[0].tie == 1 && !mixer.keyView()[0].on);
    // Waiting for its first frame and before the cut point: as they were.
    auto g = grain(mixer, 399, notReady);
    CHECK(g.fade.gain == 0.f && g.fly.gain == 0.f);
    bool before = true;
    for (std::uint64_t i = 400; i < 404; ++i)
    {
        g = grain(mixer, i, ready);
        before = before && !g.me.cutDone && g.fade.gain == 0.f && g.fly.gain == 0.f;
    }
    CHECK(before);
    // At the cut point (frame 4), with the background: on (the fly at its last keyframe).
    g = grain(mixer, 404, ready);
    CHECK(g.me.cutDone && g.fade.gain == 1.f && g.fly.gain == 1.f && dmeSameState(g.fly.settings.dme, path()[2].box));
    CHECK(mixer.keyView()[0].on && mixer.keyView()[0].tie == 0 && mixer.keyView()[1].on);
    for (std::uint64_t i = 405; i < 410; ++i)
    {
        grain(mixer, i, ready);
    }
    CHECK(mixer.view()[0].program == in(2) && !mixer.view()[0].mixing);
    // A stinger that never gets ready becomes a cut after kStingerWait grains: the keyers cut then.
    CHECK(mixer.take(1, in(1), 0, in(2), Transition::Stinger, Sting{1, 10, 4}, true) == MixResult::Ok);
    bool waiting = true;
    for (std::uint64_t k = 0; k < static_cast<std::uint64_t>(Mixer::kStingerWait); ++k)
    {
        g = grain(mixer, 500 + k, notReady);
        waiting = waiting && g.fade.gain == 1.f && g.fly.gain == 1.f;
    }
    CHECK(waiting);
    g = grain(mixer, 500 + static_cast<std::uint64_t>(Mixer::kStingerWait), notReady);
    CHECK(g.me.a == in(1) && g.fade.gain == 0.f && g.fly.gain == 0.f && dmeSameState(g.fly.settings.dme, path()[0].box));
    // A take during a stinger before its cut point: the keyers cut as the stinger ends.
    CHECK(mixer.take(1, in(2), 0, in(1), Transition::Stinger, Sting{1, 10, 4}, true) == MixResult::Ok);
    grain(mixer, 600, ready);
    CHECK(mixer.take(1, in(1), 0, std::nullopt, Transition::Cut) == MixResult::Ok);
    g = grain(mixer, 601, ready);
    CHECK(g.fade.gain == 1.f && g.fly.gain == 1.f && g.me.a == in(1) && mixer.keyView()[0].tie == 0);
}

TEST(tie_with_a_cut_and_a_cut_during_a_tied_auto)
{
    Mixer mixer(2, 2, 0, kKeyers);
    tie(mixer);
    // Cut: the buses swap and the keyers cut at the next grain.
    CHECK(mixer.cut(1) == MixResult::Ok);
    auto g = grain(mixer, 100);
    CHECK(g.me.a == in(2) && g.fade.gain == 1.f && g.fly.gain == 1.f && dmeSameState(g.fly.settings.dme, path()[2].box));
    // A Cut during a tied Auto ends it as at its end: the keyers go off once (not back on).
    mixer.autoTransition(1);
    grain(mixer, 101);
    grain(mixer, 102);
    CHECK(mixer.cut(1) == MixResult::Ok);
    g = grain(mixer, 103);
    CHECK(g.me.a == in(1) && !g.me.mixing && g.fade.gain == 0.f && g.fly.gain == 0.f && !mixer.keyView()[0].on && !mixer.keyView()[1].on);
    // A take (P2's take, a direct cut to Program) leaves the keyers alone.
    CHECK(mixer.take(1, in(2), 0, std::nullopt) == MixResult::Ok);
    g = grain(mixer, 104);
    CHECK(g.me.a == in(2) && g.fade.gain == 0.f && g.fly.gain == 0.f);
    // A new Program during a tied Auto ends it as at its end.
    mixer.autoTransition(1);
    grain(mixer, 105);
    CHECK(mixer.setProgram(1, in(1)) == MixResult::Ok);
    g = grain(mixer, 106);
    CHECK(g.fade.gain == 1.f && g.fly.gain == 1.f && dmeSameState(g.fly.settings.dme, path()[2].box));
}

TEST(tie_keyer_commands_take_it_out)
{
    Mixer mixer(2, 2, 0, kKeyers);
    tie(mixer);
    float const total = dmeKeyTime(path(), 2);
    mixer.autoTransition(1);
    for (std::uint64_t i = 100; i < 103; ++i)
    {
        grain(mixer, i);
    }
    // The flying keyer's own Auto turns back from where it is (u = 3/8 of 16 = 6), out of the tie;
    // the fading keyer goes on with the mix.
    float const u = tieAt(0.f, total, 3.f / 8.f);
    CHECK(u == 6.f && mixer.keyAuto(2, 25) == MixResult::Ok);
    CHECK(mixer.keyView()[1].tie == 0 && !mixer.keyView()[1].on && mixer.keyView()[1].run.running);
    auto g = grain(mixer, 103);
    CHECK(dmeSameState(g.fly.settings.dme, flyAt(mixer, 5.f)) && g.fade.gain == 0.5f && mixer.keyView()[0].tie == 1);
    // The fading keyer cut off: out of the tie at once.
    CHECK(mixer.keySet(1, false) == MixResult::Ok);
    g = grain(mixer, 104);
    CHECK(g.fade.gain == 0.f && mixer.keyView()[0].tie == 0 && !mixer.keyView()[0].on);
    // A stop during a tied fly off holds the box on air where it is.
    for (std::uint64_t i = 105; i < 120; ++i)
    {
        grain(mixer, i);
    }
    mixer.keySet(2, true);
    mixer.autoTransition(1);
    grain(mixer, 200);
    grain(mixer, 201);
    CHECK(!mixer.keyView()[1].on && mixer.keyStop(2) == MixResult::Ok);
    g = grain(mixer, 202);
    CHECK(mixer.keyView()[1].on && mixer.keyView()[1].tie == 0 && g.fly.gain == 1.f && dmeSameState(g.fly.settings.dme, flyAt(mixer, 12.f)));
}

TEST(tie_settings_are_checked)
{
    Mixer mixer(2, 2, 0, kKeyers);
    tie(mixer);
    TransitionSettings next;
    // Upstream keyers of the ME only, each once.
    for (auto const& ties : std::vector<std::vector<int>>{{3}, {4}, {1, 1}, {0}, {5}})
    {
        next.ties = ties;
        CHECK(mixer.setTransition(1, next) == MixResult::BadTies);
    }
    next.ties = {4};
    CHECK(mixer.setTransition(2, next) == MixResult::Ok);
    next.ties = {2, 1};
    CHECK(mixer.setTransition(1, next) == MixResult::Ok && mixer.view()[0].next.ties == std::vector<int>({2, 1}));
    // A running transition keeps the keyers it took along.
    mixer.autoTransition(1);
    next.ties = {};
    mixer.setTransition(1, next);
    CHECK(mixer.view()[0].tied == std::vector<int>({1, 2}) && mixer.view()[0].next.ties.empty());
}
