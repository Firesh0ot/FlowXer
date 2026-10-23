// What a key shows on its ME's Program in one grain (tally and audio follow video): the source
// of a luma key or a pip is visible while the keyer's gain is above 0, its box (when it has one)
// shows some of the picture on the raster, and no opaque full-raster pip above it covers it. Such
// a pip also covers the background. A linear key's fill is no source of the mixer. Only a pip
// whose picture covers the whole raster at gain 1 without a soft edge counts as covering
// (partly covered layers stay visible). docs/engine-p5.md "Audio follows what is visible".
#pragma once

#include "core/mixer.hpp"

#include <vector>

namespace fxeng
{
// The box's picture (after the crop) shows on the raster: a part of it inside [0, 1]².
bool boxShows(DmeSettings const& dme);
// The box's picture (after the crop) covers the whole raster, without a soft edge.
bool boxCovers(DmeSettings const& dme);

struct KeyVisibility
{
    // The sources of ME m's visible luma keys and pips (no duplicates, in key order).
    std::vector<Source> keyed;
    // An opaque pip covers the whole raster: the background is not seen.
    bool backgroundCovered = false;
};

// ME `me`'s keys of one grain (`keys` in configuration order: upstream keys bottom to top, then
// the DSKs).
KeyVisibility keyVisibility(int me, std::vector<KeyPlan> const& keys);

// Fills each plan's `keyed` and `backgroundCovered` from the grain's keys.
void markVisibility(FramePlan& plan);
} // namespace fxeng
