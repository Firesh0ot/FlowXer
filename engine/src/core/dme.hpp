// DME boxes (docs/engine-p4.md §3): a keyer's fill (and key) cropped, scaled and placed with
// sub-pixel accuracy, with an outer border and a rectangular drop shadow. Two steps per grain:
// a separable resample into the box buffer (weights per column and row, then a horizontal and
// a vertical pass), and the box layer in the fused compose. One code path for the CUDA kernels
// and the CPU reference: only + − × ÷ and floor, compiled without FMA contraction (-fmad=false,
// -ffp-contract=off), so the GPU and the CPU agree bit for bit.
#pragma once

#include "core/pixel.hpp"

#include <math.h>

#include <cstdint>
#include <vector>

#if defined(__CUDACC__)
#define FXENG_UNROLL _Pragma("unroll")
#else
#define FXENG_UNROLL
#endif

namespace fxeng
{
struct Planar422;

// What a box shows: a source as an opaque picture (a `pip` keyer), a luma key of a source, or a
// linear key's v210a fill + key (premultiplied).
enum class DmeKind : int
{
    Pip = 0,
    Luma = 1,
    Linear = 2
};

// A keyer's box. x, y: the centre of the (uncropped) picture as fractions of the raster; size:
// its scale (1 = the full raster); crop: fractions of the source cut away (the box shrinks, the
// picture does not move); soft: the picture's soft edge without a border; border and shadow in
// raster pixels, colours 10-bit Y'CbCr.
struct DmeSettings
{
    bool enabled = false;
    float x = 0.5f;
    float y = 0.5f;
    float size = 0.5f;
    float cropLeft = 0.f;
    float cropRight = 0.f;
    float cropTop = 0.f;
    float cropBottom = 0.f;
    float soft = 0.f;
    float borderWidth = 0.f;
    float borderSoft = 0.f;
    float borderOpacity = 1.f;
    std::uint16_t borderY = 940;
    std::uint16_t borderCb = 512;
    std::uint16_t borderCr = 512;
    float shadowDx = 0.f;
    float shadowDy = 0.f;
    float shadowBlur = 0.f;
    float shadowOpacity = 0.f;
};

// The ranges of docs/engine-p4.md §3.
inline bool dmeValid(DmeSettings const& s)
{
    auto const in = [](float v, float lo, float hi) { return v >= lo && v <= hi; };
    return in(s.x, -1.f, 2.f) && in(s.y, -1.f, 2.f) && in(s.size, 0.f, 2.f) && in(s.cropLeft, 0.f, 1.f) && in(s.cropRight, 0.f, 1.f) &&
           in(s.cropTop, 0.f, 1.f) && in(s.cropBottom, 0.f, 1.f) && s.cropLeft + s.cropRight < 1.f && s.cropTop + s.cropBottom < 1.f &&
           in(s.soft, 0.f, 64.f) && in(s.borderWidth, 0.f, 64.f) && in(s.borderSoft, 0.f, 64.f) && in(s.borderOpacity, 0.f, 1.f) &&
           s.borderY <= 1023 && s.borderCb <= 1023 && s.borderCr <= 1023 && in(s.shadowDx, -128.f, 128.f) && in(s.shadowDy, -128.f, 128.f) &&
           in(s.shadowBlur, 0.f, 128.f) && in(s.shadowOpacity, 0.f, 1.f);
}

// Taps per output sample: Catmull-Rom (support 2) widened by 1 / size when shrinking, at most
// 32 (the widening stops at size 1/8: smaller boxes alias a little).
constexpr int kDmeMaxTaps = 32;
constexpr float kDmeMinFilterScale = 0.125f;

// One grain's box on a W × H raster (makeDmeBox).
struct DmeBox
{
    DmeKind kind = DmeKind::Pip;
    int width = 0;
    int height = 0;
    // Scale; the raster position of the uncropped source's top-left corner.
    float s = 1.f;
    float dx = 0.f;
    float dy = 0.f;
    // Filter scale (min(s, 1), at least 1/8), support in source samples (2 / fs), and the taps
    // per sample (4, 8, 12, 16, 24 or 32: the kernels are compiled per count).
    float fs = 1.f;
    float sup = 2.f;
    int taps = 4;
    // The crop in source samples: columns [cl, cr) (even, so chroma has whole samples), rows
    // [ct, cb).
    int cl = 0;
    int cr = 0;
    int ct = 0;
    int cb = 0;
    // The picture's rectangle in the raster.
    float L = 0.f;
    float R = 0.f;
    float T = 0.f;
    float B = 0.f;
    // The box buffer covers the raster's [bx, bx + bw) × [by, by + bh) (bx, bw even; bw = 0: no
    // picture on the raster). Its lines are [Y bw | Cb bw/2 | Cr bw/2 | key bw (linear)], `cols`
    // samples.
    int bx = 0;
    int by = 0;
    int bw = 0;
    int bh = 0;
    int cols = 0;
    // The source rows the horizontal pass computes: [r0, r0 + rows).
    int r0 = 0;
    int rows = 0;
    // The raster area the layer can change: [ex0, ex1) × [ey0, ey1).
    int ex0 = 0;
    int ex1 = 0;
    int ey0 = 0;
    int ey1 = 0;
    // Decoration.
    float soft = 0.f;
    float borderWidth = 0.f;
    float borderSoft = 0.f;
    float borderOpacity = 0.f;
    std::uint16_t borderY = 940;
    std::uint16_t borderCb = 512;
    std::uint16_t borderCr = 512;
    float shadowDx = 0.f;
    float shadowDy = 0.f;
    float shadowBlur = 0.f;
    float shadowOpacity = 0.f;
    // 1 / soft (0 without), 1 / borderSoft, 1 / shadowBlur (1 without a blur).
    float invSoft = 0.f;
    float invBorderSoft = 0.f;
    float invBlur = 1.f;
};

FXENG_HD inline float dmeMin(float a, float b)
{
    return a < b ? a : b;
}

FXENG_HD inline float dmeMax(float a, float b)
{
    return a > b ? a : b;
}

FXENG_HD inline int dmeClampI(int v, int lo, int hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

// The Catmull-Rom cubic (a = −0.5).
FXENG_HD inline float catmullRom(float x)
{
    x = x < 0.f ? -x : x;
    if (x < 1.f)
    {
        return (1.5f * x - 2.5f) * x * x + 1.f;
    }
    if (x < 2.f)
    {
        return ((-0.5f * x + 2.5f) * x - 4.f) * x + 2.f;
    }
    return 0.f;
}

// The filter window at continuous source index c: samples floor(c − sup) + 1 … floor(c + sup).
FXENG_HD inline int dmeFirstTap(float c, float sup)
{
    return static_cast<int>(floorf(c - sup)) + 1;
}

FXENG_HD inline int dmeLastTap(float c, float sup)
{
    return static_cast<int>(floorf(c + sup));
}

// The normalised weights of one output sample at continuous source index c: taps i0 … i0 + taps
// − 1 (zero past the window). Returns i0.
FXENG_HD inline int dmeWeights(float c, float fs, float sup, int taps, float* w)
{
    int const i0 = dmeFirstTap(c, sup);
    int const i1 = dmeLastTap(c, sup);
    float sum = 0.f;
    for (int i = 0; i < taps; ++i)
    {
        float const v = i0 + i <= i1 ? catmullRom((static_cast<float>(i0 + i) - c) * fs) : 0.f;
        w[i] = v;
        sum += v;
    }
    for (int i = 0; i < taps; ++i)
    {
        w[i] = w[i] / sum;
    }
    return i0;
}

// Continuous source indices: an output pixel centre X + ½ maps to u = (X + ½ − dx) / s, luma
// index u − ½. Chroma (BT.709, co-sited with the even luma samples) takes (u − ½) / 2 at the
// output's luma position 2k. Rows likewise.
FXENG_HD inline float dmeLumaIndex(DmeBox const& b, int ox)
{
    return (static_cast<float>(b.bx + ox) + 0.5f - b.dx) / b.s - 0.5f;
}

FXENG_HD inline float dmeChromaIndex(DmeBox const& b, int ox)
{
    return ((static_cast<float>(b.bx + 2 * ox) + 0.5f - b.dx) / b.s - 0.5f) * 0.5f;
}

FXENG_HD inline float dmeRowIndex(DmeBox const& b, int oy)
{
    return (static_cast<float>(b.by + oy) + 0.5f - b.dy) / b.s - 0.5f;
}

// Weight table entries: luma columns [0, bw), chroma columns [bw, bw + bw/2), rows [bw + bw/2,
// bw + bw/2 + bh).
FXENG_HD inline int dmeEntries(DmeBox const& b)
{
    return b.bw + b.bw / 2 + b.bh;
}

FXENG_HD inline float dmeEntryIndex(DmeBox const& b, int e)
{
    int const cw = b.bw / 2;
    return e < b.bw ? dmeLumaIndex(b, e) : (e < b.bw + cw ? dmeChromaIndex(b, e - b.bw) : dmeRowIndex(b, e - b.bw - cw));
}

// The plane of buffer column e (0 Y, 1 Cb, 2 Cr, 3 key), its table entry, and the crop's source
// columns [lo, hi) on that plane.
FXENG_HD inline int dmePlaneOf(DmeBox const& b, int e, int& entry, int& lo, int& hi)
{
    int const cw = b.bw / 2;
    int plane = 0;
    if (e < b.bw)
    {
        entry = e;
    }
    else if (e < b.bw + 2 * cw)
    {
        plane = e < b.bw + cw ? 1 : 2;
        entry = b.bw + (e - b.bw) % cw;
    }
    else
    {
        plane = 3;
        entry = e - b.bw - 2 * cw;
    }
    bool const chroma = plane == 1 || plane == 2;
    lo = chroma ? b.cl / 2 : b.cl;
    hi = chroma ? b.cr / 2 : b.cr;
    return plane;
}

// A weight table on the host or the device: start[entry], w[i · entries + entry].
struct DmeTable
{
    int const* start = nullptr;
    float const* w = nullptr;
    int entries = 0;
};

// The box's source: an opaque picture (yuv16 planes: Y W × H, Cb and Cr W/2 × H) or, for a
// linear box, a v210a fill + key (the key plane after the fill), read premultiplied.
struct DmeSource
{
    std::uint16_t const* plane[3] = {nullptr, nullptr, nullptr};
    std::uint8_t const* v210a = nullptr;
    int rowBytes = 0;
    int keyRowBytes = 0;
    int width = 0;
    int height = 0;
};

// Sample j of a linear box's v210a line (`words`, its key line `keyLine`), premultiplied:
// (Y − 64) · k / 1023, (C − 512) · the pair's mean k / 1023; plane 3 is the key itself.
FXENG_HD inline float dmeFetchLinear(std::uint32_t const* words, std::uint8_t const* keyLine, int plane, int j)
{
    if (plane == 3)
    {
        return static_cast<float>(keyAt(keyLine, j));
    }
    if (plane == 0)
    {
        return (static_cast<float>(v210Luma(words, j)) - 64.f) * (static_cast<float>(keyAt(keyLine, j)) * (1.f / 1023.f));
    }
    unsigned const c = plane == 1 ? v210Cb(words, j) : v210Cr(words, j);
    unsigned const k = keyAt(keyLine, 2 * j) + keyAt(keyLine, 2 * j + 1);
    return (static_cast<float>(c) - 512.f) * (static_cast<float>(k) * (1.f / 2046.f));
}

// The horizontal pass keeps 16 × the value as int16 (signed: a premultiplied fill).
FXENG_HD inline std::int16_t dmeToMid(float acc)
{
    float v = acc * 16.f;
    v = v < -32768.f ? -32768.f : (v > 32767.f ? 32767.f : v);
    return static_cast<std::int16_t>(floorf(v + 0.5f));
}

// The vertical pass: a 10-bit sample, or a premultiplied fill sample (−1023 … 1023).
FXENG_HD inline std::int16_t dmeToBox(float acc, bool premultiplied)
{
    float const v = acc * (1.f / 16.f);
    if (!premultiplied)
    {
        return static_cast<std::int16_t>(clamp10(v));
    }
    float const c = v < -1023.f ? -1023.f : (v > 1023.f ? 1023.f : v);
    return static_cast<std::int16_t>(floorf(c + 0.5f));
}

// The taps of table entry `entry`: weights and source indices clamped to [lo, hi) (the crop's
// edge samples repeat).
template <int N>
FXENG_HD inline void dmeTaps(DmeTable const& t, int entry, int lo, int hi, float (&w)[N], int (&j)[N])
{
    int const i0 = t.start[entry];
    FXENG_UNROLL
    for (int i = 0; i < N; ++i)
    {
        w[i] = t.w[static_cast<std::size_t>(i) * static_cast<std::size_t>(t.entries) + static_cast<std::size_t>(entry)];
        j[i] = dmeClampI(i0 + i, lo, hi - 1);
    }
}

// A source line of `plane` on source row `row`: an opaque source's samples, or a linear
// source's v210a words and key line.
FXENG_HD inline std::uint16_t const* dmeLine(DmeSource const& src, int plane, int row)
{
    std::uint16_t const* base = plane == 0 ? src.plane[0] : (plane == 1 ? src.plane[1] : src.plane[2]);
    int const width = plane == 0 ? src.width : src.width / 2;
    return base + static_cast<std::size_t>(row) * static_cast<std::size_t>(width);
}

FXENG_HD inline std::uint32_t const* dmeWords(DmeSource const& src, int row)
{
    return reinterpret_cast<std::uint32_t const*>(src.v210a + static_cast<std::size_t>(row) * static_cast<std::size_t>(src.rowBytes));
}

FXENG_HD inline std::uint8_t const* dmeKeyLine(DmeSource const& src, int row)
{
    return src.v210a + static_cast<std::size_t>(src.rowBytes) * static_cast<std::size_t>(src.height) +
           static_cast<std::size_t>(row) * static_cast<std::size_t>(src.keyRowBytes);
}

// One horizontal-pass sample of an opaque source line.
template <int N>
FXENG_HD inline std::int16_t dmeHSampleOpaque(std::uint16_t const* line, float const (&w)[N], int const (&j)[N])
{
    float acc = 0.f;
    FXENG_UNROLL
    for (int i = 0; i < N; ++i)
    {
        acc += w[i] * static_cast<float>(line[j[i]]);
    }
    return dmeToMid(acc);
}

// One horizontal-pass sample of a linear source line (premultiplied, or the key).
template <int N>
FXENG_HD inline std::int16_t dmeHSampleLinear(std::uint32_t const* words, std::uint8_t const* keyLine, int plane, float const (&w)[N], int const (&j)[N])
{
    float acc = 0.f;
    FXENG_UNROLL
    for (int i = 0; i < N; ++i)
    {
        acc += w[i] * dmeFetchLinear(words, keyLine, plane, j[i]);
    }
    return dmeToMid(acc);
}

// One horizontal-pass sample: column `plane` taps over source row `row`.
template <int N>
FXENG_HD inline std::int16_t dmeHSample(DmeSource const& src, int plane, int row, float const (&w)[N], int const (&j)[N])
{
    if (src.v210a == nullptr)
    {
        return dmeHSampleOpaque<N>(dmeLine(src, plane, row), w, j);
    }
    return dmeHSampleLinear<N>(dmeWords(src, row), dmeKeyLine(src, row), plane, w, j);
}

// The taps of box row oy for the vertical pass: weights and lines of the horizontal pass's rows
// (`mid`: b.rows lines of b.cols, source rows r0 …; the crop's edge rows repeat).
template <int N>
FXENG_HD inline void dmeRowTaps(DmeBox const& b, DmeTable const& t, int oy, float (&w)[N], int (&r)[N])
{
    int const entry = b.bw + b.bw / 2 + oy;
    int const i0 = t.start[entry];
    FXENG_UNROLL
    for (int i = 0; i < N; ++i)
    {
        w[i] = t.w[static_cast<std::size_t>(i) * static_cast<std::size_t>(t.entries) + static_cast<std::size_t>(entry)];
        r[i] = dmeClampI(dmeClampI(i0 + i, b.ct, b.cb - 1) - b.r0, 0, b.rows - 1);
    }
}

// One vertical-pass sample of buffer column e from its row taps.
template <int N>
FXENG_HD inline std::int16_t dmeVSampleTaps(DmeBox const& b, std::int16_t const* mid, int e, float const* w, int const* r)
{
    float acc = 0.f;
    FXENG_UNROLL
    for (int i = 0; i < N; ++i)
    {
        acc += w[i] * static_cast<float>(mid[static_cast<std::size_t>(r[i]) * static_cast<std::size_t>(b.cols) + static_cast<std::size_t>(e)]);
    }
    bool const premultiplied = b.kind == DmeKind::Linear && e < 2 * b.bw;
    return dmeToBox(acc, premultiplied);
}

// One vertical-pass sample of buffer column e, box row oy.
template <int N>
FXENG_HD inline std::int16_t dmeVSample(DmeBox const& b, DmeTable const& t, std::int16_t const* mid, int e, int oy)
{
    float w[N];
    int r[N];
    dmeRowTaps<N>(b, t, oy, w, r);
    return dmeVSampleTaps<N>(b, mid, e, w, r);
}

// Calls f(std::integral_constant<int, taps>) for the box's tap count.
template <int N>
struct DmeTaps
{
    static constexpr int value = N;
};

template <typename F>
void dmeDispatch(int taps, F&& f)
{
    switch (taps)
    {
    case 4:
        f(DmeTaps<4>{});
        break;
    case 8:
        f(DmeTaps<8>{});
        break;
    case 12:
        f(DmeTaps<12>{});
        break;
    case 16:
        f(DmeTaps<16>{});
        break;
    case 24:
        f(DmeTaps<24>{});
        break;
    default:
        f(DmeTaps<32>{});
        break;
    }
}

// Coverage of the pixel [x, x + 1) by [l, r): the exact area for a hard edge (a one-pixel
// anti-aliased edge), else a linear ramp over `soft` px inside the edge (`inv` = 1 / soft).
FXENG_HD inline float dmeCover(float x, float l, float r, float soft, float inv)
{
    if (soft <= 0.f)
    {
        return clamp01(dmeMin(x + 1.f, r) - dmeMax(x, l));
    }
    float const c = x + 0.5f;
    return clamp01((dmeMin(c - l, r - c) + 0.5f) * inv);
}

FXENG_HD inline float dmeSmooth(float t)
{
    t = clamp01(t);
    return t * t * (3.f - 2.f * t);
}

// The factors of one axis at raster position v (a column or a row): the picture's and the
// border's coverage and the shadow's ramp. [lo, hi) is the picture on that axis, `shift` the
// shadow's offset. The border is the outer rectangle (picture + width), soft on its outside; the
// shadow is the outer rectangle moved by the offset, a smoothstep ramp over `blur` px at each
// edge. A pixel's αs are the products of its column's and its row's factors.
struct DmeAxis
{
    float picture = 0.f;
    float border = 0.f;
    float shadow = 0.f;
};

FXENG_HD inline DmeAxis dmeAxis(DmeBox const& b, float v, float lo, float hi, float shift)
{
    DmeAxis a;
    float const bw = b.borderWidth;
    bool const hasBorder = bw > 0.f;
    a.picture = hasBorder ? dmeCover(v, lo, hi, 0.f, 0.f) : dmeCover(v, lo, hi, b.soft, b.invSoft);
    float const ol = lo - bw;
    float const oh = hi + bw;
    a.border = hasBorder ? dmeCover(v, ol, oh, b.borderSoft, b.invBorderSoft) : 0.f;
    if (b.shadowOpacity > 0.f)
    {
        float const c = v + 0.5f - shift;
        a.shadow = dmeSmooth((c - ol) * b.invBlur + 0.5f) * dmeSmooth((oh - c) * b.invBlur + 0.5f);
    }
    return a;
}

FXENG_HD inline DmeAxis dmeRowAxis(DmeBox const& b, int row)
{
    return dmeAxis(b, static_cast<float>(row), b.T, b.B, b.shadowDy);
}

// A box layer as the compose draws it: the box, its buffer, and the keyer's gain and luma key.
struct DmeDraw
{
    DmeBox box;
    std::int16_t const* buffer = nullptr;
    float gain = 0.f;
    float clip = 0.f;
    float soft = 0.f;
    bool invert = false;
};

// The box layer on the pixel pair (x, x + 1) of raster row `row` (x even; `ry` its dmeRowAxis):
// shadow towards black,
// then the border colour, then the picture, each rounded to 10 bits. The keyer's gain scales
// every α; chroma takes the pair's mean α. A pip is opaque; a luma box keys its picture by its
// luma; a linear box is premultiplied over: out = bg · (1 − a) + black · a + P · g.
FXENG_HD inline void dmePair(DmeDraw const& d, DmeAxis const& ry, int x, int row, std::uint16_t y[2], std::uint16_t& cb, std::uint16_t& cr)
{
    DmeBox const& b = d.box;
    float sh[2];
    float bo[2];
    float pc[2];
    for (int i = 0; i < 2; ++i)
    {
        DmeAxis const rx = dmeAxis(b, static_cast<float>(x + i), b.L, b.R, b.shadowDx);
        pc[i] = rx.picture * ry.picture;
        bo[i] = rx.border * ry.border * b.borderOpacity;
        sh[i] = b.shadowOpacity * rx.shadow * ry.shadow;
    }
    int const lx = x - b.bx;
    int const ly = row - b.by;
    float fy[2] = {0.f, 0.f};
    float fk[2] = {1023.f, 1023.f};
    float fcb = 0.f;
    float fcr = 0.f;
    if (b.bw > 0 && lx >= 0 && lx < b.bw && ly >= 0 && ly < b.bh)
    {
        std::int16_t const* line = d.buffer + static_cast<std::size_t>(ly) * static_cast<std::size_t>(b.cols);
        fy[0] = line[lx];
        fy[1] = line[lx + 1];
        fcb = line[b.bw + lx / 2];
        fcr = line[b.bw + b.bw / 2 + lx / 2];
        if (b.kind == DmeKind::Linear)
        {
            fk[0] = line[2 * b.bw + lx];
            fk[1] = line[2 * b.bw + lx + 1];
        }
    }
    else
    {
        pc[0] = 0.f;
        pc[1] = 0.f;
    }
    float as[2];
    float ab[2];
    float ap[2];
    float gp[2];
    for (int i = 0; i < 2; ++i)
    {
        as[i] = sh[i] * d.gain;
        ab[i] = bo[i] * d.gain;
        gp[i] = pc[i] * d.gain;
        ap[i] = b.kind == DmeKind::Luma     ? pc[i] * lumaAlpha(static_cast<unsigned>(fy[i]), d.clip, d.soft, d.invert, d.gain)
                : b.kind == DmeKind::Linear ? clamp01(fk[i] * (1.f / 1023.f)) * gp[i]
                                            : gp[i];
        std::uint16_t v = mix10(y[i], 64, as[i]);
        v = mix10(v, b.borderY, ab[i]);
        if (b.kind == DmeKind::Linear)
        {
            float const fv = static_cast<float>(v);
            v = clamp10(fv + (64.f - fv) * ap[i] + fy[i] * gp[i]);
        }
        else
        {
            v = mix10(v, static_cast<std::uint16_t>(fy[i]), ap[i]);
        }
        y[i] = v;
    }
    float const cs = 0.5f * (as[0] + as[1]);
    float const cbo = 0.5f * (ab[0] + ab[1]);
    float const cp = 0.5f * (ap[0] + ap[1]);
    float const cg = 0.5f * (gp[0] + gp[1]);
    std::uint16_t c[2] = {cb, cr};
    float const f[2] = {fcb, fcr};
    std::uint16_t const border[2] = {b.borderCb, b.borderCr};
    for (int i = 0; i < 2; ++i)
    {
        std::uint16_t v = mix10(c[i], 512, cs);
        v = mix10(v, border[i], cbo);
        if (b.kind == DmeKind::Linear)
        {
            float const fv = static_cast<float>(v);
            v = clamp10(fv + (512.f - fv) * cp + f[i] * cg);
        }
        else
        {
            v = mix10(v, static_cast<std::uint16_t>(f[i]), cp);
        }
        c[i] = v;
    }
    cb = c[0];
    cr = c[1];
}

// The box of `set` on a W × H raster for a source of the same raster.
DmeBox makeDmeBox(DmeSettings const& set, DmeKind kind, int width, int height);

// CPU reference: the weight table of a box, its resample (the box buffer, b.bh lines of b.cols)
// and its layer over a picture (in place). The GPU self-test and the unit tests compare with it.
void dmeTableCpu(DmeBox const& b, std::vector<int>& start, std::vector<float>& w);
void dmeResampleCpu(DmeBox const& b, DmeSource const& src, std::vector<std::int16_t>& buffer);
void dmeLayerCpu(DmeDraw const& d, Planar422& picture);
} // namespace fxeng
