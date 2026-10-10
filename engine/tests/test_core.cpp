#include "test.hpp"

#include "core/audio.hpp"
#include "core/config.hpp"
#include "core/mixer.hpp"
#include "core/mosaic_map.hpp"
#include "core/pixel.hpp"
#include "core/tai.hpp"
#include "core/v210.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <set>

using namespace fxeng;

namespace
{
void fillRandom(Planar422& p, unsigned seed)
{
    unsigned state = seed;
    auto const next = [&state] {
        state = state * 1664525u + 1013904223u;
        return static_cast<std::uint16_t>((state >> 8) % 1024);
    };
    for (auto& v : p.y)
    {
        v = next();
    }
    for (auto& v : p.cb)
    {
        v = next();
    }
    for (auto& v : p.cr)
    {
        v = next();
    }
}
} // namespace

// ---- v210 -------------------------------------------------------------------------------

TEST(v210_group_layout)
{
    std::uint16_t const y[6] = {1, 2, 3, 4, 5, 6};
    std::uint16_t const cb[3] = {7, 8, 9};
    std::uint16_t const cr[3] = {10, 11, 12};
    std::uint32_t w[4];
    packGroup(y, cb, cr, w);
    // SMPTE v210: Cb0 Y0 Cr0 | Y1 Cb1 Y2 | Cr1 Y3 Cb2 | Y4 Cr2 Y5, 10 bits each from bit 0.
    CHECK_EQ(w[0], 7u | (1u << 10) | (10u << 20));
    CHECK_EQ(w[1], 2u | (8u << 10) | (3u << 20));
    CHECK_EQ(w[2], 11u | (4u << 10) | (9u << 20));
    CHECK_EQ(w[3], 5u | (12u << 10) | (6u << 20));
    std::uint16_t y2[6];
    std::uint16_t cb2[3];
    std::uint16_t cr2[3];
    unpackGroup(w, y2, cb2, cr2);
    CHECK(std::memcmp(y, y2, sizeof(y)) == 0);
    CHECK(std::memcmp(cb, cb2, sizeof(cb)) == 0);
    CHECK(std::memcmp(cr, cr2, sizeof(cr)) == 0);
}

TEST(v210_round_trip)
{
    // 1920 is whole groups and blocks; 1280 ends in a partial group and a padded block.
    for (int width : {1920, 1280})
    {
        int const height = 4;
        Planar422 a;
        a.allocate(width, height);
        fillRandom(a, static_cast<unsigned>(width));
        int const rowBytes = v210RowBytes(width);
        std::vector<std::uint8_t> packed(static_cast<std::size_t>(rowBytes) * height, 0xff);
        packV210(a, packed.data(), rowBytes);
        Planar422 b;
        b.allocate(width, height);
        unpackV210(packed.data(), rowBytes, b);
        CHECK(a.y == b.y);
        CHECK(a.cb == b.cb);
        CHECK(a.cr == b.cr);
        // Line padding after the last group is zero.
        int const used = ((width + 5) / 6) * 16;
        for (int x = used; x < rowBytes; ++x)
        {
            CHECK_EQ(static_cast<int>(packed[static_cast<std::size_t>(x)]), 0);
        }
    }
    CHECK_EQ(v210RowBytes(1920), 5120);
    CHECK_EQ(v210RowBytes(1280), 3456);
}

// ---- pixel maths ------------------------------------------------------------------------

TEST(dissolve_maths)
{
    CHECK_EQ(mix10(100, 900, 0.f), 100);
    CHECK_EQ(mix10(100, 900, 1.f), 900);
    CHECK_EQ(mix10(100, 200, 0.5f), 150);
    CHECK_EQ(mix10(64, 940, 0.25f), 283);
    CHECK_EQ(mix10(940, 64, 0.25f), 721);
    // Halfway rounds up; the result stays within 10 bits.
    CHECK_EQ(mix10(0, 1, 0.5f), 1);
    CHECK_EQ(mix10(1023, 1023, 0.7f), 1023);
    // Monotonic over a whole transition.
    int previous = -1;
    for (int k = 0; k <= 25; ++k)
    {
        int const v = mix10(64, 940, static_cast<float>(k) / 25.f);
        CHECK(v >= previous);
        previous = v;
    }
    CHECK_EQ(previous, 940);
}

TEST(ycbcr_rgb_round_trip)
{
    int worst = 0;
    for (int y = 64; y <= 940; y += 17)
    {
        for (int c = 64; c <= 960; c += 32)
        {
            std::uint16_t yy = 0;
            std::uint16_t cb = 0;
            std::uint16_t cr = 0;
            rgbToYcbcr(ycbcrToRgb(static_cast<std::uint16_t>(y), static_cast<std::uint16_t>(c), static_cast<std::uint16_t>(1024 - c)), yy, cb, cr);
            worst = std::max({worst, std::abs(yy - y), std::abs(cb - c), std::abs(cr - (1024 - c))});
        }
    }
    CHECK(worst <= 1);
    Rgb const white = ycbcrToRgb(940, 512, 512);
    CHECK(std::abs(white.r - 1.f) < 1e-4f && std::abs(white.g - 1.f) < 1e-4f && std::abs(white.b - 1.f) < 1e-4f);
    CHECK_EQ(static_cast<int>(to8(940)), 235);
    CHECK_EQ(static_cast<int>(to8(64)), 16);
    CHECK_EQ(static_cast<int>(to8(1023)), 255);
}

