// Raw tally in TSL UMD 5.0, as FlowXer's FLOWXER_TALLY_TSL (FlowXer README "Raw tally
// export", src/flowxer/engine/tsl.py): SCREEN = ME, INDEX = input slot from 0 (1000 + n for
// the re-entered Program of ME n), LH red on Program (both sources while a Mix runs), RH green
// on Preview, text tally off, brightness 3, TEXT = label in UTF-16LE.
#pragma once

#include "core/mixer.hpp"

#include <string>
#include <utility>
#include <vector>

namespace fxeng
{
constexpr int kTslOff = 0;
constexpr int kTslRed = 1;
constexpr int kTslGreen = 2;

struct TslDisplay
{
    int index = 0;
    // UTF-8; sent as UTF-16LE.
    std::string label;
    int lh = kTslOff;
    int rh = kTslOff;
};

// The packets of one SCREEN, each at most 2048 bytes (little-endian, FLAGS bit 0 = UTF-16).
std::vector<std::string> tslPackets(int screen, std::vector<TslDisplay> const& displays);
// TCP framing: DLE/STX, the packet with DLE doubled, DLE/ETX.
std::string tslWrapTcp(std::string const& packet);
// Every ME (SCREEN) with every input and every re-entry source it may take, lit or not.
std::vector<std::pair<int, std::vector<TslDisplay>>> tallyDisplays(Mixer const& mixer, std::vector<std::string> const& inputLabels);
} // namespace fxeng
