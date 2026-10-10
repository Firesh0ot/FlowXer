// CPU v210 reference: the unit tests and the GPU self-test compare against it.
// Adapted from mxl-multiviewer src/media/v210.cpp (Apache-2.0, same author).
#pragma once

#include "core/pixel.hpp"

#include <cstdint>
#include <vector>

namespace fxeng
{
// 10-bit 4:2:2 planar picture: Y is width × height, Cb and Cr are width/2 × height.
struct Planar422
{
    int width = 0;
    int height = 0;
    std::vector<std::uint16_t> y;
    std::vector<std::uint16_t> cb;
    std::vector<std::uint16_t> cr;

    void allocate(int w, int h);
};

void unpackV210(std::uint8_t const* src, int rowBytes, Planar422& dst);
// Writes whole lines of `rowBytes`; the padding after the last group is zero.
void packV210(Planar422 const& src, std::uint8_t* dst, int rowBytes);
} // namespace fxeng
