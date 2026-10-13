#include "core/mosaic_map.hpp"

#include <algorithm>

namespace fxeng
{
int mosaicGrid(int tiles)
{
    int n = 2;
    while (n * n < tiles)
    {
        ++n;
    }
    return n;
}

std::vector<MosaicTile> mosaicTiles(int width, int height, int inputs, int players, int mes)
{
    int const count = std::max(0, inputs) + std::max(0, players) + 2 * std::max(0, mes);
    int const n = mosaicGrid(count);
    int const tileW = (width / n) & ~1;
    int const tileH = (height / n) & ~1;
    std::vector<MosaicTile> tiles;
    auto const add = [&](MosaicTile::Kind kind, int index, std::string id) {
        int const slot = static_cast<int>(tiles.size());
        MosaicTile tile;
        tile.kind = kind;
        tile.index = index;
        tile.id = std::move(id);
        tile.x = (slot % n) * tileW;
        tile.y = (slot / n) * tileH;
        tile.w = tileW;
        tile.h = tileH;
        tiles.push_back(std::move(tile));
    };
    for (int i = 1; i <= inputs; ++i)
    {
        add(MosaicTile::Kind::Input, i, "in" + std::to_string(i));
    }
    for (int p = 1; p <= players; ++p)
    {
        add(MosaicTile::Kind::Player, p, "cp" + std::to_string(p));
    }
    for (int m = 1; m <= mes; ++m)
    {
        add(MosaicTile::Kind::Program, m, "me" + std::to_string(m) + "-pgm");
        add(MosaicTile::Kind::Preview, m, "me" + std::to_string(m) + "-pvw");
    }
    return tiles;
}
} // namespace fxeng
