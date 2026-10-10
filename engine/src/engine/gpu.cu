#include "engine/gpu.hpp"

#include "core/pixel.hpp"
#include "core/v210.hpp"

#include <cuda_fp16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace fxeng::gpu
{
namespace
{
struct __align__(8) Half4
{
    __half2 rg;
    __half2 ba;
};

__device__ std::uint32_t const* lineWords(std::uint8_t const* v210, int rowBytes, int row)
{
    return reinterpret_cast<std::uint32_t const*>(v210 + static_cast<std::size_t>(row) * static_cast<std::size_t>(rowBytes));
}

// One sample read in place from packed v210 (x in pixels for luma, in chroma pairs for Cb/Cr).
__device__ unsigned lumaAt(std::uint32_t const* words, int x)
{
    int const i = x % 6;
    int const word = (x / 6) * 4 + (i == 0 ? 0 : i <= 2 ? 1 : i == 3 ? 2 : 3);
    int const shift = (i == 1 || i == 4) ? 0 : (i == 0 || i == 3) ? 10 : 20;
    return (words[word] >> shift) & 0x3ffu;
}

__device__ unsigned cbAt(std::uint32_t const* words, int cx)
{
    int const j = cx % 3;
    return (words[(cx / 3) * 4 + j] >> (j * 10)) & 0x3ffu;
}

__device__ unsigned crAt(std::uint32_t const* words, int cx)
{
    int const j = cx % 3;
    int const word = (cx / 3) * 4 + (j == 0 ? 0 : j + 1);
    int const shift = j == 0 ? 20 : j == 1 ? 0 : 10;
    return (words[word] >> shift) & 0x3ffu;
}

__device__ void readGroup(std::uint8_t const* v210, int rowBytes, int row, int group, std::uint16_t y[6], std::uint16_t cb[3], std::uint16_t cr[3])
{
    uint4 const v = *reinterpret_cast<uint4 const*>(v210 + static_cast<std::size_t>(row) * static_cast<std::size_t>(rowBytes) + static_cast<std::size_t>(group) * 16u);
    std::uint32_t const w[4] = {v.x, v.y, v.z, v.w};
    unpackGroup(w, y, cb, cr);
}

__device__ void writeGroup(std::uint8_t* v210, int rowBytes, int row, int group, std::uint32_t const w[4])
{
    *reinterpret_cast<uint4*>(v210 + static_cast<std::size_t>(row) * static_cast<std::size_t>(rowBytes) + static_cast<std::size_t>(group) * 16u) =
        make_uint4(w[0], w[1], w[2], w[3]);
}

__global__ void unpackYuvKernel(std::uint8_t const* v210, int rowBytes, int width, int height, std::uint16_t* y, std::uint16_t* cb, std::uint16_t* cr)
{
    int const group = blockIdx.x * blockDim.x + threadIdx.x;
    int const row = blockIdx.y;
    if (row >= height || group * 6 >= width)
    {
        return;
    }
    std::uint16_t ys[6];
    std::uint16_t cbs[3];
    std::uint16_t crs[3];
    readGroup(v210, rowBytes, row, group, ys, cbs, crs);
    int const cw = width / 2;
    std::size_t const yRow = static_cast<std::size_t>(row) * static_cast<std::size_t>(width);
    std::size_t const cRow = static_cast<std::size_t>(row) * static_cast<std::size_t>(cw);
    for (int i = 0; i < 6; ++i)
    {
        int const x = group * 6 + i;
        if (x < width)
        {
            y[yRow + x] = ys[i];
        }
    }
    for (int i = 0; i < 3; ++i)
    {
        int const cx = group * 3 + i;
        if (cx < cw)
        {
            cb[cRow + cx] = cbs[i];
            cr[cRow + cx] = crs[i];
        }
    }
}

__global__ void unpackRgbaKernel(std::uint8_t const* v210, int rowBytes, int width, int height, Half4* rgba)
{
    int const group = blockIdx.x * blockDim.x + threadIdx.x;
    int const row = blockIdx.y;
    if (row >= height || group * 6 >= width)
    {
        return;
    }
    std::uint16_t ys[6];
    std::uint16_t cbs[3];
    std::uint16_t crs[3];
    readGroup(v210, rowBytes, row, group, ys, cbs, crs);
    std::size_t const base = static_cast<std::size_t>(row) * static_cast<std::size_t>(width);
    for (int i = 0; i < 6; ++i)
    {
        int const x = group * 6 + i;
        if (x < width)
        {
            // Co-sited chroma: both pixels of a pair take the pair's Cb/Cr.
            Rgb const c = ycbcrToRgb(ys[i], cbs[i / 2], crs[i / 2]);
            rgba[base + x] = Half4{__floats2half2_rn(c.r, c.g), __floats2half2_rn(c.b, 1.f)};
        }
    }
}

__global__ void mixU16Kernel(ushort4 const* a, ushort4 const* b, ushort4* out, std::size_t count, float t)
{
    std::size_t const i = static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (i >= count)
    {
        return;
    }
    ushort4 const va = a[i];
    ushort4 const vb = b[i];
    out[i] = make_ushort4(mix10(va.x, vb.x, t), mix10(va.y, vb.y, t), mix10(va.z, vb.z, t), mix10(va.w, vb.w, t));
}

__global__ void mixRgbaKernel(Half4 const* a, Half4 const* b, Half4* out, std::size_t count, float t)
{
    std::size_t const i = static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (i >= count)
    {
        return;
    }
    Half4 const va = a[i];
    Half4 const vb = b[i];
    float2 const arg = __half22float2(va.rg);
    float2 const aba = __half22float2(va.ba);
    float2 const brg = __half22float2(vb.rg);
    float2 const bba = __half22float2(vb.ba);
    out[i] = Half4{__floats2half2_rn(arg.x + (brg.x - arg.x) * t, arg.y + (brg.y - arg.y) * t),
        __floats2half2_rn(aba.x + (bba.x - aba.x) * t, aba.y + (bba.y - aba.y) * t)};
}

// One thread per 16-byte group of a line, including the padding groups (written as zero).
__global__ void packYuvKernel(std::uint16_t const* y, std::uint16_t const* cb, std::uint16_t const* cr, int width, int height, std::uint8_t* v210, int rowBytes)
{
    int const group = blockIdx.x * blockDim.x + threadIdx.x;
    int const row = blockIdx.y;
    if (row >= height || group * 16 >= rowBytes)
    {
        return;
    }
    std::uint16_t ys[6] = {};
    std::uint16_t cbs[3] = {};
    std::uint16_t crs[3] = {};
    int const cw = width / 2;
    std::size_t const yRow = static_cast<std::size_t>(row) * static_cast<std::size_t>(width);
    std::size_t const cRow = static_cast<std::size_t>(row) * static_cast<std::size_t>(cw);
    for (int i = 0; i < 6; ++i)
    {
        int const x = group * 6 + i;
        if (x < width)
        {
            ys[i] = y[yRow + x];
        }
    }
    for (int i = 0; i < 3; ++i)
    {
        int const cx = group * 3 + i;
        if (cx < cw)
        {
            cbs[i] = cb[cRow + cx];
            crs[i] = cr[cRow + cx];
        }
    }
    std::uint32_t w[4] = {};
    if (group * 6 < width)
    {
        packGroup(ys, cbs, crs, w);
    }
    writeGroup(v210, rowBytes, row, group, w);
}

__global__ void packRgbaKernel(Half4 const* rgba, int width, int height, std::uint8_t* v210, int rowBytes)
{
    int const group = blockIdx.x * blockDim.x + threadIdx.x;
    int const row = blockIdx.y;
    if (row >= height || group * 16 >= rowBytes)
    {
        return;
    }
    std::uint32_t w[4] = {};
    if (group * 6 < width)
    {
        std::uint16_t ys[6] = {};
        std::uint16_t cbs[3] = {};
        std::uint16_t crs[3] = {};
        std::size_t const base = static_cast<std::size_t>(row) * static_cast<std::size_t>(width);
        for (int pair = 0; pair < 3; ++pair)
        {
            unsigned cbSum = 0;
            unsigned crSum = 0;
            int n = 0;
            for (int k = 0; k < 2; ++k)
            {
                int const i = pair * 2 + k;
                int const x = group * 6 + i;
                if (x >= width)
                {
                    continue;
                }
                Half4 const p = rgba[base + x];
                float2 const rg = __half22float2(p.rg);
                float2 const ba = __half22float2(p.ba);
                std::uint16_t yy = 0;
                std::uint16_t cbv = 0;
                std::uint16_t crv = 0;
                rgbToYcbcr(Rgb{rg.x, rg.y, ba.x}, yy, cbv, crv);
                ys[i] = yy;
                cbSum += cbv;
                crSum += crv;
                ++n;
            }
            if (n > 0)
            {
                cbs[pair] = static_cast<std::uint16_t>((cbSum + n / 2) / n);
                crs[pair] = static_cast<std::uint16_t>((crSum + n / 2) / n);
            }
        }
        packGroup(ys, cbs, crs, w);
    }
    writeGroup(v210, rowBytes, row, group, w);
}

__global__ void fillBlackKernel(std::uint8_t* v210, int rowBytes, int width, int height)
{
    int const group = blockIdx.x * blockDim.x + threadIdx.x;
    int const row = blockIdx.y;
    if (row >= height || group * 16 >= rowBytes)
    {
        return;
    }
    std::uint32_t w[4] = {};
    if (group * 6 < width)
    {
        std::uint16_t const ys[6] = {64, 64, 64, 64, 64, 64};
        std::uint16_t const cs[3] = {512, 512, 512};
        packGroup(ys, cs, cs, w);
    }
    writeGroup(v210, rowBytes, row, group, w);
}

struct MosaicArgs
{
    MosaicSource tiles[kMaxMosaicTiles];
    int count;
};

// One thread per 2×2 block of a tile: four luma samples and one NV12 chroma pair, each the
// average of its source area.
__global__ void mosaicKernel(MosaicArgs args, std::uint8_t* luma, int lumaPitch, std::uint8_t* chroma, int chromaPitch)
{
    int const tileIndex = blockIdx.z;
    if (tileIndex >= args.count)
    {
        return;
    }
    MosaicSource const tile = args.tiles[tileIndex];
    int const bx = blockIdx.x * blockDim.x + threadIdx.x;
    int const by = blockIdx.y * blockDim.y + threadIdx.y;
    if (bx * 2 >= tile.w || by * 2 >= tile.h)
    {
        return;
    }
    int const ox = tile.x + bx * 2;
    int const oy = tile.y + by * 2;
    std::uint8_t* const chromaOut = chroma + static_cast<std::size_t>(oy / 2) * chromaPitch + ox;
    if (tile.v210 == nullptr)
    {
        for (int r = 0; r < 2; ++r)
        {
            luma[static_cast<std::size_t>(oy + r) * lumaPitch + ox] = 16;
            luma[static_cast<std::size_t>(oy + r) * lumaPitch + ox + 1] = 16;
        }
        chromaOut[0] = 128;
        chromaOut[1] = 128;
        return;
    }
    auto const span = [](int d, int size, int src, int& s0, int& s1) {
        s0 = static_cast<int>(static_cast<long long>(d) * src / size);
        s1 = static_cast<int>(static_cast<long long>(d + 1) * src / size);
        if (s1 <= s0)
        {
            s1 = s0 + 1;
        }
    };
    for (int r = 0; r < 2; ++r)
    {
        int y0 = 0;
        int y1 = 0;
        span(by * 2 + r, tile.h, tile.height, y0, y1);
        for (int c = 0; c < 2; ++c)
        {
            int x0 = 0;
            int x1 = 0;
            span(bx * 2 + c, tile.w, tile.width, x0, x1);
            unsigned sum = 0;
            for (int sy = y0; sy < y1; ++sy)
            {
                auto const* words = lineWords(tile.v210, tile.rowBytes, sy);
                for (int sx = x0; sx < x1; ++sx)
                {
                    sum += lumaAt(words, sx);
                }
            }
            unsigned const n = static_cast<unsigned>((y1 - y0) * (x1 - x0));
            luma[static_cast<std::size_t>(oy + r) * lumaPitch + ox + c] = to8((sum + n / 2) / n);
        }
    }
    // Chroma of the 2×2 block: the 4:2:2 chroma pairs under its source area.
    int y0 = 0;
    int y1 = 0;
    int x0 = 0;
    int x1 = 0;
    span(by, tile.h / 2, tile.height, y0, y1);
    span(bx, tile.w / 2, tile.width, x0, x1);
    int const c0 = x0 / 2;
    int const c1 = max((x1 + 1) / 2, c0 + 1);
    unsigned cbSum = 0;
    unsigned crSum = 0;
    for (int sy = y0; sy < y1; ++sy)
    {
        auto const* words = lineWords(tile.v210, tile.rowBytes, sy);
        for (int cx = c0; cx < c1; ++cx)
        {
            cbSum += cbAt(words, cx);
            crSum += crAt(words, cx);
        }
    }
    unsigned const n = static_cast<unsigned>((y1 - y0) * (c1 - c0));
    chromaOut[0] = to8((cbSum + n / 2) / n);
    chromaOut[1] = to8((crSum + n / 2) / n);
}

std::uint16_t* planeY(WorkBuffer const& b)
{
    return static_cast<std::uint16_t*>(b.data);
}

std::uint16_t* planeCb(WorkBuffer const& b)
{
    return planeY(b) + static_cast<std::size_t>(b.width) * static_cast<std::size_t>(b.height);
}

std::uint16_t* planeCr(WorkBuffer const& b)
{
    return planeCb(b) + static_cast<std::size_t>(b.width / 2) * static_cast<std::size_t>(b.height);
}

dim3 groupGrid(int groups, int height)
{
    return dim3(static_cast<unsigned>((groups + 127) / 128), static_cast<unsigned>(height));
}
} // namespace

std::size_t workBytes(WorkFormat format, int width, int height)
{
    auto const pixels = static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    return format == WorkFormat::Yuv16 ? pixels * 2u * sizeof(std::uint16_t) : pixels * sizeof(Half4);
}

cudaError_t allocWork(WorkBuffer& buffer, WorkFormat format, int width, int height)
{
    freeWork(buffer);
    buffer.format = format;
    buffer.width = width;
    buffer.height = height;
    buffer.bytes = workBytes(format, width, height);
    return cudaMalloc(&buffer.data, buffer.bytes);
}

void freeWork(WorkBuffer& buffer)
{
    if (buffer.data != nullptr)
    {
        cudaFree(buffer.data);
    }
    buffer.data = nullptr;
    buffer.bytes = 0;
}

cudaError_t unpack(cudaStream_t stream, std::uint8_t const* v210, int rowBytes, WorkBuffer& out)
{
    int const groups = (out.width + 5) / 6;
    if (out.format == WorkFormat::Yuv16)
    {
        unpackYuvKernel<<<groupGrid(groups, out.height), 128, 0, stream>>>(v210, rowBytes, out.width, out.height, planeY(out), planeCb(out), planeCr(out));
    }
    else
    {
        unpackRgbaKernel<<<groupGrid(groups, out.height), 128, 0, stream>>>(v210, rowBytes, out.width, out.height, static_cast<Half4*>(out.data));
    }
    return cudaGetLastError();
}

cudaError_t mix(cudaStream_t stream, WorkBuffer const& a, WorkBuffer const* b, float t, WorkBuffer& out)
{
    if (b == nullptr || t <= 0.f)
    {
        return cudaMemcpyAsync(out.data, a.data, out.bytes, cudaMemcpyDeviceToDevice, stream);
    }
    if (out.format == WorkFormat::Yuv16)
    {
        std::size_t const count = out.bytes / sizeof(ushort4);
        mixU16Kernel<<<static_cast<unsigned>((count + 255) / 256), 256, 0, stream>>>(static_cast<ushort4 const*>(a.data), static_cast<ushort4 const*>(b->data),
            static_cast<ushort4*>(out.data), count, t);
    }
    else
    {
        std::size_t const count = out.bytes / sizeof(Half4);
        mixRgbaKernel<<<static_cast<unsigned>((count + 255) / 256), 256, 0, stream>>>(static_cast<Half4 const*>(a.data), static_cast<Half4 const*>(b->data),
            static_cast<Half4*>(out.data), count, t);
    }
    return cudaGetLastError();
}

cudaError_t pack(cudaStream_t stream, WorkBuffer const& in, std::uint8_t* v210, int rowBytes)
{
    int const groups = rowBytes / 16;
    if (in.format == WorkFormat::Yuv16)
    {
        packYuvKernel<<<groupGrid(groups, in.height), 128, 0, stream>>>(planeY(in), planeCb(in), planeCr(in), in.width, in.height, v210, rowBytes);
    }
    else
    {
        packRgbaKernel<<<groupGrid(groups, in.height), 128, 0, stream>>>(static_cast<Half4 const*>(in.data), in.width, in.height, v210, rowBytes);
    }
    return cudaGetLastError();
}

cudaError_t fillBlack(cudaStream_t stream, std::uint8_t* v210, int rowBytes, int width, int height)
{
    fillBlackKernel<<<groupGrid(rowBytes / 16, height), 128, 0, stream>>>(v210, rowBytes, width, height);
    return cudaGetLastError();
}

cudaError_t mosaic(cudaStream_t stream, MosaicSource const* tiles, int count, std::uint8_t* luma, int lumaPitch, std::uint8_t* chroma, int chromaPitch)
{
    if (count <= 0)
    {
        return cudaSuccess;
    }
    MosaicArgs args{};
    args.count = std::min(count, kMaxMosaicTiles);
    int maxW = 0;
    int maxH = 0;
    for (int i = 0; i < args.count; ++i)
    {
        args.tiles[i] = tiles[i];
        maxW = std::max(maxW, tiles[i].w);
        maxH = std::max(maxH, tiles[i].h);
    }
    dim3 const block(16, 16);
    dim3 const grid(static_cast<unsigned>((maxW / 2 + 15) / 16), static_cast<unsigned>((maxH / 2 + 15) / 16), static_cast<unsigned>(args.count));
    mosaicKernel<<<grid, block, 0, stream>>>(args, luma, lumaPitch, chroma, chromaPitch);
    return cudaGetLastError();
}

namespace
{
// Device buffer freed on scope exit (self-test only).
struct DeviceBytes
{
    void* ptr = nullptr;
    explicit DeviceBytes(std::size_t bytes)
    {
        if (cudaMalloc(&ptr, bytes) != cudaSuccess)
        {
            ptr = nullptr;
        }
    }
    ~DeviceBytes()
    {
        if (ptr != nullptr)
        {
            cudaFree(ptr);
        }
    }
    DeviceBytes(DeviceBytes const&) = delete;
    DeviceBytes& operator=(DeviceBytes const&) = delete;
    std::uint8_t* get() const
    {
        return static_cast<std::uint8_t*>(ptr);
    }
};

void randomPicture(Planar422& p, unsigned seed)
{
    unsigned state = seed;
    auto const next = [&state] {
        state = state * 1664525u + 1013904223u;
        return static_cast<std::uint16_t>(64 + (state >> 8) % 877);
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

int maxDiff(Planar422 const& a, Planar422 const& b)
{
    int worst = 0;
    for (std::size_t i = 0; i < a.y.size(); ++i)
    {
        worst = std::max(worst, std::abs(int(a.y[i]) - int(b.y[i])));
    }
    for (std::size_t i = 0; i < a.cb.size(); ++i)
    {
        worst = std::max(worst, std::abs(int(a.cb[i]) - int(b.cb[i])));
        worst = std::max(worst, std::abs(int(a.cr[i]) - int(b.cr[i])));
    }
    return worst;
}
} // namespace

bool selfTest(std::string& report)
{
    int const w = 1920;
    int const h = 1080;
    int const rowBytes = v210RowBytes(w);
    std::size_t const frameBytes = static_cast<std::size_t>(rowBytes) * h;
    Planar422 pa;
    Planar422 pb;
    pa.allocate(w, h);
    pb.allocate(w, h);
    randomPicture(pa, 1);
    randomPicture(pb, 2);
    std::vector<std::uint8_t> va(frameBytes);
    std::vector<std::uint8_t> vb(frameBytes);
    std::vector<std::uint8_t> back(frameBytes);
    packV210(pa, va.data(), rowBytes);
    packV210(pb, vb.data(), rowBytes);
    DeviceBytes da(frameBytes);
    DeviceBytes db(frameBytes);
    DeviceBytes dout(frameBytes);
    if (da.get() == nullptr || db.get() == nullptr || dout.get() == nullptr)
    {
        report = "cudaMalloc failed";
        return false;
    }
    cudaMemcpy(da.get(), va.data(), frameBytes, cudaMemcpyHostToDevice);
    cudaMemcpy(db.get(), vb.data(), frameBytes, cudaMemcpyHostToDevice);
    std::string done;
    for (auto const format : {WorkFormat::Yuv16, WorkFormat::Rgba16f})
    {
        char const* name = format == WorkFormat::Yuv16 ? "yuv16" : "rgba16f";
        WorkBuffer wa;
        WorkBuffer wb;
        WorkBuffer wo;
        if (allocWork(wa, format, w, h) != cudaSuccess || allocWork(wb, format, w, h) != cudaSuccess || allocWork(wo, format, w, h) != cudaSuccess)
        {
            report = std::string(name) + ": allocWork failed";
            return false;
        }
        // Round trip.
        unpack(nullptr, da.get(), rowBytes, wa);
        pack(nullptr, wa, dout.get(), rowBytes);
        cudaError_t err = cudaDeviceSynchronize();
        cudaMemcpy(back.data(), dout.get(), frameBytes, cudaMemcpyDeviceToHost);
        Planar422 got;
        got.allocate(w, h);
        unpackV210(back.data(), rowBytes, got);
        int const roundTrip = maxDiff(pa, got);
        int const limit = format == WorkFormat::Yuv16 ? 0 : 2;
        if (err != cudaSuccess || roundTrip > limit)
        {
            report = std::string(name) + ": round trip max error " + std::to_string(roundTrip) + " (" + cudaGetErrorString(err) + ")";
            freeWork(wa);
            freeWork(wb);
            freeWork(wo);
            return false;
        }
        // Dissolve at t = 0.3 against the CPU maths.
        unpack(nullptr, db.get(), rowBytes, wb);
        mix(nullptr, wa, &wb, 0.3f, wo);
        pack(nullptr, wo, dout.get(), rowBytes);
        err = cudaDeviceSynchronize();
        cudaMemcpy(back.data(), dout.get(), frameBytes, cudaMemcpyDeviceToHost);
        unpackV210(back.data(), rowBytes, got);
        Planar422 want;
        want.allocate(w, h);
        for (std::size_t i = 0; i < want.y.size(); ++i)
        {
            want.y[i] = mix10(pa.y[i], pb.y[i], 0.3f);
        }
        for (std::size_t i = 0; i < want.cb.size(); ++i)
        {
            want.cb[i] = mix10(pa.cb[i], pb.cb[i], 0.3f);
            want.cr[i] = mix10(pa.cr[i], pb.cr[i], 0.3f);
        }
        int const dissolve = maxDiff(want, got);
        // rgba16f mixes in R'G'B'; a different but equally valid dissolve, so only a sanity bound.
        int const mixLimit = format == WorkFormat::Yuv16 ? 0 : 8;
        freeWork(wa);
        freeWork(wb);
        freeWork(wo);
        if (err != cudaSuccess || dissolve > mixLimit)
        {
            report = std::string(name) + ": dissolve max error " + std::to_string(dissolve) + " (" + cudaGetErrorString(err) + ")";
            return false;
        }
        done += std::string(name) + " round trip max " + std::to_string(roundTrip) + ", dissolve max " + std::to_string(dissolve) + "; ";
    }
    // Mosaic of a flat grey picture: every tile is that grey, a missing source is black.
    Planar422 grey;
    grey.allocate(w, h);
    std::fill(grey.y.begin(), grey.y.end(), static_cast<std::uint16_t>(502));
    packV210(grey, va.data(), rowBytes);
    cudaMemcpy(da.get(), va.data(), frameBytes, cudaMemcpyHostToDevice);
    DeviceBytes nv12(static_cast<std::size_t>(w) * h * 3 / 2);
    MosaicSource tiles[2];
    tiles[0] = MosaicSource{da.get(), rowBytes, w, h, 0, 0, w / 4, h / 4};
    tiles[1] = MosaicSource{nullptr, 0, 0, 0, w / 4, 0, w / 4, h / 4};
    mosaic(nullptr, tiles, 2, nv12.get(), w, nv12.get() + static_cast<std::size_t>(w) * h, w);
    std::vector<std::uint8_t> pic(static_cast<std::size_t>(w) * h * 3 / 2);
    cudaError_t const err = cudaDeviceSynchronize();
    cudaMemcpy(pic.data(), nv12.get(), pic.size(), cudaMemcpyDeviceToHost);
    std::uint8_t const greyY = pic[static_cast<std::size_t>(100) * w + 100];
    std::uint8_t const blackY = pic[static_cast<std::size_t>(100) * w + w / 4 + 100];
    std::uint8_t const greyU = pic[static_cast<std::size_t>(w) * h + static_cast<std::size_t>(50) * w + 100];
    if (err != cudaSuccess || greyY != to8(502) || blackY != 16 || greyU != 128)
    {
        report = "mosaic: grey Y " + std::to_string(greyY) + " (want " + std::to_string(to8(502)) + "), black Y " + std::to_string(blackY) + ", U " +
                 std::to_string(greyU) + " (" + cudaGetErrorString(err) + ")";
        return false;
    }
    report = done + "mosaic ok";
    return true;
}
} // namespace fxeng::gpu
