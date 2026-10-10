// The preview mosaic: one canvas, a square grid of 16:9 tiles. The live inputs and the clip
// players first (slot order), then per rendering ME its Program and Preview. A GUI crops single
// tiles out of the one stream (GET /mosaic/map).
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
        Player,
        Program,
        Preview
    };
    Kind kind = Kind::Input;
    // Input 1..N, player 1..C or ME 1..4.
    int index = 1;
    // "in1", "cp1", "me1-pgm", "me1-pvw".
    std::string id;
    int x = 0;
    int y = 0;
    int w = 0;
    int h = 0;
};

// Tiles for `inputs` live inputs, `players` clip players and `mes` MEs on a width × height
// canvas: the smallest n × n grid (n >= 2) that holds them all. Tile edges are even (NV12 chroma
// is 2×2).
std::vector<MosaicTile> mosaicTiles(int width, int height, int inputs, int players, int mes);
// Columns (and rows) of that grid.
int mosaicGrid(int tiles);
} // namespace fxeng
