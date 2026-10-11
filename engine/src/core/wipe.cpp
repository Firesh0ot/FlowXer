#include "core/wipe.hpp"

#include "core/v210.hpp"

namespace fxeng
{
void wipeFrame(Planar422 const& a, Planar422 const& b, Wipe const& w, Planar422& out)
{
    int const width = a.width;
    int const cw = width / 2;
    out.allocate(width, a.height);
    for (int y = 0; y < a.height; ++y)
    {
        for (int p = 0; p < cw; ++p)
        {
            std::size_t const yi = static_cast<std::size_t>(y) * static_cast<std::size_t>(width) + 2u * static_cast<std::size_t>(p);
            std::size_t const ci = static_cast<std::size_t>(y) * static_cast<std::size_t>(cw) + static_cast<std::size_t>(p);
            wipePair(w, 2 * p, y, &a.y[yi], a.cb[ci], a.cr[ci], &b.y[yi], b.cb[ci], b.cr[ci], &out.y[yi], out.cb[ci], out.cr[ci]);
        }
    }
}
} // namespace fxeng
