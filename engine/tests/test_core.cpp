#include "test.hpp"

#include "core/audio.hpp"
#include "core/config.hpp"
#include "core/mixer.hpp"
#include "core/mosaic_map.hpp"
#include "core/pixel.hpp"
#include "core/tai.hpp"
#include "core/v210.hpp"
#include "core/wipe.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
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

TEST(key_maths)
{
    // A v210a key line: three 10-bit keys per word.
    std::uint32_t const line[2] = {0u | (512u << 10) | (1023u << 20), 257u};
    auto const* bytes = reinterpret_cast<std::uint8_t const*>(line);
    CHECK_EQ(keyAt(bytes, 0), 0u);
    CHECK_EQ(keyAt(bytes, 1), 512u);
    CHECK_EQ(keyAt(bytes, 2), 1023u);
    CHECK_EQ(keyAt(bytes, 3), 257u);
    CHECK_EQ(keyAlpha(0, 1.f), 0.f);
    CHECK(std::abs(keyAlpha(1023, 1.f) - 1.f) < 1e-6f);
    CHECK(std::abs(keyAlpha(1023, 0.5f) - 0.5f) < 1e-6f);
    // Straight white fill (940) over black (64): key 0 is the background, 1023 the fill, and
    // partial keys in between (the browser source's 25/50/75 % alpha).
    CHECK_EQ(mix10(64, 940, keyAlpha(0, 1.f)), 64);
    CHECK_EQ(mix10(64, 940, keyAlpha(1023, 1.f)), 940);
    CHECK_EQ(mix10(64, 940, keyAlpha(257, 1.f)), 284);
    CHECK_EQ(mix10(64, 940, keyAlpha(514, 1.f)), 504);
    CHECK_EQ(mix10(64, 940, keyAlpha(766, 1.f)), 720);
    // The keyer's gain (a fade) scales the key: a full key at half gain is the half mix.
    CHECK_EQ(mix10(64, 940, keyAlpha(1023, 0.5f)), 502);
    CHECK_EQ(mix10(300, 700, keyAlpha(1023, 0.f)), 300);
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


// ---- wipes ------------------------------------------------------------------------------

namespace
{
float patternAt(int code, int x, int y, bool reverse = false, int w = 1920, int h = 1080)
{
    WipeSettings set;
    set.pattern = code;
    set.reverse = reverse;
    return wipePattern(makeWipe(set, 0.5f, w, h), x, y);
}
} // namespace

TEST(wipe_codes_at_known_points)
{
    int const w = 1920;
    int const h = 1080;
    // Bars: left edge first, right edge last; top first, bottom last.
    CHECK(patternAt(1, 0, 500) < 0.001f && patternAt(1, w - 1, 500) > 0.999f);
    CHECK(patternAt(2, 900, 0) < 0.001f && patternAt(2, 900, h - 1) > 0.999f);
    // Corner boxes start in their corner, centre boxes and irises in the centre.
    CHECK(patternAt(3, 0, 0) < 0.001f && patternAt(5, w - 1, h - 1) < 0.001f && patternAt(4, w - 1, 0) < 0.001f && patternAt(6, 0, h - 1) < 0.001f);
    CHECK(patternAt(3, w - 1, h - 1) > 0.999f);
    for (int code : {101, 102, 119})
    {
        CHECK(patternAt(code, w / 2, h / 2) < 0.002f);
        CHECK(patternAt(code, 0, 0) > 0.99f);
    }
    // Edge boxes start at the middle of their edge.
    CHECK(patternAt(23, w / 2, 0) < 0.002f && patternAt(24, w - 1, h / 2) < 0.002f && patternAt(25, w / 2, h - 1) < 0.002f &&
          patternAt(26, 0, h / 2) < 0.002f);
    // Barn doors open from the centre line; diagonals from their corner.
    CHECK(patternAt(21, w / 2, 0) < 0.002f && patternAt(21, 0, 0) > 0.99f);
    CHECK(patternAt(22, 0, h / 2) < 0.002f && patternAt(22, 0, 0) > 0.99f);
    CHECK(patternAt(41, 0, 0) < 0.001f && patternAt(41, w - 1, h - 1) > 0.999f);
    CHECK(patternAt(42, w - 1, 0) < 0.001f && patternAt(42, 0, h - 1) > 0.999f);
    CHECK(patternAt(45, 0, h - 1) < 0.002f && patternAt(46, 0, 0) < 0.002f && patternAt(46, w - 1, 0) > 0.99f);
    // The circle is round in pixels: the same s 400 pixels right of and below the centre.
    CHECK(std::abs(patternAt(119, w / 2 + 400, h / 2) - patternAt(119, w / 2, h / 2 + 400)) < 0.001f);
    // Clock: clockwise from 12, 3, 6, 9 o'clock.
    CHECK(patternAt(201, w / 2 + 2, 0) < 0.01f);
    CHECK(std::abs(patternAt(201, w - 1, h / 2) - 0.25f) < 0.001f);
    CHECK(std::abs(patternAt(201, w / 2, h - 1) - 0.5f) < 0.001f);
    CHECK(std::abs(patternAt(201, 0, h / 2) - 0.75f) < 0.001f);
    CHECK(patternAt(202, w - 1, h / 2 + 1) < 0.01f && patternAt(203, w / 2 - 1, h - 1) < 0.01f && patternAt(204, 0, h / 2 - 1) < 0.01f);
    // Reverse runs every pattern backwards.
    for (int code : kWipeCodes)
    {
        for (auto [x, y] : {std::pair{17, 33}, std::pair{960, 540}, std::pair{1800, 1000}})
        {
            CHECK(patternAt(code, x, y, true) == 1.f - patternAt(code, x, y));
        }
    }
    CHECK(wipeCodeValid(119) && !wipeCodeValid(7) && !wipeCodeValid(0));
    CHECK_EQ(sizeof(kWipeCodes) / sizeof(kWipeCodes[0]), 23u);
}

TEST(wipe_end_points_are_exact)
{
    // t = 0 is A and t = 1 is B everywhere, for every code, softness, border and direction.
    int const w = 96;
    int const h = 54;
    Planar422 a;
    Planar422 b;
    a.allocate(w, h);
    b.allocate(w, h);
    fillRandom(a, 3);
    fillRandom(b, 4);
    Planar422 out;
    int errors = 0;
    for (int code : kWipeCodes)
    {
        for (float soft : {0.f, 0.08f, 1.f})
        {
            for (float border : {0.f, 0.03f, 1.f})
            {
                for (bool reverse : {false, true})
                {
                    WipeSettings set;
                    set.pattern = code;
                    set.reverse = reverse;
                    set.soft = soft;
                    set.border = border;
                    wipeFrame(a, b, makeWipe(set, 0.f, w, h), out);
                    errors += !(out.y == a.y && out.cb == a.cb && out.cr == a.cr);
                    wipeFrame(a, b, makeWipe(set, 1.f, w, h), out);
                    errors += !(out.y == b.y && out.cb == b.cb && out.cr == b.cr);
                }
            }
        }
    }
    CHECK_EQ(errors, 0);
}

TEST(wipe_soft_edge_and_border)
{
    // A horizontal bar on a 1000-pixel line at t = 0.5: A = 100, B = 900.
    int const w = 1000;
    int const h = 2;
    Planar422 a;
    Planar422 b;
    a.allocate(w, h);
    b.allocate(w, h);
    std::fill(a.y.begin(), a.y.end(), static_cast<std::uint16_t>(100));
    std::fill(b.y.begin(), b.y.end(), static_cast<std::uint16_t>(900));
    std::fill(a.cb.begin(), a.cb.end(), static_cast<std::uint16_t>(512));
    std::fill(b.cb.begin(), b.cb.end(), static_cast<std::uint16_t>(512));
    a.cr = a.cb;
    b.cr = b.cb;
    WipeSettings set;
    Planar422 out;
    // Hard: B left of the front (F = 0.5), A right of it.
    wipeFrame(a, b, makeWipe(set, 0.5f, w, h), out);
    CHECK(out.y[0] == 900 && out.y[499] == 900 && out.y[500] == 100 && out.y[999] == 100);
    // Soft 0.1: F = 0.55, a linear ramp from B to A over 100 pixels (s 0.45 .. 0.55).
    set.soft = 0.1f;
    wipeFrame(a, b, makeWipe(set, 0.5f, w, h), out);
    CHECK(out.y[440] == 900 && out.y[560] == 100);
    CHECK(std::abs(static_cast<int>(out.y[500]) - 500) <= 5);
    for (int x = 441; x < 560; ++x)
    {
        CHECK(out.y[static_cast<std::size_t>(x)] <= out.y[static_cast<std::size_t>(x - 1)]);
    }
    // Hard with a border of 0.1 in grey 600: F = 0.55; B up to s = 0.45, the border to 0.55.
    set.soft = 0.f;
    set.border = 0.1f;
    set.borderY = 600;
    wipeFrame(a, b, makeWipe(set, 0.5f, w, h), out);
    CHECK(out.y[440] == 900 && out.y[460] == 600 && out.y[540] == 600 && out.y[560] == 100);
    // Chroma takes the mean of its pair's masks: the pair across the hard edge at x = 499/500.
    set.border = 0.f;
    std::fill(b.cb.begin(), b.cb.end(), static_cast<std::uint16_t>(712));
    WipeSettings odd;
    wipeFrame(a, b, makeWipe(odd, 0.501f, w, h), out);
    CHECK_EQ(out.y[500], 900);
    CHECK_EQ(out.y[501], 100);
    CHECK_EQ(out.cb[250], 612);
}

TEST(wipe_vectors_for_the_gui)
{
    // The GUI's icons use a port of s(u, v) that is checked against these values.
    std::ifstream file(FXENG_WIPE_VECTORS);
    auto const doc = nlohmann::json::parse(file, nullptr, false);
    CHECK(!doc.is_discarded() && doc["vectors"].size() == 23u * 11u);
    if (doc.is_discarded())
    {
        return;
    }
    int const w = doc["width"].get<int>();
    int const h = doc["height"].get<int>();
    float worst = 0.f;
    for (auto const& v : doc["vectors"])
    {
        float const s = patternAt(v[0].get<int>(), v[1].get<int>(), v[2].get<int>(), false, w, h);
        worst = std::max(worst, std::abs(s - v[3].get<float>()));
    }
    CHECK(worst < 1e-6f);
}
