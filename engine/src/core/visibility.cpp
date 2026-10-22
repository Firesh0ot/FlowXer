#include "core/visibility.hpp"

#include <algorithm>

namespace fxeng
{
namespace
{
// The picture's rectangle on the raster (fractions): the uncropped picture is `size` of the
// raster around (x, y); the crop cuts fractions of it away without moving it.
struct Rect
{
    float l, r, t, b;
};

Rect pictureRect(DmeSettings const& d)
{
    float const half = d.size / 2.f;
    return Rect{d.x - half + d.cropLeft * d.size, d.x + half - d.cropRight * d.size, d.y - half + d.cropTop * d.size,
        d.y + half - d.cropBottom * d.size};
}

// Less than this (a fraction of the raster, ~0.2 px of 1920) shows nothing.
constexpr float kMinShown = 1e-4f;
} // namespace

bool boxShows(DmeSettings const& dme)
{
    if (dme.size <= 0.f)
    {
        return false;
    }
    auto const p = pictureRect(dme);
    float const w = std::min(p.r, 1.f) - std::max(p.l, 0.f);
    float const h = std::min(p.b, 1.f) - std::max(p.t, 0.f);
    return w > kMinShown && h > kMinShown;
}

bool boxCovers(DmeSettings const& dme)
{
    auto const p = pictureRect(dme);
    return dme.soft <= 0.f && p.l <= 0.f && p.r >= 1.f && p.t <= 0.f && p.b >= 1.f;
}

KeyVisibility keyVisibility(int me, std::vector<KeyPlan> const& keys)
{
    KeyVisibility out;
    // From the top down: a covering pip hides every layer under it and the background.
    std::vector<KeyPlan const*> layers;
    for (auto const& key : keys)
    {
        if (key.me == me)
        {
            layers.push_back(&key);
        }
    }
    // DSKs are over the upstream keys (the configuration lists them after them).
    std::stable_partition(layers.begin(), layers.end(), [](KeyPlan const* k) { return k->upstream; });
    std::vector<Source> found;
    bool covered = false;
    for (auto it = layers.rbegin(); it != layers.rend() && !covered; ++it)
    {
        auto const& key = **it;
        auto const& s = key.settings;
        if (key.gain <= 0.f || s.gain <= 0.f)
        {
            continue;
        }
        bool const boxed = key.upstream && (s.pip || s.dme.enabled);
        if (boxed && !boxShows(s.dme))
        {
            continue;
        }
        if ((s.pip || s.luma) && s.source.kind != Source::Kind::Black &&
            std::find(found.begin(), found.end(), s.source) == found.end())
        {
            found.push_back(s.source);
        }
        covered = s.pip && boxed && key.gain >= 1.f && s.gain >= 1.f && boxCovers(s.dme);
    }
    std::reverse(found.begin(), found.end());
    out.keyed = std::move(found);
    out.backgroundCovered = covered;
    return out;
}

void markVisibility(FramePlan& plan)
{
    for (auto& me : plan.mes)
    {
        auto v = keyVisibility(me.me, plan.keys);
        me.keyed = std::move(v.keyed);
        me.backgroundCovered = v.backgroundCovered;
    }
}
} // namespace fxeng
