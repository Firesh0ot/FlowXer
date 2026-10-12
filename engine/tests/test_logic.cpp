// MEs, transitions, DSKs, the mosaic map, the TAI grid, the configuration and the audio maths.
#include "test.hpp"

#include "core/audio.hpp"
#include "core/config.hpp"
#include "core/mixer.hpp"
#include "core/mosaic_map.hpp"
#include "core/pixel.hpp"
#include "core/playout.hpp"
#include "core/tai.hpp"

#include <cmath>
#include <set>

#include <nlohmann/json.hpp>

using namespace fxeng;

namespace
{
bool configThrows(std::map<std::string, std::string> const& env)
{
    try
    {
        loadConfig(env);
    }
    catch (ConfigError const&)
    {
        return true;
    }
    return false;
}

std::string const kInputs = R"([{"domain":"/Volumes/mxl/a","flow":"f1","label":"Cam 1"},{"domain":"/Volumes/mxl/b","flow":"f2"}])";

Source in(int k)
{
    return Source{Source::Kind::Input, k};
}
} // namespace

// ---- MEs, re-entry, transitions -----------------------------------------------------------

TEST(reentry_order_and_rules)
{
    Mixer mixer(4, 8, 2);
    auto const order = mixer.renderOrder();
    CHECK(order == (std::vector<int>{4, 3, 2, 1}));
    auto const plans = mixer.frame(1000).mes;
    CHECK_EQ(plans.size(), 4u);
    CHECK_EQ(plans.front().me, 4);
    CHECK_EQ(plans.back().me, 1);
    // ME m takes the Program of ME n only when n > m: no loop can exist.
    CHECK(mixer.sourceAllowed(1, Source{Source::Kind::Me, 2}));
    CHECK(mixer.sourceAllowed(1, Source{Source::Kind::Me, 4}));
    CHECK(mixer.sourceAllowed(3, Source{Source::Kind::Me, 4}));
    CHECK(!mixer.sourceAllowed(2, Source{Source::Kind::Me, 2}));
    CHECK(!mixer.sourceAllowed(2, Source{Source::Kind::Me, 1}));
    CHECK(!mixer.sourceAllowed(4, Source{Source::Kind::Me, 5}));
    CHECK(mixer.sourceAllowed(4, in(8)));
    CHECK(!mixer.sourceAllowed(4, in(9)));
    CHECK(!mixer.sourceAllowed(4, in(0)));
    // Clip players and Black.
    CHECK(mixer.sourceAllowed(1, Source{Source::Kind::Player, 2}));
    CHECK(!mixer.sourceAllowed(1, Source{Source::Kind::Player, 3}));
    CHECK(mixer.sourceAllowed(4, Source{Source::Kind::Black, 0}));
    CHECK(mixer.setProgram(1, Source{Source::Kind::Me, 2}) == MixResult::Ok);
    CHECK(mixer.setPreview(2, Source{Source::Kind::Me, 1}) == MixResult::BadSource);
    CHECK(mixer.setPreview(5, in(1)) == MixResult::BadMe);
    // With 2 MEs there is no ME 3 to re-enter.
    Mixer two(2, 8);
    CHECK(!two.sourceAllowed(1, Source{Source::Kind::Me, 3}));
    CHECK(two.sourceAllowed(1, Source{Source::Kind::Me, 2}));
    // Without inputs every ME starts on Black.
    Mixer none(1, 0, 1);
    CHECK(none.view()[0].program == (Source{Source::Kind::Black, 0}));
}

TEST(source_names)
{
    CHECK(parseSource("in1").has_value());
    CHECK(parseSource("in23")->index == 23);
    CHECK(parseSource("me2")->kind == Source::Kind::Me);
    CHECK(parseSource("cp3")->kind == Source::Kind::Player);
    CHECK(parseSource("black")->kind == Source::Kind::Black);
    CHECK(!parseSource("in0").has_value());
    CHECK(!parseSource("me").has_value());
    CHECK(!parseSource("pgm1").has_value());
    CHECK(!parseSource("in01").has_value());
    CHECK(!parseSource("Black").has_value());
    CHECK(sourceName(Source{Source::Kind::Me, 3}) == "me3");
    CHECK(sourceName(Source{Source::Kind::Player, 2}) == "cp2");
    CHECK(sourceName(Source{Source::Kind::Black, 0}) == "black");
}

TEST(mix_follows_the_grain_index)
{
    Mixer mixer(1, 8);
    CHECK(mixer.setProgram(1, in(1)) == MixResult::Ok);
    CHECK(mixer.setPreview(1, in(2)) == MixResult::Ok);
    CHECK(mixer.autoTransition(1, 0) == MixResult::BadFrames);
    CHECK(mixer.autoTransition(1, 4) == MixResult::Ok);
    CHECK(mixer.autoTransition(1, 4) == MixResult::Busy);
    auto p = mixer.frame(100).mes;
    CHECK(p[0].mixing);
    CHECK(p[0].t == 0.25f);
    CHECK(p[0].a == in(1));
    CHECK(p[0].b == in(2));
    // Grain 101 was skipped: the Mix still ends on time.
    p = mixer.frame(102).mes;
    CHECK(p[0].t == 0.75f);
    p = mixer.frame(103).mes;
    CHECK(p[0].mixing && p[0].t == 1.f);
    // After the last grain the buses have swapped.
    p = mixer.frame(104).mes;
    CHECK(!p[0].mixing);
    CHECK(p[0].a == in(2));
    CHECK(p[0].preview == in(1));
}

TEST(cut_swaps_and_ends_a_mix)
{
    Mixer mixer(1, 8);
    CHECK(mixer.cut(1) == MixResult::Ok);
    auto p = mixer.frame(1).mes;
    CHECK(p[0].a == in(2));
    CHECK(p[0].preview == in(1));
    CHECK(mixer.autoTransition(1, 50) == MixResult::Ok);
    mixer.frame(2);
    CHECK(mixer.view()[0].mixing);
    CHECK(mixer.cut(1) == MixResult::Ok);
    p = mixer.frame(3).mes;
    CHECK(!p[0].mixing);
    CHECK(p[0].a == in(1));
}

TEST(take_with_explicit_preview)
{
    // FlowXer's control plane sends the buses it wants: Program to the target, Preview after.
    Mixer mixer(1, 4, 1);
    mixer.setProgram(1, in(1));
    mixer.setPreview(1, in(2));
    // A take (no swap): Program in3, Preview stays in2.
    CHECK(mixer.take(1, in(3), 0, std::nullopt) == MixResult::Ok);
    auto p = mixer.frame(10).mes;
    CHECK(p[0].a == in(3) && p[0].preview == in(2) && !p[0].mixing);
    // Fade to Black: Program dissolves to black, Preview unchanged.
    Source const black{Source::Kind::Black, 0};
    CHECK(mixer.take(1, black, 2, std::nullopt) == MixResult::Ok);
    p = mixer.frame(11).mes;
    CHECK(p[0].mixing && p[0].a == in(3) && p[0].b == black && p[0].t == 0.5f);
    p = mixer.frame(12).mes;
    CHECK(p[0].t == 1.f);
    p = mixer.frame(13).mes;
    CHECK(!p[0].mixing && p[0].a == black && p[0].preview == in(2));
    // A Mix with a swap: Preview becomes the old Program at the end, not before.
    CHECK(mixer.take(1, in(2), 2, black) == MixResult::Ok);
    p = mixer.frame(14).mes;
    CHECK(p[0].mixing && p[0].preview == in(2));
    mixer.frame(15);
    p = mixer.frame(16).mes;
    CHECK(p[0].a == in(2) && p[0].preview == black);
    // A take during a Mix ends that Mix first (buses as at its end), then does its own.
    CHECK(mixer.take(1, Source{Source::Kind::Player, 1}, 10, in(2)) == MixResult::Ok);
    mixer.frame(17);
    CHECK(mixer.take(1, in(4), 0, in(1)) == MixResult::Ok);
    p = mixer.frame(18).mes;
    CHECK(!p[0].mixing && p[0].a == in(4) && p[0].preview == in(1));
    // A Preview chosen during a Mix wins over the swap at its end.
    CHECK(mixer.take(1, in(1), 2, in(4)) == MixResult::Ok);
    mixer.frame(19);
    CHECK(mixer.setPreview(1, in(3)) == MixResult::Ok);
    mixer.frame(20);
    p = mixer.frame(21).mes;
    CHECK(p[0].a == in(1) && p[0].preview == in(3));
    CHECK(mixer.take(1, in(9), 0, std::nullopt) == MixResult::BadSource);
    CHECK(mixer.take(1, in(1), 1001, std::nullopt) == MixResult::BadFrames);
}

TEST(transition_types_and_wipes)
{
    Mixer mixer(2, 4);
    mixer.setProgram(1, in(1));
    mixer.setPreview(1, in(2));
    // The ME's next transition: a circle wipe at 4 grains.
    TransitionSettings next;
    next.type = Transition::Wipe;
    next.frames = 4;
    next.wipe.pattern = 119;
    next.wipe.soft = 0.05f;
    CHECK(mixer.setTransition(1, next) == MixResult::Ok);
    CHECK(mixer.view()[0].next.type == Transition::Wipe && mixer.view()[0].next.wipe.pattern == 119);
    // Auto runs it over its rate, then the buses swap.
    CHECK(mixer.autoTransition(1) == MixResult::Ok);
    CHECK(mixer.autoTransition(1) == MixResult::Busy);
    // A change of the next transition does not touch the running one.
    next.wipe.pattern = 1;
    CHECK(mixer.setTransition(1, next) == MixResult::Ok);
    auto p = mixer.frame(10).mes;
    CHECK(p[1].me == 1 && p[1].mixing && p[1].wiping && p[1].wipe.pattern == 119 && p[1].t == 0.25f);
    CHECK(!p[0].mixing && !p[0].wiping);
    CHECK(mixer.view()[0].wiping && mixer.view()[0].frames == 4);
    p = mixer.frame(13).mes;
    CHECK(p[1].wiping && p[1].t == 1.f && p[1].b == in(2));
    p = mixer.frame(14).mes;
    CHECK(!p[1].mixing && !p[1].wiping && p[1].a == in(2) && p[1].preview == in(1));
    // An explicit rate overrides the ME's; the next wipe is the bar.
    CHECK(mixer.autoTransition(1, 2) == MixResult::Ok);
    p = mixer.frame(15).mes;
    CHECK(p[1].wiping && p[1].wipe.pattern == 1 && p[1].t == 0.5f);
    // A Cut during a wipe ends it as if it had run to its end (the target stays on Program).
    CHECK(mixer.cut(1) == MixResult::Ok);
    p = mixer.frame(16).mes;
    CHECK(!p[1].mixing && !p[1].wiping && p[1].a == in(1) && p[1].preview == in(2));
    // A take with a type: wipe, mix, cut; a stinger without a stinger player is a cut.
    CHECK(mixer.take(1, in(3), 2, std::nullopt, Transition::Wipe) == MixResult::Ok);
    p = mixer.frame(17).mes;
    CHECK(p[1].wiping && p[1].b == in(3));
    mixer.frame(18);
    CHECK(mixer.take(1, in(4), 2, std::nullopt, Transition::Mix) == MixResult::Ok);
    p = mixer.frame(19).mes;
    CHECK(p[1].mixing && !p[1].wiping && p[1].a == in(3) && p[1].b == in(4));
    CHECK(mixer.take(1, in(1), 25, std::nullopt, Transition::Stinger) == MixResult::Ok);
    p = mixer.frame(20).mes;
    CHECK(!p[1].mixing && p[1].a == in(1));
    CHECK(mixer.take(1, in(2), 25, std::nullopt, Transition::Cut) == MixResult::Ok);
    CHECK(mixer.frame(21).mes[1].a == in(2) && !mixer.view()[0].mixing);
    // A stinger as the next type: Auto has no stinger player, so it is a cut (buses swapped at the
    // next grain); FlowXer takes a stinger with its player.
    next.type = Transition::Stinger;
    CHECK(mixer.setTransition(1, next) == MixResult::Ok);
    mixer.setPreview(1, in(3));
    CHECK(mixer.autoTransition(1) == MixResult::Ok);
    p = mixer.frame(22).mes;
    CHECK(!p[1].mixing && p[1].a == in(3) && p[1].preview == in(2));
    // Checks.
    next.wipe.pattern = 7;
    CHECK(mixer.setTransition(1, next) == MixResult::BadPattern);
    next.wipe.pattern = 1;
    next.wipe.soft = 1.5f;
    CHECK(mixer.setTransition(1, next) == MixResult::BadPattern);
    next.wipe.soft = 0.f;
    next.frames = 0;
    CHECK(mixer.setTransition(1, next) == MixResult::BadFrames);
    next.frames = 25;
    CHECK(mixer.setTransition(3, next) == MixResult::BadMe);
    CHECK(parseTransition("wipe") == Transition::Wipe && !parseTransition("Wipe").has_value());
    CHECK(transitionName(Transition::Stinger) == "stinger");
}

TEST(tbar_position)
{
    Mixer mixer(1, 4);
    mixer.setProgram(1, in(1));
    mixer.setPreview(1, in(2));
    TransitionSettings next;
    next.type = Transition::Wipe;
    next.frames = 10;
    next.wipe.pattern = 101;
    CHECK(mixer.setTransition(1, next) == MixResult::Ok);
    // t = 0 without a transition: nothing.
    CHECK(mixer.position(1, 0.f) == MixResult::Ok);
    CHECK(!mixer.view()[0].mixing);
    CHECK(mixer.position(1, 1.5f) == MixResult::BadPosition);
    // t > 0 starts the ME's transition and holds it; skipped grains do not move it.
    CHECK(mixer.position(1, 0.3f) == MixResult::Ok);
    CHECK(mixer.view()[0].manual && mixer.view()[0].t == 0.3f);
    auto p = mixer.frame(100).mes;
    CHECK(p[0].mixing && p[0].wiping && p[0].t == 0.3f && p[0].tPrev == 0.f && p[0].a == in(1) && p[0].b == in(2));
    p = mixer.frame(101).mes;
    CHECK(p[0].t == 0.3f && p[0].tPrev == 0.3f);
    CHECK(mixer.position(1, 0.6f) == MixResult::Ok);
    p = mixer.frame(105).mes;
    CHECK(p[0].t == 0.6f && p[0].tPrev == 0.3f);
    // An Auto from the held position runs the rest (0.4 of 10 grains) from the next grain.
    CHECK(mixer.autoTransition(1) == MixResult::Ok);
    CHECK(mixer.position(1, 0.5f) == MixResult::Busy);
    p = mixer.frame(106).mes;
    CHECK(p[0].wiping && std::abs(p[0].t - 0.7f) < 1e-6f && p[0].tPrev == 0.6f);
    mixer.frame(107);
    mixer.frame(108);
    p = mixer.frame(109).mes;
    CHECK(p[0].t == 1.f);
    p = mixer.frame(110).mes;
    CHECK(!p[0].mixing && p[0].a == in(2) && p[0].preview == in(1));
    // t = 1 completes it after that grain.
    CHECK(mixer.position(1, 0.5f) == MixResult::Ok);
    mixer.frame(111);
    CHECK(mixer.position(1, 1.f) == MixResult::Ok);
    p = mixer.frame(112).mes;
    CHECK(p[0].mixing && p[0].t == 1.f && p[0].tPrev == 0.5f);
    p = mixer.frame(113).mes;
    CHECK(!p[0].mixing && p[0].a == in(1) && p[0].preview == in(2));
    // Back to t = 0 ends it without the swap.
    CHECK(mixer.position(1, 0.4f) == MixResult::Ok);
    mixer.frame(114);
    CHECK(mixer.position(1, 0.f) == MixResult::Ok);
    p = mixer.frame(115).mes;
    CHECK(p[0].mixing && p[0].t == 0.f && p[0].tPrev == 0.4f);
    p = mixer.frame(116).mes;
    CHECK(!p[0].mixing && p[0].a == in(1) && p[0].preview == in(2));
    // A Cut at a held position completes the transition.
    CHECK(mixer.position(1, 0.5f) == MixResult::Ok);
    CHECK(mixer.cut(1) == MixResult::Ok);
    p = mixer.frame(117).mes;
    CHECK(!p[0].mixing && p[0].a == in(2) && p[0].preview == in(1));
    // A stinger does not scrub: t > 0 starts it as an Auto (without a stinger player a cut).
    next.type = Transition::Stinger;
    CHECK(mixer.setTransition(1, next) == MixResult::Ok);
    CHECK(mixer.position(1, 0.2f) == MixResult::Ok);
    p = mixer.frame(118).mes;
    CHECK(!p[0].mixing && p[0].a == in(1));
}

TEST(stinger_cut_point_and_end)
{
    Mixer mixer(2, 4);
    mixer.setProgram(1, in(1));
    mixer.setPreview(1, in(2));
    std::vector<bool> const ready{true};
    std::vector<bool> const notReady{false};
    // A 6-frame stinger on player 1, cut at frame 3, the buses swapping at its end.
    CHECK(mixer.take(1, in(2), 0, in(1), Transition::Stinger, Sting{1, 6, 3}) == MixResult::Ok);
    // Its first frame is not decoded yet: it waits (A, no overlay), counted; Auto is busy.
    auto p = mixer.frame(100, notReady).mes[1];
    CHECK(p.stinger == 0 && p.background() == in(1) && mixer.stingerWaits() == 1 && mixer.stingersPlayed() == 0);
    CHECK(mixer.view()[0].mixing && mixer.view()[0].stinger == 1);
    CHECK(mixer.autoTransition(1) == MixResult::Busy);
    // Ready at grain 101: frame f in grain 101 + f, the background B from grain 104 (the cut).
    for (int f = 0; f < 6; ++f)
    {
        p = mixer.frame(101 + static_cast<std::uint64_t>(f), ready).mes[1];
        CHECK(p.stinger == 1 && p.stingerFrame == f && !p.mixing && p.a == in(1) && p.b == in(2));
        CHECK(p.background() == (f >= 3 ? in(2) : in(1)));
        CHECK(p.tPrev == stingerFadeT(f - 1, 3, 6) && p.t == stingerFadeT(f, 3, 6));
    }
    // After the last frame the buses have swapped.
    p = mixer.frame(107, ready).mes[1];
    CHECK(p.stinger == 0 && p.a == in(2) && p.preview == in(1) && !mixer.view()[0].mixing);
    // Skipped grains advance it: grain s + 4 shows frame 4, past the end it ends.
    CHECK(mixer.take(1, in(3), 0, std::nullopt, Transition::Stinger, Sting{1, 6, 2}) == MixResult::Ok);
    CHECK(mixer.frame(200, ready).mes[1].stingerFrame == 0);
    p = mixer.frame(204, ready).mes[1];
    CHECK(p.stingerFrame == 4 && p.cutDone && p.background() == in(3));
    p = mixer.frame(210, ready).mes[1];
    CHECK(p.stinger == 1 && p.stingerFrame == 5);
    CHECK(mixer.frame(211, ready).mes[1].a == in(3) && !mixer.view()[0].mixing);
    // A Cut during a stinger ends it as if it had run to its end; its overlay stops.
    CHECK(mixer.take(1, in(1), 0, in(3), Transition::Stinger, Sting{1, 10, 5}) == MixResult::Ok);
    mixer.frame(300, ready);
    CHECK(mixer.cut(1) == MixResult::Ok);
    p = mixer.frame(301, ready).mes[1];
    CHECK(p.stinger == 0 && p.a == in(1) && p.preview == in(3));
    // A player that never gets ready: a cut after kStingerWait grains.
    auto const waits = mixer.stingerWaits();
    CHECK(mixer.take(1, in(4), 0, std::nullopt, Transition::Stinger, Sting{1, 10, 5}) == MixResult::Ok);
    for (std::uint64_t k = 0; k < static_cast<std::uint64_t>(Mixer::kStingerWait); ++k)
    {
        CHECK(mixer.frame(400 + k, notReady).mes[1].a == in(1));
    }
    CHECK(mixer.stingerWaits() == waits + static_cast<std::uint64_t>(Mixer::kStingerWait) && mixer.stingerCuts() == 0);
    p = mixer.frame(400 + static_cast<std::uint64_t>(Mixer::kStingerWait), notReady).mes[1];
    CHECK(p.stinger == 0 && p.a == in(4) && mixer.stingerCuts() == 1 && !mixer.view()[0].mixing);
    // Played: the three stingers whose first frame went on air, not the one that became a cut.
    CHECK(mixer.stingersPlayed() == 3);
    // A cut point past the end is clamped to the length (B only after the swap).
    CHECK(mixer.take(1, in(2), 0, std::nullopt, Transition::Stinger, Sting{1, 3, 9}) == MixResult::Ok);
    CHECK(!mixer.frame(500, ready).mes[1].cutDone && !mixer.frame(502, ready).mes[1].cutDone);
    CHECK(mixer.frame(503, ready).mes[1].a == in(2));
}

TEST(stinger_background_sound)
{
    // The equal-power crossfade of the background sound runs over the frames cut − 2 … cut + 1.
    CHECK(stingerFadeT(-1, 10, 50) == 0.f && stingerFadeT(7, 10, 50) == 0.f);
    CHECK(stingerFadeT(8, 10, 50) == 0.25f && stingerFadeT(9, 10, 50) == 0.5f);
    CHECK(stingerFadeT(10, 10, 50) == 0.75f && stingerFadeT(11, 10, 50) == 1.f && stingerFadeT(30, 10, 50) == 1.f);
    // A cut at frame 0 starts the fade with the stinger; the last frame always ends on B.
    CHECK(stingerFadeT(0, 0, 50) == 0.75f && stingerFadeT(1, 0, 50) == 1.f);
    CHECK(stingerFadeT(47, 49, 50) == 0.25f && stingerFadeT(48, 49, 50) == 0.5f && stingerFadeT(49, 49, 50) == 1.f);
    // One grain at the cut ramps from 0.5 to 0.75 and ends where the next grain starts.
    std::vector<float> a(960, 1.f);
    std::vector<float> b(960, 0.f);
    std::vector<float> out(960);
    crossfadeBlock(a.data(), b.data(), out.data(), out.size(), stingerFadeT(9, 10, 50), stingerFadeT(10, 10, 50));
    CHECK(std::abs(out.back() - crossfade(0.75f).a) < 1e-6f && out.front() < crossfade(0.5f).a);
    // Outside the window the background is exactly A or B.
    crossfadeBlock(a.data(), b.data(), out.data(), out.size(), 0.f, 0.f);
    CHECK(out.front() == 1.f && out.back() == 1.f);
    crossfadeBlock(a.data(), b.data(), out.data(), out.size(), 1.f, 1.f);
    CHECK(out.front() == 0.f && out.back() == 0.f);
}

TEST(downstream_keyer_gain)
{
    Mixer mixer(2, 2, 0, {KeyerSpec{1, false}});
    CHECK(mixer.keyView().size() == 1u);
    auto f = mixer.frame(1);
    CHECK(f.keys.size() == 1u && f.keys[0].gain == 0.f && f.keys[0].me == 1 && !f.keys[0].upstream);
    CHECK(mixer.keySet(1, true) == MixResult::Ok);
    CHECK(mixer.frame(2).keys[0].gain == 1.f);
    CHECK(mixer.keySet(2, true) == MixResult::BadMe);
    // Auto off over 4 grains, following the grain index.
    CHECK(mixer.keyAuto(1, 4) == MixResult::Ok);
    CHECK(mixer.frame(3).keys[0].gain == 0.75f);
    CHECK(mixer.frame(5).keys[0].gain == 0.25f);
    // An auto during an auto turns back from where it is.
    CHECK(mixer.keyAuto(1, 4) == MixResult::Ok);
    CHECK(mixer.keyView()[0].on);
    CHECK(mixer.frame(6).keys[0].gain > 0.25f);
    float last = 0.f;
    for (std::uint64_t i = 7; i < 12; ++i)
    {
        last = mixer.frame(i).keys[0].gain;
    }
    CHECK(last == 1.f && !mixer.keyView()[0].transition);
    CHECK(mixer.keyAuto(1, 0) == MixResult::BadFrames);
}

TEST(keyers_per_me_and_luma_settings)
{
    // ME 2 and ME 1, each with an upstream keyer and a DSK, in the configuration's order.
    Mixer mixer(2, 4, 1, {KeyerSpec{1, true}, KeyerSpec{1, false}, KeyerSpec{2, true}, KeyerSpec{2, false}});
    auto f = mixer.frame(1);
    CHECK(f.keys.size() == 4u && f.keys[0].upstream && !f.keys[1].upstream && f.keys[2].me == 2);
    // A luma key of a clip player on ME 1's upstream keyer; ME 1 may key ME 2's Program too.
    KeySettings luma;
    luma.luma = true;
    luma.source = Source{Source::Kind::Player, 1};
    luma.clip = 0.3f;
    luma.soft = 0.1f;
    luma.gain = 0.8f;
    luma.invert = true;
    CHECK(mixer.keySettings(1, luma) == MixResult::Ok);
    CHECK(mixer.keySet(1, true) == MixResult::Ok);
    f = mixer.frame(2);
    CHECK(f.keys[0].gain == 1.f && f.keys[0].settings.luma && f.keys[0].settings.source == (Source{Source::Kind::Player, 1}));
    CHECK(f.keys[0].settings.invert && f.keys[0].settings.gain == 0.8f && f.keys[1].gain == 0.f);
    luma.source = Source{Source::Kind::Me, 2};
    CHECK(mixer.keySettings(1, luma) == MixResult::Ok);
    // ME 2 may not key ME 1 (no loops), nor a player that does not exist; values 0..1.
    CHECK(mixer.keySettings(3, luma) == MixResult::BadSource);
    luma.source = Source{Source::Kind::Player, 2};
    CHECK(mixer.keySettings(1, luma) == MixResult::BadSource);
    luma.source = Source{Source::Kind::Input, 1};
    luma.clip = 1.5f;
    CHECK(mixer.keySettings(1, luma) == MixResult::BadKey);
    // A linear key needs no source.
    CHECK(mixer.keySettings(4, KeySettings{}) == MixResult::Ok && !mixer.keyView()[3].settings.luma);
    CHECK(mixer.keySettings(5, KeySettings{}) == MixResult::BadMe);
}

TEST(keyers_pip_and_dme_settings)
{
    Mixer mixer(2, 4, 1, {KeyerSpec{1, true}, KeyerSpec{1, false}, KeyerSpec{2, true}});
    // A pip of input 2 on ME 1's upstream keyer: its box is always on.
    KeySettings pip;
    pip.pip = true;
    pip.source = Source{Source::Kind::Input, 2};
    pip.dme.x = 0.75f;
    pip.dme.size = 0.4f;
    CHECK(mixer.keySettings(1, pip) == MixResult::Ok);
    auto const view = mixer.keyView()[0];
    CHECK(view.settings.pip && view.settings.dme.enabled && view.settings.dme.x == 0.75f && view.settings.source == (Source{Source::Kind::Input, 2}));
    // A pip of ME 2 on ME 1 (re-entry), not of ME 1 on ME 2; no pip or box on a DSK.
    pip.source = Source{Source::Kind::Me, 2};
    CHECK(mixer.keySettings(1, pip) == MixResult::Ok);
    pip.source = Source{Source::Kind::Me, 1};
    CHECK(mixer.keySettings(3, pip) == MixResult::BadSource);
    pip.source = Source{Source::Kind::Input, 1};
    CHECK(mixer.keySettings(2, pip) == MixResult::BadDme);
    KeySettings linear;
    linear.dme.enabled = true;
    CHECK(mixer.keySettings(2, linear) == MixResult::BadDme);
    CHECK(mixer.keySettings(3, linear) == MixResult::Ok && mixer.keyView()[2].settings.dme.enabled);
    // Ranges, and pip and luma at once.
    linear.dme.size = 3.f;
    CHECK(mixer.keySettings(3, linear) == MixResult::BadDme);
    pip.luma = true;
    CHECK(mixer.keySettings(1, pip) == MixResult::BadKey);
}

TEST(luma_key_alpha)
{
    // Y 64 is 0, 940 is 1: clip 0.5 hard keys above the middle; soft 0.2 ramps over 0.5..0.7.
    CHECK(lumaAlpha(940, 0.5f, 0.f, false, 1.f) == 1.f);
    CHECK(lumaAlpha(64, 0.5f, 0.f, false, 1.f) == 0.f);
    CHECK(lumaAlpha(502, 0.5f, 0.f, false, 1.f) == 0.f);
    CHECK(lumaAlpha(503, 0.5f, 0.f, false, 1.f) == 1.f);
    CHECK(std::abs(lumaAlpha(64 + 876 * 6 / 10, 0.5f, 0.2f, false, 1.f) - 0.5f) < 0.01f);
    CHECK(lumaAlpha(940, 0.5f, 0.2f, false, 1.f) == 1.f);
    // Invert and gain.
    CHECK(lumaAlpha(940, 0.5f, 0.f, true, 1.f) == 0.f);
    CHECK(lumaAlpha(64, 0.5f, 0.f, true, 0.5f) == 0.5f);
}

TEST(fade_to_black_over_the_whole_output)
{
    Mixer mixer(2, 2, 0, {KeyerSpec{1, false}});
    CHECK(mixer.frame(1).mes[1].ftb == 0.f);
    // Auto over 4 grains on ME 1 only, following the grain index; the audio ramps from ftbPrev.
    CHECK(mixer.ftbAuto(1, 4) == MixResult::Ok);
    auto p = mixer.frame(2).mes;
    CHECK(p[1].me == 1 && p[1].ftb == 0.25f && p[1].ftbPrev == 0.f && p[0].ftb == 0.f);
    p = mixer.frame(5).mes;
    CHECK(p[1].ftb == 1.f && p[1].ftbPrev == 0.25f && mixer.ftbGain(1) == 1.f);
    // The buses are untouched; back up over 2 grains; on/off at once.
    CHECK(p[1].a == in(1) && !p[1].mixing);
    CHECK(mixer.ftbAuto(1, 2) == MixResult::Ok);
    CHECK(mixer.frame(6).mes[1].ftb == 0.5f);
    CHECK(mixer.frame(7).mes[1].ftb == 0.f);
    CHECK(mixer.ftbSet(2, true) == MixResult::Ok && mixer.frame(8).mes[0].ftb == 1.f);
    CHECK(mixer.ftbSet(3, true) == MixResult::BadMe && mixer.ftbAuto(1, 0) == MixResult::BadFrames);
}

// ---- mosaic map ------------------------------------------------------------------------

TEST(mosaic_tile_map)
{
    auto const tiles = mosaicTiles(1920, 1080, 8, 0, 4);
    CHECK_EQ(tiles.size(), 16u);
    std::set<std::string> ids;
    std::set<std::pair<int, int>> corners;
    for (auto const& t : tiles)
    {
        ids.insert(t.id);
        corners.insert({t.x, t.y});
        CHECK_EQ(t.w, 480);
        CHECK_EQ(t.h, 270);
        CHECK(t.x % 2 == 0 && t.y % 2 == 0);
        CHECK(t.x >= 0 && t.y >= 0 && t.x + t.w <= 1920 && t.y + t.h <= 1080);
    }
    // 16 different tiles on 16 different grid cells: they cover the canvas without overlap.
    CHECK_EQ(ids.size(), 16u);
    CHECK_EQ(corners.size(), 16u);
    CHECK(tiles[0].id == "in1" && tiles[0].x == 0 && tiles[0].y == 0);
    CHECK(tiles[7].id == "in8" && tiles[7].x == 1440 && tiles[7].y == 270);
    CHECK(tiles[8].id == "me1-pgm" && tiles[9].id == "me1-pvw");
    // The grid follows the sources: 4 cams, 2 players, 2 MEs = 10 tiles on 4x4.
    auto const small = mosaicTiles(1920, 1080, 4, 2, 2);
    CHECK_EQ(small.size(), 10u);
    CHECK(small[4].id == "cp1" && small[4].kind == MosaicTile::Kind::Player);
    CHECK_EQ(small[0].w, 480);
    // 1 input and 1 ME: 2x2 (960x540 tiles); 23 sources and 4 MEs: 6x6 (320x180).
    CHECK_EQ(mosaicTiles(1920, 1080, 1, 0, 1)[0].w, 960);
    auto const big = mosaicTiles(1920, 1080, 20, 3, 4);
    CHECK_EQ(big.size(), 31u);
    CHECK_EQ(big[0].w, 320);
    CHECK_EQ(big[0].h, 180);
    CHECK(big.back().x + big.back().w <= 1920 && big.back().y + big.back().h <= 1080);
    CHECK_EQ(mosaicGrid(36), 6);
    CHECK_EQ(mosaicGrid(37), 7);
}

// ---- TAI grid --------------------------------------------------------------------------

TEST(tai_index_arithmetic)
{
    for (Rate rate : {Rate{50, 1}, Rate{25, 1}, Rate{60000, 1001}})
    {
        for (std::uint64_t index : {1ull, 2ull, 88'000'000'000ull, 88'000'000'001ull})
        {
            CHECK_EQ(timestampToIndex(rate, indexToTimestamp(rate, index)), index);
            auto const start = windowStart(rate, index);
            CHECK_EQ(timestampToIndex(rate, start), index);
            CHECK_EQ(timestampToIndex(rate, start - 1), index - 1);
        }
    }
    CHECK_EQ(periodNs(Rate{50, 1}), 20'000'000ull);
    // Grain i is current from T(i) - 10 ms to T(i) + 10 ms at 50p (MXL rounds).
    CHECK_EQ(windowStart(Rate{50, 1}, 100), indexToTimestamp(Rate{50, 1}, 100) - 10'000'000ull);
    CHECK_EQ(inputIndexFor(100, 2), 98ull);
    CHECK_EQ(inputIndexFor(1, 2), 0ull);
}

TEST(latency_for_the_arrival_lag)
{
    // Grain i - L must be there half a grain before T(i): L >= lag + 0.5 (+ 0.1 margin).
    CHECK_EQ(latencyFor(0.0), 1);
    CHECK_EQ(latencyFor(0.3), 1);
    CHECK_EQ(latencyFor(0.45), 2);
    CHECK_EQ(latencyFor(1.2), 2);
    CHECK_EQ(latencyFor(1.5), 3);
    // An input up to 5.2 grains late (small's RDMA mirrors): L = 6.
    CHECK_EQ(latencyFor(5.2), 6);
    CHECK_EQ(latencyFor(-1.0), 1);
}

TEST(recommended_latency_counts_what_delivers)
{
    // small's test-all (1080p50, 20 ms grains): Cameras 1-3 routed, Camera 4 not, two clip
    // players, ME 1 Key 1/2 and DSK 1 Key without a key flow, an empty stinger player. Only what
    // delivers counts: the routed cameras' video and running audio. A large peak of an unrouted
    // input, of a key flow without grains or of a still audio head (no_signal) does not.
    auto status = nlohmann::json::parse(R"({
        "inputs": [
            {"input": 1, "state": "running", "arrival_ms": {"avg": 31.0, "max": 36.0, "peak": 55.0},
             "audio": {"state": "running", "lag_ms": {"avg": 20.0, "max": 30.0, "peak": 41.0}}},
            {"input": 2, "state": "running", "arrival_ms": {"avg": 29.0, "max": 35.0, "peak": 50.0},
             "audio": {"state": "no_signal", "lag_ms": {"avg": 800.0, "max": 1000.0, "peak": 1000.0}}},
            {"input": 3, "state": "running", "arrival_ms": {"avg": 33.0, "max": 36.0, "peak": 52.0},
             "audio": {"state": "running", "lag_ms": {"avg": 20.0, "max": 30.0, "peak": 33.0}}},
            {"input": 4, "state": "not_routed", "arrival_ms": {"avg": 0.0, "max": 0.0, "peak": 540.0},
             "audio": {"state": "not_routed", "lag_ms": {"avg": 0.0, "max": 0.0, "peak": 540.0}}}
        ],
        "keys": [
            {"key": 1, "label": "ME 1 Key 1", "state": "not_routed", "flow_slot": null},
            {"key": 2, "label": "ME 1 Key 2", "state": "not_routed", "flow_slot": null},
            {"key": 3, "label": "DSK 1 Key", "state": "flow_not_found", "flow_slot": 1,
             "arrival_ms": {"avg": 0.0, "max": 0.0, "peak": 540.0}}
        ],
        "key_flows": [{"key": 1, "name": "key1", "state": "flow_not_found", "arrival_ms": {"avg": 0.0, "max": 0.0, "peak": 540.0}}],
        "players": [{"player": 1, "state": "empty"}, {"player": 2, "state": "empty"}],
        "stingers": {"players": [{"player": 1, "label": "Stinger 1", "state": "empty", "ahead": 0, "late": 0}]}
    })");
    // 55 ms = 2.75 grains: L = 4.
    CHECK_EQ(recommendedLatency(status, 20.0), 4);
    // A key flow that runs counts like an input, in ms: 70 ms = 3.5 grains, L = 5.
    status["key_flows"][0]["state"] = "running";
    status["key_flows"][0]["arrival_ms"]["peak"] = 70.0;
    CHECK_EQ(recommendedLatency(status, 20.0), 5);
    // Audio that runs counts too.
    status["inputs"][1]["audio"]["state"] = "running";
    CHECK_EQ(recommendedLatency(status, 20.0), 51);
    // Nothing that delivers: 1.
    CHECK_EQ(recommendedLatency(nlohmann::json::object(), 20.0), 1);
}

TEST(skip_on_late_never_slides)
{
    // Start: the current grain, nothing counted.
    auto s = nextOutputIndex(0, 500);
    CHECK_EQ(s.index, 500ull);
    CHECK_EQ(s.skipped, 0ull);
    // On time: the next grain (its window has not started yet).
    s = nextOutputIndex(501, 500);
    CHECK_EQ(s.index, 501ull);
    CHECK_EQ(s.skipped, 0ull);
    s = nextOutputIndex(501, 501);
    CHECK_EQ(s.index, 501ull);
    // Late by three grains: render the current one, count three skipped.
    s = nextOutputIndex(501, 504);
    CHECK_EQ(s.index, 504ull);
    CHECK_EQ(s.skipped, 3ull);
    // The clock stepped back: restart at the current grain.
    s = nextOutputIndex(900, 500);
    CHECK_EQ(s.index, 500ull);
    CHECK_EQ(s.skipped, 0ull);
}

// ---- configuration ---------------------------------------------------------------------

TEST(config_defaults_and_errors)
{
    auto const cfg = loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs}});
    CHECK_EQ(cfg.inputs.size(), 2u);
    CHECK(cfg.inputs[0].label == "Cam 1");
    CHECK(cfg.inputs[1].label == "Input 2");
    CHECK_EQ(cfg.mes, 1);
    CHECK_EQ(cfg.latency, 4);
    CHECK(cfg.pvwOutputs);
    CHECK_EQ(cfg.width, 1920);
    CHECK_EQ(cfg.rate.num, 50);
    CHECK_EQ(cfg.httpPort, 9630);
    CHECK(cfg.httpBind == "127.0.0.1");
    CHECK(cfg.workFormat == "yuv16");
    CHECK_EQ(cfg.audioWaitMs, 8);
    auto const other = loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_FORMAT", "1080p5994"}, {"FLOWXER_ENGINE_MES", "2"},
        {"FLOWXER_ENGINE_PVW_OUTPUTS", "false"}, {"FLOWXER_ENGINE_LATENCY", "6"}});
    CHECK_EQ(other.rate.num, 60000);
    CHECK_EQ(other.rate.den, 1001);
    CHECK_EQ(other.mes, 2);
    CHECK_EQ(other.latency, 6);
    CHECK(!other.pvwOutputs);
    // No inputs at all is fine (clip players and Black only, or a production being set up).
    auto const empty = loadConfig({});
    CHECK(empty.inputs.empty() && empty.players.empty());
    // An input without a flow is not routed yet.
    CHECK(loadConfig({{"FLOWXER_ENGINE_INPUTS", R"([{"domain":"/x"}])"}}).inputs[0].video.flow.empty());
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_MES", "5"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_LATENCY", "two"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_LATENCY", "11"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_FORMAT", "1080i50"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_WORK_FORMAT", "rgb8"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", "{}"}}));
}

TEST(sources_players_and_keys)
{
    std::string many = "[";
    for (int i = 0; i < 20; ++i)
    {
        many += std::string(i ? "," : "") + "{\"label\":\"Cam " + std::to_string(i + 1) + "\"}";
    }
    many += "]";
    auto const cfg = loadConfig({{"FLOWXER_ENGINE_INPUTS", many}, {"FLOWXER_ENGINE_PLAYERS", R"([{"label":"Clip 1"},{},{}])"},
        {"FLOWXER_ENGINE_KEYS", R"([{"label":"DSK 1 Key","domain":"/Volumes/mxl/bs","flow":"k1"}])"}});
    CHECK_EQ(cfg.inputs.size(), 20u);
    CHECK_EQ(cfg.players.size(), 3u);
    CHECK(cfg.players[1].label == "Clip 2");
    CHECK_EQ(cfg.keys.size(), 1u);
    CHECK(cfg.keys[0].route.flow == "k1" && cfg.keys[0].me == 1 && !cfg.keys[0].upstream);
    auto const two = loadConfig({{"FLOWXER_ENGINE_MES", "2"}, {"FLOWXER_ENGINE_KEYS", R"([{"label":"ME 2 Key 1","me":2,"stage":"upstream"},{"me":2}])"}});
    CHECK(two.keys[0].upstream && two.keys[0].me == 2 && !two.keys[1].upstream);
    CHECK(configThrows({{"FLOWXER_ENGINE_KEYS", R"([{"stage":"middle"}])"}}));
    // 24 sources with Black at most.
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", many}, {"FLOWXER_ENGINE_PLAYERS", R"([{},{},{},{}])"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_KEYS", R"([{"flow":"k1"}])"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_KEYS", R"([{"me":2}])"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_PLAYERS", R"(["x"])"}}));
    // Stinger players: none unless FlowXer asks for them, at most 4.
    CHECK_EQ(cfg.stingerPlayers, 0);
    CHECK_EQ(loadConfig({{"FLOWXER_ENGINE_STINGER_PLAYERS", "2"}}).stingerPlayers, 2);
    CHECK(configThrows({{"FLOWXER_ENGINE_STINGER_PLAYERS", "5"}}));
}

TEST(preview_contract)
{
    // Without PREVIEW_PUBLISH_URL: the own MediaMTX on localhost, streams under the own name.
    auto const own = loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs}});
    CHECK(own.ownMediamtx());
    CHECK(own.rtspBase() == "rtsp://127.0.0.1:8654");
    CHECK(own.streamPath("mosaic") == "flowxer/mosaic");
    auto const shared = loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs},
        {"PREVIEW_PUBLISH_URL", "rtsp://mxl-mediamtx.mxl-platform.svc:8554/"},
        {"PREVIEW_PATH_PREFIX", "/show/vmix1/"},
        {"PREVIEW_WHEP_URL", "https://preview.example/whep"}});
    CHECK(!shared.ownMediamtx());
    CHECK(shared.rtspBase() == "rtsp://mxl-mediamtx.mxl-platform.svc:8554");
    CHECK(shared.streamPath("mosaic") == "show/vmix1/mosaic");
    CHECK(shared.whepBase == "https://preview.example/whep");
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"PREVIEW_PUBLISH_URL", "http://x:8554"}}));
}

// ---- outputs and audio -------------------------------------------------------------------

TEST(outputs_and_input_audio)
{
    std::string const inputs =
        R"([{"domain":"/d","flow":"v1","audio":"a1","label":"Cam 1"},{"label":"black"},{"domain":"/d","flow":"v3","audio":"a3","audio_domain":"/e"}])";
    auto const cfg = loadConfig({{"FLOWXER_ENGINE_INPUTS", inputs},
        {"FLOWXER_ENGINE_MES", "2"},
        {"FLOWXER_ENGINE_OUTPUT_DOMAIN", "/Volumes/mxl/out"},
        {"FLOWXER_ENGINE_OUTPUTS",
            R"([{"me":1,"domain":"/Volumes/mxl/vmix1","domain_id":"fba22e51-0000-4000-8000-000000000000","pgm_video":"3924b34c-4532-57ab-8e21-cb87bccfa4e3","pgm_audio":"5ac3feb3-0d22-5835-8b07-fe175fc4d640","label":"ME 1","group":"me-1"}])"}});
    CHECK(cfg.inputs[0].audio.flow == "a1" && cfg.inputs[0].audio.domain == "/d");
    CHECK(cfg.inputs[1].video.flow.empty() && cfg.inputs[1].label == "black");
    CHECK(cfg.inputs[2].audio.domain == "/e");
    auto const named = loadConfig({{"FLOWXER_ENGINE_INPUTS", R"([{"domain":"/d","video_flow":"v","audio_flow":"a","label":"Cam"}])"}});
    CHECK(named.inputs[0].video.flow == "v" && named.inputs[0].audio.flow == "a");
    CHECK_EQ(named.holdGrains, 0);
    CHECK_EQ(cfg.outputs.size(), 2u);
    CHECK(cfg.outputs[0].domain == "/Volumes/mxl/vmix1" && cfg.outputs[0].pgmVideo == "3924b34c-4532-57ab-8e21-cb87bccfa4e3");
    CHECK(cfg.outputs[0].pgmAudio == "5ac3feb3-0d22-5835-8b07-fe175fc4d640" && cfg.outputs[0].pvwVideo.empty());
    CHECK(cfg.outputs[0].label == "ME 1" && cfg.outputs[0].group == "me-1");
    CHECK(cfg.outputs[1].domain == "/Volumes/mxl/out" && cfg.outputs[1].pgmVideo.empty());
    CHECK_EQ(cfg.audioChannels, 2);
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", R"([{"flow":"v1"}])"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_OUTPUTS", R"([{"me":5}])"}}));
    // v210a key plane: three 10-bit samples per word, whole words per line.
    CHECK_EQ(keyPlaneRowBytes(1920), 2560);
    CHECK_EQ(keyPlaneRowBytes(1280), 1708);
}

TEST(audio_crossfade)
{
    // Equal power: a² + b² = 1 all through the Mix.
    for (int k = 0; k <= 10; ++k)
    {
        auto const g = crossfade(static_cast<float>(k) / 10.f);
        CHECK(std::abs(g.a * g.a + g.b * g.b - 1.f) < 1e-5f);
    }
    CHECK(crossfade(0.f).a == 1.f && crossfade(0.f).b == 0.f);
    CHECK(crossfade(1.f).a == 0.f && crossfade(1.f).b == 1.f);
    // One grain of a 4-grain Mix ramps from 0.25 to 0.5; the next starts where it ended.
    std::vector<float> a(960, 1.f);
    std::vector<float> b(960, 0.f);
    std::vector<float> out(960);
    crossfadeBlock(a.data(), b.data(), out.data(), out.size(), 0.25f, 0.5f);
    CHECK(std::abs(out.back() - crossfade(0.5f).a) < 1e-6f);
    CHECK(out.front() < crossfade(0.25f).a && out.front() > crossfade(0.5f).a);
    for (std::size_t s = 1; s < out.size(); ++s)
    {
        CHECK(out[s] <= out[s - 1]);
    }
    // 48 kHz samples on the video grid: 960 per grain at 50p, the same sample index as MXL.
    auto const r = grainSamples(Rate{50, 1}, 1000);
    CHECK_EQ(r.first, 960000ull);
    CHECK_EQ(r.end - r.first, 960ull);
    std::uint64_t total = 0;
    for (std::uint64_t i = 0; i < 5; ++i)
    {
        auto const g = grainSamples(Rate{60000, 1001}, 1000 + i);
        total += g.end - g.first;
    }
    CHECK_EQ(total, 4004ull);
}

// ---- clip player transport -------------------------------------------------------------

TEST(playout_cue_play_pause_stop)
{
    Playout p;
    CHECK(p.advance(1, false).frame == -1);
    p.load(4, false, false);
    CHECK(p.state() == Playout::State::Cued);
    auto s = p.advance(10, false);
    CHECK(s.frame == 0 && !s.playing);
    p.play();
    CHECK(p.advance(11, false).frame == 0);
    s = p.advance(12, false);
    CHECK(s.frame == 1 && s.playing);
    p.pause();
    // Paused: the last frame shown holds.
    CHECK(p.advance(13, false).frame == 1 && !p.advance(14, false).playing);
    p.play();
    CHECK(p.advance(15, false).frame == 2);
    s = p.advance(16, false);
    CHECK(s.frame == 3 && s.seq == 3 && s.generation == p.cursor().generation);
    // The end without loop: the last frame holds, the state is ended.
    s = p.advance(17, false);
    CHECK(s.frame == 3 && !s.playing && p.state() == Playout::State::Ended);
    // Play from the end starts over.
    auto const before = p.cursor().generation;
    p.play();
    CHECK(p.cursor().generation > before && p.cursor().start == 0);
    CHECK(p.advance(18, false).frame == 0);
    p.cue(2);
    s = p.advance(19, false);
    CHECK(p.state() == Playout::State::Cued && s.frame == 2 && s.seq == 0 && p.cursor().start == 2);
    p.play();
    s = p.advance(20, false);
    CHECK(s.frame == 2 && s.seq == 0);
    s = p.advance(21, false);
    CHECK(s.frame == 3 && s.seq == 1 && p.cursor().start == 2);
    p.cue(99);
    CHECK(p.advance(22, false).frame == 3);
    p.stop();
    CHECK(p.state() == Playout::State::Cued && p.advance(23, false).frame == 0);
    p.unload();
    CHECK(p.advance(24, false).frame == -1 && p.cursor().empty);
}

TEST(playout_loop_skips_and_play_on_cut)
{
    Playout p;
    p.load(3, true, true);
    // Not on air: holds the first frame.
    CHECK(p.advance(100, false).frame == 0);
    // Taken to Program: plays from the cued frame.
    auto s = p.advance(101, true);
    CHECK(s.frame == 0 && s.playing && p.state() == Playout::State::Playing);
    CHECK(p.advance(102, true).frame == 1);
    CHECK(p.advance(103, true).frame == 2);
    // Loop: on to the first frame without a break.
    s = p.advance(104, true);
    CHECK(s.frame == 0 && s.playing);
    // Two grains skipped by the render thread: the clip stays on time (0 -> 3 = frame 0 again).
    s = p.advance(107, true);
    CHECK(s.frame == 0 && s.seq == 6);
    // Still on air: no new start; pause and going on air again resumes.
    p.pause();
    CHECK(p.advance(108, false).frame == 0);
    CHECK(p.advance(109, true).playing);
    // Without play on cut, going on air does not start it.
    Playout q;
    q.load(5, false, false);
    CHECK(!q.advance(1, true).playing && q.state() == Playout::State::Cued);
    // A still (1 frame) plays and holds its only frame.
    Playout still;
    still.load(1, false, false);
    still.play();
    CHECK(still.advance(1, false).frame == 0 && still.advance(2, false).frame == 0);
    CHECK(still.state() == Playout::State::Ended);
    // Without loop a skip past the end ends the clip on its last frame.
    Playout r;
    r.load(3, false, false);
    r.play();
    r.advance(1, false);
    s = r.advance(9, false);
    CHECK(s.frame == 2 && s.seq == 2 && !s.playing && r.state() == Playout::State::Ended);
    CHECK(std::string(stateName(Playout::State::Paused)) == "paused");
}
