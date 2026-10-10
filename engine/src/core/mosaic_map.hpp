// The preview mosaic: one canvas, a 4×4 grid. Rows 1-2 are the inputs, row 3 the ME
// Programs, row 4 the ME Previews. A GUI crops single tiles out of the one stream.
#pragma once

#include <string>
#include <vector>

namespace fxeng
{
struct MosaicTile
{
    enum class Kind
    {
        Input,
        Program,
        Preview
    };
    Kind kind = Kind::Input;
    // Input 1..8 or ME 1..4.
    int index = 1;
    // "in1", "me1-pgm", "me1-pvw".
    std::string id;
    int x = 0;
    int y = 0;
    int w = 0;
    int h = 0;
};

constexpr int kMosaicColumns = 4;
constexpr int kMosaicRows = 4;
constexpr int kMaxInputs = 8;
constexpr int kMaxMes = 4;

// Tiles for `inputs` (<= 8) and `mes` (<= 4) on a width × height canvas. Tile edges are even
// (NV12 chroma is 2×2).
std::vector<MosaicTile> mosaicTiles(int width, int height, int inputs, int mes);
} // namespace fxeng
