// What keys show on Program (core/visibility.hpp) and audio that follows it: a pip's or luma
// key's source is heard (AFV) while it is visible; hidden boxes (size 0, off screen, cropped away,
// gain 0, covered by a full-raster pip) are not; a full-raster pip covers the background; nested
// MEs (a pip of an ME that has a pip) are heard through each ME's own mix.
#include "test.hpp"

#include "core/audiomix.hpp"
#include "core/mixer.hpp"
#include "core/visibility.hpp"

#include <algorithm>
#include <cmath>

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

MePlan still(int m, Source a)
{
    MePlan p;
    p.me = m;
    p.a = a;
    p.b = a;
    return p;
}

DmeSettings box(float x, float y, float size)
{
    DmeSettings d;
    d.enabled = true;
    d.x = x;
    d.y = y;
    d.size = size;
    return d;
}

KeyPlan pip(int me, Source source, DmeSettings dme, float gain = 1.f)
{
    KeyPlan k;
    k.me = me;
    k.upstream = true;
    k.gain = gain;
    k.settings.pip = true;
    k.settings.source = source;
    k.settings.dme = dme;
    return k;
}

KeyPlan luma(int me, Source source, bool upstream, float gain = 1.f)
{
    KeyPlan k;
    k.me = me;
    k.upstream = upstream;
    k.gain = gain;
    k.settings.luma = true;
    k.settings.source = source;
    return k;
}

bool has(std::vector<Source> const& list, Source s)
{
    return std::find(list.begin(), list.end(), s) != list.end();
}

std::vector<float> flat(std::size_t n, float v)
{
    return std::vector<float>(n, v);
}
} // namespace

TEST(visibility_boxes)
{
    CHECK(boxShows(box(0.5f, 0.5f, 0.5f)));
    CHECK(!boxShows(box(0.5f, 0.5f, 0.f)));     // size 0
    CHECK(!boxShows(box(-0.3f, 0.5f, 0.5f)));   // off screen left
    CHECK(!boxShows(box(0.5f, 1.4f, 0.5f)));    // off screen below
    CHECK(boxShows(box(-0.2f, 0.5f, 0.5f)));    // half on screen
    auto cropped = box(0.5f, 0.5f, 0.5f);
    cropped.cropLeft = 0.6f;
    cropped.cropRight = 0.39999f; // a sliver of 1e-5 of the picture
    CHECK(!boxShows(cropped));
    CHECK(boxCovers(box(0.5f, 0.5f, 1.f)));
    CHECK(!boxCovers(box(0.5f, 0.5f, 0.99f)));
    auto soft = box(0.5f, 0.5f, 1.f);
    soft.soft = 4.f;
    CHECK(!boxCovers(soft));
    auto croppedFull = box(0.5f, 0.5f, 1.f);
    croppedFull.cropTop = 0.1f;
    CHECK(!boxCovers(croppedFull));
}

TEST(visibility_keys)
{
    // A pip of in3 and a luma key of cp1 on ME 1, a pip of in4 on ME 2.
    std::vector<KeyPlan> keys{pip(1, in(3), box(0.75f, 0.25f, 0.25f)), luma(1, cp(1), true), pip(2, in(4), box(0.5f, 0.5f, 0.5f))};
    auto v = keyVisibility(1, keys);
    CHECK(v.keyed.size() == 2 && v.keyed[0] == in(3) && v.keyed[1] == cp(1) && !v.backgroundCovered);
    CHECK(keyVisibility(2, keys).keyed.size() == 1 && keyVisibility(2, keys).keyed[0] == in(4));
    // Off (gain 0), the keyer's own gain 0, off screen, size 0: hidden.
    keys[0].gain = 0.f;
    CHECK(!has(keyVisibility(1, keys).keyed, in(3)));
    keys[0].gain = 0.5f; // fading: visible
    CHECK(has(keyVisibility(1, keys).keyed, in(3)));
    keys[0].settings.gain = 0.f;
    CHECK(!has(keyVisibility(1, keys).keyed, in(3)));
    keys[0].settings.gain = 1.f;
    keys[0].settings.dme = box(1.5f, 0.5f, 0.5f);
    CHECK(!has(keyVisibility(1, keys).keyed, in(3)));
    keys[0].settings.dme = box(0.5f, 0.5f, 0.f);
    CHECK(!has(keyVisibility(1, keys).keyed, in(3)));
    // A full-raster pip on top covers the background and every layer under it.
    keys[0].settings.dme = box(0.5f, 0.5f, 0.25f);
    keys.push_back(pip(1, in(5), box(0.5f, 0.5f, 1.f)));
    v = keyVisibility(1, keys);
    CHECK(v.backgroundCovered && v.keyed.size() == 1 && v.keyed[0] == in(5));
    // ... not while it fades (gain < 1) or with a soft edge.
    keys.back().gain = 0.9f;
    v = keyVisibility(1, keys);
    CHECK(!v.backgroundCovered && v.keyed.size() == 3);
    keys.back().gain = 1.f;
    // A DSK is over the upstream keys: a luma DSK stays visible over the covering pip.
    keys.push_back(luma(1, in(6), false));
    v = keyVisibility(1, keys);
    CHECK(v.backgroundCovered && v.keyed.size() == 2 && has(v.keyed, in(5)) && has(v.keyed, in(6)));
    // A linear key shows no source; Black is no source.
    KeyPlan linear;
    linear.me = 3;
    linear.upstream = true;
    linear.gain = 1.f;
    CHECK(keyVisibility(3, {linear, pip(3, Source{}, box(0.5f, 0.5f, 0.5f))}).keyed.empty());
}

TEST(visibility_in_the_frame_plan)
{
    // ME 1 upstream keyers 1, 2 and DSK 3; ME 2 upstream keyer 4.
    Mixer mixer(2, 4, 1, {KeyerSpec{1, true}, KeyerSpec{1, true}, KeyerSpec{1, false}, KeyerSpec{2, true}});
    mixer.setProgram(2, in(1));
    mixer.setProgram(1, in(2));
    KeySettings p;
    p.pip = true;
    p.source = in(3);
    p.dme = box(0.75f, 0.75f, 0.25f);
    CHECK(mixer.keySettings(4, p) == MixResult::Ok); // ME 2: pip of in3
    p.source = me(2);
    CHECK(mixer.keySettings(1, p) == MixResult::Ok); // ME 1: pip of ME 2
    auto plan = mixer.frame(1);
    CHECK(plan.mes[0].me == 2 && plan.mes[1].me == 1);
    CHECK(plan.mes[0].keyed.empty() && plan.mes[1].keyed.empty()); // keyers off
    mixer.keySet(4, true);
    mixer.keySet(1, true);
    plan = mixer.frame(2);
    CHECK(plan.mes[0].keyed.size() == 1 && plan.mes[0].keyed[0] == in(3));
    CHECK(plan.mes[1].keyed.size() == 1 && plan.mes[1].keyed[0] == me(2));
}

TEST(audio_follows_visible_keys)
{
    std::size_t const n = 480;
    AudioMix mix(1, 3, 0, 1);
    auto const a = flat(n, 0.5f);
    auto const b = flat(n, 0.25f);
    auto const c = flat(n, 0.125f);
    MixSources sources{{a.data(), b.data(), c.data()}, {}, {}};
    std::vector<std::vector<float>> out;
    MePlan p = still(1, in(1));
    mix.grain({p}, sources, n, out);
    CHECK(out[0][n - 1] == 0.5f);
    // A pip of in2 goes on air: in2 comes in over one grain (AFV on), Program stays.
    p.keyed = {in(2)};
    mix.grain({p}, sources, n, out);
    CHECK(out[0][0] < 0.5f + 0.25f && out[0][n - 1] == 0.5f + 0.25f);
    CHECK(mix.stripStates(1)[1].heard);
    mix.grain({p}, sources, n, out);
    CHECK(out[0][0] == 0.75f);
    // In a Mix to in3 the pip's source stays at full level; A and B crossfade.
    MePlan m = p;
    m.b = in(3);
    m.mixing = true;
    m.tPrev = 0.4f;
    m.t = 0.5f;
    mix.grain({m}, sources, n, out);
    auto const half = crossfade(0.5f);
    CHECK(std::abs(out[0][n - 1] - (0.5f * half.a + 0.125f * half.b + 0.25f)) < 1e-6f);
    // The pip's source is also the Mix's B: heard once, in full.
    m.b = in(2);
    mix.grain({m}, sources, n, out);
    mix.grain({m}, sources, n, out);
    CHECK(std::abs(out[0][n - 1] - (0.5f * half.a + 0.25f)) < 1e-6f);
    // A full-raster pip covers the background: Program's sound goes out, the pip's stays.
    p.backgroundCovered = true;
    mix.grain({p}, sources, n, out);
    mix.grain({p}, sources, n, out);
    CHECK(out[0][0] == 0.25f && !mix.stripStates(1)[0].heard && mix.stripStates(1)[1].heard);
    // The pip hidden again: back to Program only.
    p.keyed.clear();
    p.backgroundCovered = false;
    mix.grain({p}, sources, n, out);
    mix.grain({p}, sources, n, out);
    CHECK(out[0][0] == 0.5f && !mix.stripStates(1)[1].heard);
    // heardInputs counts a key's source.
    p.keyed = {in(3)};
    auto const heard = mix.heardInputs({p});
    CHECK(heard[0] && heard[2]);
}

TEST(audio_follows_nested_mes)
{
    // ME 3: Program cp1 with a pip of in3. ME 2: Program in2 with a pip of ME 3. ME 1: Program
    // in1 with a pip of ME 2. ME 1 hears in1 + ME 2 (in2 + ME 3 (cp1 + in3)).
    std::size_t const n = 480;
    AudioMix mix(3, 3, 1, 1);
    auto const i1 = flat(n, 1.f / 2);
    auto const i2 = flat(n, 1.f / 4);
    auto const i3 = flat(n, 1.f / 8);
    auto const c1 = flat(n, 1.f / 16);
    MixSources sources{{i1.data(), i2.data(), i3.data()}, {c1.data()}, {}};
    std::vector<std::vector<float>> out;
    MePlan m3 = still(3, cp(1));
    m3.keyed = {in(3)};
    MePlan m2 = still(2, in(2));
    m2.keyed = {me(3)};
    MePlan m1 = still(1, in(1));
    m1.keyed = {me(2)};
    std::vector<MePlan> plans{m3, m2, m1};
    mix.grain(plans, sources, n, out);
    mix.grain(plans, sources, n, out);
    CHECK(out[2][0] == 1.f / 16 + 1.f / 8);
    CHECK(out[1][0] == 1.f / 4 + out[2][0]);
    CHECK(out[0][0] == 1.f / 2 + out[1][0]);
    // ME 3's pip hidden (e.g. its box flew off): in3 leaves ME 3, so ME 2 and ME 1 too.
    plans[0].keyed.clear();
    mix.grain(plans, sources, n, out);
    mix.grain(plans, sources, n, out);
    CHECK(out[0][0] == 1.f / 2 + 1.f / 4 + 1.f / 16);
    // ME 2 not shown on ME 1 any more: ME 1 hears in1 only.
    plans[2].keyed.clear();
    mix.grain(plans, sources, n, out);
    mix.grain(plans, sources, n, out);
    CHECK(out[0][0] == 1.f / 2);
}
