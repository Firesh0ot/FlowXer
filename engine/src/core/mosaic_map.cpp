#include "core/mosaic_map.hpp"

#include <algorithm>

namespace fxeng
{
std::vector<MosaicTile> mosaicTiles(int width, int height, int inputs, int mes)
{
    int const tileW = (width / kMosaicColumns) & ~1;
    int const tileH = (height / kMosaicRows) & ~1;
    std::vector<MosaicTile> tiles;
    auto const add = [&](MosaicTile::Kind kind, int index, std::string id, int slot) {
        MosaicTile tile;
        tile.kind = kind;
        tile.index = index;
        tile.id = std::move(id);
        tile.x = (slot % kMosaicColumns) * tileW;
        tile.y = (slot / kMosaicColumns) * tileH;
        tile.w = tileW;
        tile.h = tileH;
        tiles.push_back(std::move(tile));
    };
    for (int i = 1; i <= std::min(inputs, kMaxInputs); ++i)
    {
        add(MosaicTile::Kind::Input, i, "in" + std::to_string(i), i - 1);
    }
    for (int m = 1; m <= std::min(mes, kMaxMes); ++m)
    {
        add(MosaicTile::Kind::Program, m, "me" + std::to_string(m) + "-pgm", 2 * kMosaicColumns + m - 1);
        add(MosaicTile::Kind::Preview, m, "me" + std::to_string(m) + "-pvw", 3 * kMosaicColumns + m - 1);
    }
    return tiles;
}
} // namespace fxeng
