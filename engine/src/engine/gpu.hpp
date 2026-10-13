// CUDA kernels of the engine. All of them are queued on a caller's stream and return at once.
// The v210 sample addressing follows mxl-multiviewer src/media/cuda_compose.cu (Apache-2.0,
// same author).
#pragma once

#include "core/dme.hpp"
#include "core/wipe.hpp"

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

// A 4:4:4 decoder picture (three device planes of `pitch` bytes per line: 8-bit, or with `deep`
// 16-bit samples holding 10 or 12 bits in their high bits) to packed v210; chroma goes to 4:2:2
// as the mean of each pixel pair.
cudaError_t from444(cudaStream_t stream, std::uint8_t const* y, std::uint8_t const* cb, std::uint8_t const* cr, int pitch, bool deep, int width,
    int height, std::uint8_t* v210, int rowBytes);

// A stinger's alpha (the luma plane of a decoder picture, `pitch` bytes per line: 8-bit, or with
// `deep` 16-bit samples holding 10 bits in their high bits) as a v210a key plane, full range
// (8 bits widen to 10); without a plane a full key (1023).
cudaError_t alphaToKey(cudaStream_t stream, std::uint8_t const* luma, int pitch, bool deep, int width, int height, std::uint8_t* key, int keyRowBytes);

// A key layer: a linear key from a v210a fill + key (device; the key plane follows the fill,
// `keyRowBytes` per line), α = key/1023 · gain (straight fill); or, with `luma` set, a luma key of
// a work picture (yuv16 only), α = lumaAlpha(Y, clip, soft, invert, gain), the fill the picture
// itself. out = fill·α + out·(1 − α) per sample, chroma with the pair's mean α. With `dme` set it
// is a box layer instead (yuv16 only; core/dme.hpp dmePair): its box buffer (dmeResample), border
// and shadow.
struct KeyLayer
{
    std::uint8_t const* v210a = nullptr;
    float gain = 0.f;
    bool luma = false;
    WorkBuffer const* source = nullptr;
    float clip = 0.f;
    float soft = 0.f;
    bool invert = false;
    DmeDraw const* dme = nullptr;
};
constexpr int kMaxLayers = 14;
// Box layers per compose: the upstream keyers of an ME.
constexpr int kMaxBoxes = 8;

// Device scratch of the boxes' resample: the weight table. One serves every box, as the boxes
// resample one after another on the render stream.
struct DmeScratch
{
    int* start = nullptr;
    float* weights = nullptr;
    std::size_t bytes = 0;
};
cudaError_t allocDmeScratch(DmeScratch& scratch, int width, int height);
void freeDmeScratch(DmeScratch& scratch);
// Bytes of a box buffer for any box on a W × H raster (the full raster, with a key plane).
std::size_t dmeBufferBytes(int width, int height);
// A box's resample into its box buffer (device, b.bh lines of b.cols int16): the weight table,
// then one kernel that runs the horizontal pass into shared memory and the vertical pass from
// there, per tile of box rows. Nothing for a box that is not on the raster.
cudaError_t dmeResample(cudaStream_t stream, DmeBox const& box, DmeSource const& source, DmeScratch& scratch, std::int16_t* buffer);
// A box's source: a work picture (yuv16), a v210a fill + key, or that fill + key unpacked.
DmeSource dmeSource(WorkBuffer const& work);
DmeSource dmeSource(std::uint8_t const* v210a, int width, int height, int rowBytes, int keyRowBytes);
DmeSource dmeSource(float const* planes, int width, int height);
// A linear box's v210a fill + key unpacked into float planes (core/dme.hpp dmeUnpackPixel), once
// per grain for all the linear boxes on that key flow: the resample then reads it as a pip reads
// its work picture. Bytes of the planes on a W × H raster.
cudaError_t dmeUnpack(cudaStream_t stream, DmeSource const& packed, float* planes);
std::size_t dmePlanesBytes(int width, int height);

// One ME's Program in one pass (docs/engine-p3.md §2): the background (A; the dissolve of A to B
// at t; or the wipe of A to B), then the key layers in order, written once. In yuv16 one fused
// kernel; in rgba16f separate passes.
struct Compose
{
    WorkBuffer const* a = nullptr;
    // A transition to b runs when it is set.
    WorkBuffer const* b = nullptr;
    float t = 0.f;
    bool wiping = false;
    Wipe wipe;
    KeyLayer layers[kMaxLayers];
    int layerCount = 0;
};
cudaError_t compose(cudaStream_t stream, Compose const& job, int rowBytes, int keyRowBytes, WorkBuffer& out);

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
// exact, rgba16f within 2 codes), dissolve (exact), every wipe code (bit-exact), the fused compose
// with key layers (exact), DME boxes (resample and box layers, exact), mosaic. `report` says
// what was checked or what failed.
bool selfTest(std::string& report);
} // namespace fxeng::gpu
