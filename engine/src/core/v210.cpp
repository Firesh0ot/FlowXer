#include "core/v210.hpp"

#include <cstring>

namespace fxeng
{
void Planar422::allocate(int w, int h)
{
    width = w;
    height = h;
    y.assign(static_cast<std::size_t>(w) * static_cast<std::size_t>(h), 64);
    cb.assign(static_cast<std::size_t>(w / 2) * static_cast<std::size_t>(h), 512);
    cr.assign(static_cast<std::size_t>(w / 2) * static_cast<std::size_t>(h), 512);
}

void unpackV210(std::uint8_t const* src, int rowBytes, Planar422& dst)
{
    int const cw = dst.width / 2;
    for (int row = 0; row < dst.height; ++row)
    {
        std::uint8_t const* line = src + static_cast<std::size_t>(row) * static_cast<std::size_t>(rowBytes);
        for (int group = 0; group * 6 < dst.width; ++group)
        {
            std::uint32_t w[4];
            std::memcpy(w, line + static_cast<std::size_t>(group) * 16u, sizeof(w));
            std::uint16_t y[6];
            std::uint16_t cb[3];
            std::uint16_t cr[3];
            unpackGroup(w, y, cb, cr);
            for (int i = 0; i < 6 && group * 6 + i < dst.width; ++i)
            {
                dst.y[static_cast<std::size_t>(row) * static_cast<std::size_t>(dst.width) + static_cast<std::size_t>(group * 6 + i)] = y[i];
            }
            for (int i = 0; i < 3 && group * 3 + i < cw; ++i)
            {
                auto const at = static_cast<std::size_t>(row) * static_cast<std::size_t>(cw) + static_cast<std::size_t>(group * 3 + i);
                dst.cb[at] = cb[i];
                dst.cr[at] = cr[i];
            }
        }
    }
}

void packV210(Planar422 const& src, std::uint8_t* dst, int rowBytes)
{
    int const cw = src.width / 2;
    for (int row = 0; row < src.height; ++row)
    {
        std::uint8_t* line = dst + static_cast<std::size_t>(row) * static_cast<std::size_t>(rowBytes);
        std::memset(line, 0, static_cast<std::size_t>(rowBytes));
        for (int group = 0; group * 6 < src.width; ++group)
        {
            std::uint16_t y[6] = {};
            std::uint16_t cb[3] = {};
            std::uint16_t cr[3] = {};
            for (int i = 0; i < 6 && group * 6 + i < src.width; ++i)
            {
                y[i] = src.y[static_cast<std::size_t>(row) * static_cast<std::size_t>(src.width) + static_cast<std::size_t>(group * 6 + i)];
            }
            for (int i = 0; i < 3 && group * 3 + i < cw; ++i)
            {
                auto const at = static_cast<std::size_t>(row) * static_cast<std::size_t>(cw) + static_cast<std::size_t>(group * 3 + i);
                cb[i] = src.cb[at];
                cr[i] = src.cr[at];
            }
            std::uint32_t w[4];
            packGroup(y, cb, cr, w);
            std::memcpy(line + static_cast<std::size_t>(group) * 16u, w, sizeof(w));
        }
    }
}
} // namespace fxeng
