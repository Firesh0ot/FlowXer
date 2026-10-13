// DME keyframes (P4 M2): easing, the log-space size, the timeline, runs and loops, and the
// keyers' runs and flies per grain in the Mixer.
#include "test.hpp"

#include "core/keyframes.hpp"
#include "core/mixer.hpp"

#include <algorithm>
#include <cmath>

using namespace fxeng;

namespace
{
DmeSettings boxAt(float x, float y, float size)
{
    DmeSettings box;
    box.enabled = true;
    box.x = x;
    box.y = y;
    box.size = size;
    return box;
}

DmeKeyframe key(float x, float size, int frames = 25, Ease ease = Ease::Linear)
{
    DmeKeyframe k;
    k.box = boxAt(x, 0.5f, size);
    k.frames = frames;
    k.ease = ease;
    return k;
}

// Three keyframes: x 0 → 1 over 10 grains (linear), → 0.5 at size 0.25 over 4 (hold). The
// timeline is 14 grains.
std::vector<DmeKeyframe> three()
{
    return {key(0.f, 0.5f), key(1.f, 0.5f, 10), key(0.5f, 0.25f, 4, Ease::Hold)};
}

// ME 1 with an upstream keyer and a DSK; keyer 1 a pip of input 1 (its box is on).
std::vector<KeyerSpec> const kKeyers = {KeyerSpec{1, true}, KeyerSpec{1, false}};

void pip(Mixer& mixer, bool fly)
{
    KeySettings settings;
    settings.pip = true;
    settings.source = Source{Source::Kind::Input, 1};
    settings.fly = fly;
    mixer.keySettings(1, settings);
}

KeySettings settingsOf(Mixer const& mixer, int k)
{
    return mixer.keyView()[static_cast<std::size_t>(k - 1)].settings;
}
} // namespace

TEST(keyframe_easing)
{
    for (auto ease : {Ease::Linear, Ease::Ease, Ease::EaseIn, Ease::EaseOut, Ease::Hold})
    {
        CHECK(easeAt(ease, 0.f) == 0.f && easeAt(ease, 1.f) == 1.f);
        CHECK(parseEase(easeName(ease)) == ease);
    }
    CHECK(easeAt(Ease::Linear, 0.25f) == 0.25f);
    CHECK(easeAt(Ease::Ease, 0.25f) == 0.15625f);
    CHECK(easeAt(Ease::Ease, 0.5f) == 0.5f);
    CHECK(easeAt(Ease::EaseIn, 0.25f) == 0.0625f);
    CHECK(easeAt(Ease::EaseOut, 0.25f) == 0.4375f);
    CHECK(easeAt(Ease::Hold, 0.99f) == 0.f);
    CHECK(easeAt(Ease::Linear, 1.5f) == 1.f && easeAt(Ease::Linear, -1.f) == 0.f);
    CHECK(!parseEase("bounce"));
    CHECK(parseLoop("pingpong") == DmeLoop::PingPong && parseLoop("wrap") == DmeLoop::Wrap && parseLoop("off") == DmeLoop::Off);
    CHECK(!parseLoop("loop"));
}

TEST(keyframe_size_in_log_space)
{
    // Exact at the ends; the middle of 0.1 → 1 is √0.1 (a zoom that looks even).
    CHECK(dmeLogSize(0.1f, 1.f, 0.f) == 0.1f && dmeLogSize(0.1f, 1.f, 1.f) == 1.f);
    CHECK(std::abs(dmeLogSize(0.1f, 1.f, 0.5f) - 0.316227766f) < 1e-6f);
    // Size 0 interpolates as 0.01: the middle of 0 → 1 is 0.1; two tiny sizes interpolate linearly.
    CHECK(dmeLogSize(0.f, 1.f, 0.f) == 0.f && std::abs(dmeLogSize(0.f, 1.f, 0.5f) - 0.1f) < 1e-6f);
    CHECK(dmeLogSize(0.f, 0.004f, 0.5f) == 0.002f);
    // The engine's own ln and exp (no libm) against the C library over a sweep.
    float worst = 0.f;
    for (int i = 0; i <= 100; ++i)
    {
        float const w = static_cast<float>(i) / 100.f;
        double const want = std::exp(std::log(0.07) + (std::log(1.9) - std::log(0.07)) * static_cast<double>(w));
        worst = std::max(worst, static_cast<float>(std::abs(static_cast<double>(dmeLogSize(0.07f, 1.9f, w)) - want) / want));
    }
    CHECK(worst < 1e-6f);
    // Linear in the other fields; exact at both ends.
    DmeSettings a = boxAt(0.1f, 0.2f, 0.5f);
    DmeSettings b = boxAt(0.7f, 0.9f, 0.5f);
    a.shadowOpacity = 0.25f;
    b.shadowOpacity = 0.75f;
    CHECK(dmeSameState(dmeBetween(a, a, b, 0.f), a) && dmeSameState(dmeBetween(a, a, b, 1.f), b));
    auto const mid = dmeBetween(a, a, b, 0.5f);
    CHECK(std::abs(mid.x - 0.4f) < 1e-6f && std::abs(mid.y - 0.55f) < 1e-6f && mid.shadowOpacity == 0.5f && mid.size == 0.5f);
}

TEST(keyframe_path)
{
    auto const keys = three();
    DmeSettings box = boxAt(0.3f, 0.3f, 1.f);
    box.borderY = 700;
    CHECK(dmeKeyTime(keys, 0) == 0.f && dmeKeyTime(keys, 1) == 10.f && dmeKeyTime(keys, 2) == 14.f);
    // The animated fields come from the path, the border colour from the keyer's box.
    auto const at0 = dmePathAt(keys, 0.f, box);
    CHECK(at0.x == 0.f && at0.borderY == 700 && at0.enabled);
    CHECK(dmePathAt(keys, 5.f, box).x == 0.5f);
    CHECK(dmeSameState(dmePathAt(keys, 10.f, box), keys[1].box));
    // Hold: keyframe 1 until the last grain of the segment, then keyframe 2.
    CHECK(dmeSameState(dmePathAt(keys, 13.f, box), keys[1].box));
    CHECK(dmeSameState(dmePathAt(keys, 14.f, box), keys[2].box));
    CHECK(dmeSameState(dmePathAt(keys, 99.f, box), keys[2].box) && dmeSameState(dmePathAt(keys, -1.f, box), keys[0].box));
    CHECK(dmeKeyframeOf(keys, keys[2].box) == 2 && dmeKeyframeOf(keys, box) == -1);
}

TEST(keyframe_runs_and_loops)
{
    auto const keys = three();
    DmeSettings const box = boxAt(0.f, 0.5f, 0.5f);
    // On the path from keyframe 0 to 2: step f is at u = f, done at 14 (keyframe 2 exactly).
    auto run = dmeRunFrom(keys, box, true, 0.f, 2, DmeLoop::Off, false);
    for (std::uint64_t f = 1; f < 14; ++f)
    {
        auto const s = dmeRunAt(keys, run, f, box);
        CHECK(s.onPath && s.u == static_cast<float>(f) && !s.done && s.keyframe == (f <= 10 ? 1 : 2));
        CHECK(dmeSameState(s.box, dmePathAt(keys, static_cast<float>(f), box)));
    }
    auto const end = dmeRunAt(keys, run, 14, box);
    CHECK(end.done && end.u == 14.f && dmeSameState(end.box, keys[2].box) && end.keyframe == 2);
    CHECK(dmeRunAt(keys, run, 40, box).done && dmeRunAt(keys, run, 40, box).u == 14.f);
    // Back from 12 to keyframe 0.
    run = dmeRunFrom(keys, box, true, 12.f, 0, DmeLoop::Off, false);
    CHECK(dmeRunAt(keys, run, 1, box).u == 11.f && dmeRunAt(keys, run, 1, box).keyframe == 1 && dmeRunAt(keys, run, 3, box).keyframe == 0);
    CHECK(dmeRunAt(keys, run, 12, box).done && dmeSameState(dmeRunAt(keys, run, 12, box).box, keys[0].box));
    // Off the path: a lead-in straight to keyframe 1 over its frames (10) and ease, done there.
    DmeSettings const off = boxAt(0.4f, 0.2f, 1.f);
    run = dmeRunFrom(keys, off, false, 0.f, 1, DmeLoop::Off, false);
    auto const half = dmeRunAt(keys, run, 5, box);
    CHECK(!half.onPath && !half.done && half.keyframe == 1 && dmeSameState(half.box, dmeBetween(box, off, keys[1].box, 0.5f)));
    auto const there = dmeRunAt(keys, run, 10, box);
    CHECK(there.onPath && there.done && there.u == 10.f && dmeSameState(there.box, keys[1].box));
    // Wrap from keyframe 0: up to 14, the next grain jumps to 0, a cycle of 15 grains.
    run = dmeRunFrom(keys, box, true, 0.f, 0, DmeLoop::Wrap, false);
    CHECK(dmeRunAt(keys, run, 14, box).u == 14.f && dmeRunAt(keys, run, 15, box).u == 0.f && dmeRunAt(keys, run, 16, box).u == 1.f);
    CHECK(dmeRunAt(keys, run, 29, box).u == 14.f && dmeRunAt(keys, run, 30, box).u == 0.f && !dmeRunAt(keys, run, 30, box).done);
    // Wrap backwards from 14: down to 0, then 14 again.
    run = dmeRunFrom(keys, box, true, 14.f, 0, DmeLoop::Wrap, true);
    CHECK(dmeRunAt(keys, run, 1, box).u == 13.f && dmeRunAt(keys, run, 14, box).u == 0.f && dmeRunAt(keys, run, 15, box).u == 14.f);
    CHECK(dmeRunAt(keys, run, 1, box).keyframe == 1);
    // Pingpong from 4: up to 14, down to 0, up again (a period of 28).
    run = dmeRunFrom(keys, box, true, 4.f, 0, DmeLoop::PingPong, false);
    CHECK(dmeRunAt(keys, run, 10, box).u == 14.f && dmeRunAt(keys, run, 11, box).u == 13.f && dmeRunAt(keys, run, 24, box).u == 0.f);
    CHECK(dmeRunAt(keys, run, 25, box).u == 1.f && dmeRunAt(keys, run, 11, box).keyframe == 1 && dmeRunAt(keys, run, 25, box).keyframe == 1);
    // Pingpong down first; a loop off the path leads in to its end first.
    run = dmeRunFrom(keys, box, true, 4.f, 0, DmeLoop::PingPong, true);
    CHECK(dmeRunAt(keys, run, 1, box).u == 3.f && dmeRunAt(keys, run, 4, box).u == 0.f && dmeRunAt(keys, run, 5, box).u == 1.f);
    run = dmeRunFrom(keys, off, false, 0.f, 0, DmeLoop::PingPong, false);
    CHECK(run.leadTo == 2 && run.leadFrames == 4 && run.leadEase == Ease::Hold);
    CHECK(dmeRunAt(keys, run, 4, box).u == 14.f && dmeRunAt(keys, run, 5, box).u == 13.f);
}

TEST(keyer_run_follows_the_grain_index)
{
    Mixer mixer(1, 2, 0, kKeyers);
    pip(mixer, false);
    std::vector<DmeKeyframe> keys = {key(0.2f, 0.1f), key(0.8f, 1.f, 25, Ease::Ease)};
    CHECK(mixer.keyKeyframes(1, keys) == MixResult::Ok);
    CHECK(mixer.keyRun(1, 0, false, DmeLoop::Off, 0) == MixResult::Ok);
    CHECK(dmeSameState(settingsOf(mixer, 1).dme, keys[0].box) && mixer.keyView()[0].run.keyframe == 0 && mixer.keyView()[0].run.onPath);
    // The run starts at the first grain after the command: grain 1000 shows step 1.
    CHECK(mixer.keyRun(1, std::nullopt, false, DmeLoop::Off, std::nullopt) == MixResult::Ok);
    CHECK(mixer.keyView()[0].run.running);
    DmeRun const reference = dmeRunFrom(keys, settingsOf(mixer, 1).dme, true, 0.f, 1, DmeLoop::Off, false);
    DmeSettings const base = settingsOf(mixer, 1).dme;
    bool exact = true;
    for (std::uint64_t i : {1000u, 1001u, 1002u, 1010u, 1011u, 1023u})
    {
        auto const plan = mixer.frame(i);
        exact = exact && dmeSameState(plan.keys[0].settings.dme, dmeRunAt(keys, reference, i - 999, base).box);
    }
    CHECK(exact);
    auto view = mixer.keyView()[0];
    CHECK(view.run.running && view.run.start == 1000 && view.run.step == 24 && view.run.keyframe == 1);
    // Skipped grains do not stretch it: grain 1024 is its last step (keyframe 1 exactly).
    auto const last = mixer.frame(1024).keys[0].settings.dme;
    view = mixer.keyView()[0];
    CHECK(dmeSameState(last, keys[1].box) && !view.run.running && view.run.keyframe == 1 && view.run.onPath && view.run.u == 25.f);
    CHECK(dmeSameState(mixer.frame(1030).keys[0].settings.dme, keys[1].box));
    // A size fly is smooth: no grain repeats the one before.
    mixer.keyRun(1, 0, false, DmeLoop::Off, std::nullopt);
    float previous = 2.f;
    bool shrinking = true;
    for (std::uint64_t i = 2000; i < 2025; ++i)
    {
        float const size = mixer.frame(i).keys[0].settings.dme.size;
        shrinking = shrinking && size < previous;
        previous = size;
    }
    CHECK(shrinking && previous == 0.1f);
}

TEST(keyer_stop_resume_and_box_changes)
{
    Mixer mixer(1, 2, 0, kKeyers);
    pip(mixer, false);
    std::vector<DmeKeyframe> keys = {key(0.f, 0.5f), key(1.f, 0.5f, 10)};
    mixer.keyKeyframes(1, keys);
    mixer.keyRun(1, 0, false, DmeLoop::Off, 0);
    mixer.keyRun(1, 1, false, DmeLoop::PingPong, std::nullopt);
    mixer.frame(100);
    mixer.frame(103);
    // Stop holds where it is (u = 4); a new run goes on from there.
    CHECK(mixer.keyStop(1) == MixResult::Ok);
    auto view = mixer.keyView()[0];
    CHECK(!view.run.running && view.run.onPath && view.run.u == 4.f && view.settings.dme.x == 0.4f && view.run.keyframe == -1);
    CHECK(mixer.frame(110).keys[0].settings.dme.x == 0.4f);
    mixer.keyRun(1, std::nullopt, false, DmeLoop::Off, std::nullopt);
    CHECK(mixer.frame(111).keys[0].settings.dme.x == 0.5f);
    // A change of the border colour keeps a run; a change of the position stops it there.
    KeySettings settings = settingsOf(mixer, 1);
    CHECK(mixer.keySettings(1, settings, [](DmeSettings& box) { box.borderY = 300; }) == MixResult::Ok);
    CHECK(mixer.keyView()[0].run.running && mixer.frame(112).keys[0].settings.dme.borderY == 300);
    CHECK(mixer.keySettings(1, settings, [](DmeSettings& box) { box.y = 0.25f; }) == MixResult::Ok);
    view = mixer.keyView()[0];
    CHECK(!view.run.running && !view.run.onPath && view.settings.dme.x == 0.6f && view.settings.dme.y == 0.25f);
    // From off the path: a lead-in to keyframe 1 over its 10 grains.
    mixer.keyRun(1, 1, false, DmeLoop::Off, std::nullopt);
    auto const lead = mixer.frame(200).keys[0].settings.dme;
    CHECK(std::abs(lead.x - 0.64f) < 1e-6f && std::abs(lead.y - 0.275f) < 1e-6f);
    CHECK(dmeSameState(mixer.frame(209).keys[0].settings.dme, keys[1].box) && mixer.keyView()[0].run.onPath);
    // New keyframes stop a run; a box on one of them is on the path there.
    mixer.keyRun(1, 0, false, DmeLoop::Off, std::nullopt);
    mixer.frame(300);
    CHECK(mixer.keyKeyframes(1, {keys[1], keys[0]}) == MixResult::Ok);
    view = mixer.keyView()[0];
    CHECK(!view.run.running && !view.run.onPath);
    mixer.keyRun(1, 1, false, DmeLoop::Off, 0);
    view = mixer.keyView()[0];
    CHECK(view.run.onPath && view.run.u == 25.f && view.run.keyframe == 1);
    // Errors: 9 keyframes, frames 0, a DSK, ranges; runs without a box, to a missing keyframe, a
    // loop of one keyframe, frames other than 0.
    CHECK(mixer.keyKeyframes(1, std::vector<DmeKeyframe>(9, keys[0])) == MixResult::BadKeyframes);
    CHECK(mixer.keyKeyframes(1, {key(0.f, 0.5f, 0)}) == MixResult::BadFrames);
    CHECK(mixer.keyKeyframes(2, keys) == MixResult::BadDme);
    CHECK(mixer.keyKeyframes(1, {key(0.f, 3.f)}) == MixResult::BadDme);
    CHECK(mixer.keyRun(1, 2, false, DmeLoop::Off, std::nullopt) == MixResult::BadKeyframes);
    CHECK(mixer.keyRun(1, 0, false, DmeLoop::Off, 5) == MixResult::BadFrames);
    CHECK(mixer.keyRun(2, 0, false, DmeLoop::Off, std::nullopt) == MixResult::BadDme);
    mixer.keyKeyframes(1, {keys[0]});
    CHECK(mixer.keyRun(1, std::nullopt, false, DmeLoop::Wrap, std::nullopt) == MixResult::BadKeyframes);
    CHECK(mixer.keyRun(1, 0, false, DmeLoop::Off, std::nullopt) == MixResult::Ok);
    CHECK(mixer.keyRun(3, 0, false, DmeLoop::Off, std::nullopt) == MixResult::BadMe && mixer.keyStop(3) == MixResult::BadMe);
}

TEST(keyer_flies_with_its_auto)
{
    Mixer mixer(1, 2, 0, kKeyers);
    pip(mixer, true);
    std::vector<DmeKeyframe> keys = {key(-0.3f, 0.5f), key(0.5f, 0.5f, 10, Ease::EaseOut)};
    mixer.keyKeyframes(1, keys);
    // Auto on: the keyer is on at once and its box runs from keyframe 0 to 1.
    CHECK(mixer.keyAuto(1, 25) == MixResult::Ok);
    auto plan = mixer.frame(10).keys[0];
    CHECK(plan.gain == 1.f && dmeSameState(plan.settings.dme, dmePathAt(keys, 1.f, plan.settings.dme)));
    CHECK(mixer.keyView()[0].on && mixer.keyView()[0].transition && mixer.keyView()[0].run.fly);
    plan = mixer.frame(19).keys[0];
    CHECK(plan.gain == 1.f && dmeSameState(plan.settings.dme, keys[1].box) && !mixer.keyView()[0].transition);
    // Auto off: back to keyframe 0; that grain is its last on air.
    mixer.keyAuto(1, 25);
    CHECK(!mixer.keyView()[0].on);
    for (std::uint64_t i = 20; i < 29; ++i)
    {
        plan = mixer.frame(i).keys[0];
        CHECK(plan.gain == 1.f);
    }
    plan = mixer.frame(29).keys[0];
    CHECK(plan.gain == 1.f && dmeSameState(plan.settings.dme, keys[0].box));
    CHECK(mixer.frame(30).keys[0].gain == 0.f && !mixer.keyView()[0].on && !mixer.keyView()[0].transition);
    // An Auto during a fly turns back from where the box is (4 grains in, 4 grains back), then off.
    mixer.keyAuto(1, 25);
    for (std::uint64_t i = 40; i < 44; ++i)
    {
        mixer.frame(i);
    }
    mixer.keyAuto(1, 25);
    CHECK(mixer.frame(44).keys[0].settings.dme.x == dmePathAt(keys, 3.f, keys[0].box).x);
    mixer.frame(46);
    plan = mixer.frame(47).keys[0];
    CHECK(plan.gain == 1.f && dmeSameState(plan.settings.dme, keys[0].box) && mixer.frame(48).keys[0].gain == 0.f);
    // A stop during a fly off leaves the keyer on air where it is.
    mixer.keyAuto(1, 25);
    mixer.frame(50);
    mixer.frame(59);
    mixer.keyAuto(1, 25);
    mixer.frame(60);
    mixer.keyStop(1);
    CHECK(mixer.keyView()[0].on && mixer.frame(70).keys[0].gain == 1.f && mixer.keyView()[0].run.u == 9.f);
    // Cut on and off jump to the last keyframe and to keyframe 0.
    CHECK(mixer.keySet(1, false) == MixResult::Ok);
    plan = mixer.frame(71).keys[0];
    CHECK(plan.gain == 0.f && dmeSameState(plan.settings.dme, keys[0].box));
    mixer.keySet(1, true);
    plan = mixer.frame(72).keys[0];
    CHECK(plan.gain == 1.f && dmeSameState(plan.settings.dme, keys[1].box));
    // Without keyframes (or with fly off) the Auto fades.
    mixer.keyKeyframes(1, {});
    mixer.keyAuto(1, 4);
    CHECK(mixer.frame(80).keys[0].gain == 0.75f);
}
