// Wipes (docs/engine-p3.md §4): every SMPTE ST 258 wipe code maps a pixel to s in [0, 1], the
// moment the edge reaches it; the mask follows from the front F = t·(1 + soft + border).
// One code path for the CUDA kernel and the CPU reference: only + − × ÷ √ and a polynomial
// arctangent, compiled without FMA contraction (-fmad=false, -ffp-contract=off), so the GPU and
// the CPU agree bit for bit.
#pragma once

#include "core/pixel.hpp"

#include <math.h>

#include <cstdint>

namespace fxeng
{
struct Planar422;

// The wipe codes of P3: bars, corner and edge boxes, barn doors, diagonals, box, diamond, circle,
// clock.
constexpr int kWipeCodes[] = {1, 2, 3, 4, 5, 6, 21, 22, 23, 24, 25, 26, 41, 42, 45, 46, 101, 102, 119, 201, 202, 203, 204};

inline bool wipeCodeValid(int code)
{
    for (int c : kWipeCodes)
    {
        if (c == code)
        {
            return true;
        }
    }
    return false;
}

// The ME's wipe settings. soft and border are fractions of the pattern's travel (0..1); the
// border colour is 10-bit Y'CbCr.
struct WipeSettings
{
    int pattern = 1;
    bool reverse = false;
    float soft = 0.f;
    float border = 0.f;
    std::uint16_t borderY = 940;
    std::uint16_t borderCb = 512;
    std::uint16_t borderCr = 512;
};

// One grain's wipe on a raster.
struct Wipe
{
    WipeSettings set;
    float front = 0.f;
    float invW = 0.f;
    float invH = 0.f;
    float aspect = 1.f;
    float invRmax = 1.f;
};

// t = 0 is A everywhere and t = 1 is B everywhere, for every pattern, softness and border: the
// front is moved well past the pattern there, so rounding cannot leave a sample behind.
inline Wipe makeWipe(WipeSettings const& set, float t, int width, int height)
{
    Wipe w;
    w.set = set;
    float const travel = 1.f + set.soft + set.border;
    w.front = t <= 0.f ? -1.f : t >= 1.f ? travel + 1.f : t * travel;
    w.invW = 1.f / static_cast<float>(width);
    w.invH = 1.f / static_cast<float>(height);
    w.aspect = static_cast<float>(width) / static_cast<float>(height);
    w.invRmax = 1.f / (0.5f * sqrtf(w.aspect * w.aspect + 1.f));
    return w;
}

FXENG_HD inline float wipeMax(float a, float b)
{
    return a > b ? a : b;
}

FXENG_HD inline float wipeAbs(float v)
{
    return v < 0.f ? -v : v;
}

// atan(z) for z in [0, 1]: minimax polynomial, |error| < 2e-5 rad.
FXENG_HD inline float atan01(float z)
{
    float const z2 = z * z;
    float p = -0.01172120f;
    p = p * z2 + 0.05265332f;
    p = p * z2 - 0.11643287f;
    p = p * z2 + 0.19354346f;
    p = p * z2 - 0.33262347f;
    p = p * z2 + 0.99997726f;
    return p * z;
}

// Clockwise angle from 12 o'clock in turns [0, 1); dx to the right, dy down.
FXENG_HD inline float clockTurns(float dx, float dy)
{
    float const x = -dy;
    float const y = dx;
    float const ax = wipeAbs(x);
    float const ay = wipeAbs(y);
    if (ax == 0.f && ay == 0.f)
    {
        return 0.f;
    }
    float const hi = wipeMax(ax, ay);
    float const lo = ax > ay ? ay : ax;
    float r = atan01(lo / hi);
    if (ay > ax)
    {
        r = 1.57079633f - r;
    }
    if (x < 0.f)
    {
        r = 3.14159265f - r;
    }
    if (y < 0.f)
    {
        r = -r;
    }
    if (r < 0.f)
    {
        r = r + 6.28318531f;
    }
    float const turns = r * 0.159154943f;
    return turns >= 1.f ? 0.f : turns;
}

// s(u, v) of pixel (x, y), u = (x + ½)/W, v = (y + ½)/H; reverse runs it backwards (1 − s).
FXENG_HD inline float wipePattern(Wipe const& w, int x, int y)
{
    float const u = (static_cast<float>(x) + 0.5f) * w.invW;
    float const v = (static_cast<float>(y) + 0.5f) * w.invH;
    float s = u;
    switch (w.set.pattern)
    {
    case 2:
        s = v;
        break;
    case 3:
        s = wipeMax(u, v);
        break;
    case 4:
        s = wipeMax(1.f - u, v);
        break;
    case 5:
        s = wipeMax(1.f - u, 1.f - v);
        break;
    case 6:
        s = wipeMax(u, 1.f - v);
        break;
    case 21:
        s = wipeAbs(2.f * u - 1.f);
        break;
    case 22:
        s = wipeAbs(2.f * v - 1.f);
        break;
    case 23:
        s = wipeMax(wipeAbs(2.f * u - 1.f), v);
        break;
    case 24:
        s = wipeMax(1.f - u, wipeAbs(2.f * v - 1.f));
        break;
    case 25:
        s = wipeMax(wipeAbs(2.f * u - 1.f), 1.f - v);
        break;
    case 26:
        s = wipeMax(u, wipeAbs(2.f * v - 1.f));
        break;
    case 41:
        s = 0.5f * (u + v);
        break;
    case 42:
        s = 0.5f * ((1.f - u) + v);
        break;
    case 45:
        s = wipeAbs(u + v - 1.f);
        break;
    case 46:
        s = wipeAbs(u - v);
        break;
    case 101:
        s = wipeMax(wipeAbs(2.f * u - 1.f), wipeAbs(2.f * v - 1.f));
        break;
    case 102:
        s = 0.5f * (wipeAbs(2.f * u - 1.f) + wipeAbs(2.f * v - 1.f));
        break;
    case 119:
    {
        // Round in pixels: x scaled by the aspect ratio, 1 at the corners.
        float const dx = (u - 0.5f) * w.aspect;
        float const dy = v - 0.5f;
        s = sqrtf(dx * dx + dy * dy) * w.invRmax;
        break;
    }
    case 201:
    case 202:
    case 203:
    case 204:
    {
        // Clockwise from 12, 3, 6 or 9 o'clock.
        float const f = clockTurns((u - 0.5f) * w.aspect, v - 0.5f) - 0.25f * static_cast<float>(w.set.pattern - 201);
        s = f < 0.f ? f + 1.f : f;
        break;
    }
    default:
        break;
    }
    return w.set.reverse ? 1.f - s : s;
}

// The masks of one pixel. Without a border mA == mB is B's share; with a border mA is the share of
// "border or B" and mB the share of B within it (the soft edge is split around the border).
FXENG_HD inline void wipeMasks(Wipe const& w, int x, int y, float& mA, float& mB)
{
    float const d = w.front - wipePattern(w, x, y);
    float const soft = w.set.soft;
    if (w.set.border <= 0.f)
    {
        mA = soft > 0.f ? clamp01(d / soft) : (d > 0.f ? 1.f : 0.f);
        mB = mA;
        return;
    }
    float const h = 0.5f * soft;
    if (h > 0.f)
    {
        mA = clamp01(d / h);
        mB = clamp01((d - h - w.set.border) / h);
    }
    else
    {
        mA = d > 0.f ? 1.f : 0.f;
        mB = (d - w.set.border) > 0.f ? 1.f : 0.f;
    }
}

// One sample: lerp(a, b, mA), or lerp(a, lerp(border, b, mB), mA) with a border.
FXENG_HD inline std::uint16_t wipeSample(std::uint16_t a, std::uint16_t b, std::uint16_t border, float mA, float mB, bool hasBorder)
{
    float const fa = static_cast<float>(a);
    if (!hasBorder)
    {
        return clamp10(fa + (static_cast<float>(b) - fa) * mA);
    }
    float const fc = static_cast<float>(border);
    float const inner = fc + (static_cast<float>(b) - fc) * mB;
    return clamp10(fa + (inner - fa) * mA);
}

// A luma pair (x even) and its chroma sample: the chroma masks are the means of the pair's.
FXENG_HD inline void wipePair(Wipe const& w, int x, int y, std::uint16_t const ay[2], std::uint16_t acb, std::uint16_t acr, std::uint16_t const by[2],
    std::uint16_t bcb, std::uint16_t bcr, std::uint16_t oy[2], std::uint16_t& ocb, std::uint16_t& ocr)
{
    float mA0 = 0.f;
    float mB0 = 0.f;
    float mA1 = 0.f;
    float mB1 = 0.f;
    wipeMasks(w, x, y, mA0, mB0);
    wipeMasks(w, x + 1, y, mA1, mB1);
    bool const hb = w.set.border > 0.f;
    oy[0] = wipeSample(ay[0], by[0], w.set.borderY, mA0, mB0, hb);
    oy[1] = wipeSample(ay[1], by[1], w.set.borderY, mA1, mB1, hb);
    float const mAc = 0.5f * (mA0 + mA1);
    float const mBc = 0.5f * (mB0 + mB1);
    ocb = wipeSample(acb, bcb, w.set.borderCb, mAc, mBc, hb);
    ocr = wipeSample(acr, bcr, w.set.borderCr, mAc, mBc, hb);
}

// The CPU reference of a whole picture (the unit tests, the GPU self-test, the lab check).
void wipeFrame(Planar422 const& a, Planar422 const& b, Wipe const& w, Planar422& out);
} // namespace fxeng
