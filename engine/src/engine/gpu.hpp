// CUDA kernels of the engine. All of them are queued on a caller's stream and return at once.
// The v210 sample addressing follows mxl-multiviewer src/media/cuda_compose.cu (Apache-2.0,
// same author).
#pragma once

#include <cuda_runtime_api.h>

#include <cstddef>
#include <cstdint>
#include <string>

namespace fxeng::gpu
{
enum class WorkFormat
{
    // 10-bit 4:2:2 in 16-bit planes: Y (w×h), then Cb and Cr (w/2×h each).
    Yuv16,
    // Non-linear R'G'B'A in half floats, w×h pixels of 8 bytes.
    Rgba16f
};

// One picture in the working format on the device (one allocation).
struct WorkBuffer
{
    WorkFormat format = WorkFormat::Yuv16;
    int width = 0;
    int height = 0;
    void* data = nullptr;
    std::size_t bytes = 0;
};

std::size_t workBytes(WorkFormat format, int width, int height);
cudaError_t allocWork(WorkBuffer& buffer, WorkFormat format, int width, int height);
void freeWork(WorkBuffer& buffer);

// Packed v210 (device) to the working format.
cudaError_t unpack(cudaStream_t stream, std::uint8_t const* v210, int rowBytes, WorkBuffer& out);
// out = a when b is null or t <= 0 (a copy), else the dissolve from a to b at t (0..1).
cudaError_t mix(cudaStream_t stream, WorkBuffer const& a, WorkBuffer const* b, float t, WorkBuffer& out);
// Working format to packed v210 (device); line padding is zeroed.
cudaError_t pack(cudaStream_t stream, WorkBuffer const& in, std::uint8_t* v210, int rowBytes);
// A packed v210 picture of limited-range black.
cudaError_t fillBlack(cudaStream_t stream, std::uint8_t* v210, int rowBytes, int width, int height);

struct MosaicSource
{
    // Packed v210 on the device; null draws a black tile.
    std::uint8_t const* v210 = nullptr;
    int rowBytes = 0;
    int width = 0;
    int height = 0;
    // Destination tile (even edges).
    int x = 0;
    int y = 0;
    int w = 0;
    int h = 0;
};
constexpr int kMaxMosaicTiles = 16;

// Area-average downscale of each source into its tile of an 8-bit NV12 canvas.
cudaError_t mosaic(cudaStream_t stream, MosaicSource const* tiles, int count, std::uint8_t* luma, int lumaPitch, std::uint8_t* chroma, int chromaPitch);

// Kernel checks against the CPU reference on the current device: v210 round trip (yuv16
// exact, rgba16f within 2 codes), dissolve (exact), mosaic. `report` says what was checked
// or what failed.
bool selfTest(std::string& report);
} // namespace fxeng::gpu
