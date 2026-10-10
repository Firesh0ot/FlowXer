// Pixel maths shared by the CUDA kernels and the CPU reference (unit tests).
// The v210 group layout follows mxl-multiviewer src/media/v210.cpp (Apache-2.0, same author).
#pragma once

#include <cstdint>

#if defined(__CUDACC__)
#define FXENG_HD __host__ __device__
#else
#define FXENG_HD
#endif

namespace fxeng
{
// Bytes per v210 line: 48 pixels in 128 bytes, lines padded to whole 128-byte blocks.
FXENG_HD constexpr int v210RowBytes(int width)
{
    return ((width + 47) / 48) * 128;
}

FXENG_HD inline std::uint16_t clamp10(float v)
{
    if (v < 0.f)
    {
        v = 0.f;
    }
    if (v > 1023.f)
    {
        v = 1023.f;
    }
    return static_cast<std::uint16_t>(v + 0.5f);
}

// One v210 group: 4 little-endian words hold 6 Y, 3 Cb and 3 Cr samples.
FXENG_HD inline void unpackGroup(std::uint32_t const w[4], std::uint16_t y[6], std::uint16_t cb[3], std::uint16_t cr[3])
{
    cb[0] = static_cast<std::uint16_t>(w[0] & 0x3ffu);
    y[0] = static_cast<std::uint16_t>((w[0] >> 10) & 0x3ffu);
    cr[0] = static_cast<std::uint16_t>((w[0] >> 20) & 0x3ffu);
    y[1] = static_cast<std::uint16_t>(w[1] & 0x3ffu);
    cb[1] = static_cast<std::uint16_t>((w[1] >> 10) & 0x3ffu);
    y[2] = static_cast<std::uint16_t>((w[1] >> 20) & 0x3ffu);
    cr[1] = static_cast<std::uint16_t>(w[2] & 0x3ffu);
    y[3] = static_cast<std::uint16_t>((w[2] >> 10) & 0x3ffu);
    cb[2] = static_cast<std::uint16_t>((w[2] >> 20) & 0x3ffu);
    y[4] = static_cast<std::uint16_t>(w[3] & 0x3ffu);
    cr[2] = static_cast<std::uint16_t>((w[3] >> 10) & 0x3ffu);
    y[5] = static_cast<std::uint16_t>((w[3] >> 20) & 0x3ffu);
}

FXENG_HD inline void packGroup(std::uint16_t const y[6], std::uint16_t const cb[3], std::uint16_t const cr[3], std::uint32_t w[4])
{
    w[0] = (cb[0] & 0x3ffu) | (static_cast<std::uint32_t>(y[0] & 0x3ffu) << 10) | (static_cast<std::uint32_t>(cr[0] & 0x3ffu) << 20);
    w[1] = (y[1] & 0x3ffu) | (static_cast<std::uint32_t>(cb[1] & 0x3ffu) << 10) | (static_cast<std::uint32_t>(y[2] & 0x3ffu) << 20);
    w[2] = (cr[1] & 0x3ffu) | (static_cast<std::uint32_t>(y[3] & 0x3ffu) << 10) | (static_cast<std::uint32_t>(cb[2] & 0x3ffu) << 20);
    w[3] = (y[4] & 0x3ffu) | (static_cast<std::uint32_t>(cr[2] & 0x3ffu) << 10) | (static_cast<std::uint32_t>(y[5] & 0x3ffu) << 20);
}

// Dissolve of one 10-bit sample: t = 0 gives a, t = 1 gives b.
FXENG_HD inline std::uint16_t mix10(std::uint16_t a, std::uint16_t b, float t)
{
    float const fa = static_cast<float>(a);
    return clamp10(fa + (static_cast<float>(b) - fa) * t);
}

// BT.709 limited range, 10-bit Y'CbCr <-> non-linear R'G'B' in 0..1. Values outside
// 0..1 (sub-black, super-white, out-of-gamut test patterns) are kept, not clipped.
struct Rgb
{
    float r;
    float g;
    float b;
};

FXENG_HD inline Rgb ycbcrToRgb(std::uint16_t y, std::uint16_t cb, std::uint16_t cr)
{
    float const yn = (static_cast<float>(y) - 64.f) / 876.f;
    float const pb = (static_cast<float>(cb) - 512.f) / 896.f;
    float const pr = (static_cast<float>(cr) - 512.f) / 896.f;
    return Rgb{yn + 1.5748f * pr, yn - 0.187324f * pb - 0.468124f * pr, yn + 1.8556f * pb};
}

FXENG_HD inline void rgbToYcbcr(Rgb c, std::uint16_t& y, std::uint16_t& cb, std::uint16_t& cr)
{
    float const yn = 0.2126f * c.r + 0.7152f * c.g + 0.0722f * c.b;
    y = clamp10(64.f + 876.f * yn);
    cb = clamp10(512.f + 896.f * (c.b - yn) / 1.8556f);
    cr = clamp10(512.f + 896.f * (c.r - yn) / 1.5748f);
}

// 10-bit video level to 8-bit (NV12 preview).
FXENG_HD inline std::uint8_t to8(std::uint32_t v10)
{
    std::uint32_t const v = (v10 + 2u) >> 2;
    return static_cast<std::uint8_t>(v > 255u ? 255u : v);
}
} // namespace fxeng
