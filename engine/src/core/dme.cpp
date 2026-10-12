#include "core/dme.hpp"

#include "core/v210.hpp"

#include <algorithm>
#include <cmath>

namespace fxeng
{
DmeBox makeDmeBox(DmeSettings const& set, DmeKind kind, int width, int height)
{
    DmeBox b;
    b.kind = kind;
    b.width = width;
    b.height = height;
    float const w = static_cast<float>(width);
    float const h = static_cast<float>(height);
    b.s = set.size;
    // The crop on whole samples: columns on even luma samples (whole chroma samples).
    b.cl = std::min(2 * static_cast<int>(std::floor(set.cropLeft * w * 0.5f + 0.5f)), width - 2);
    b.cr = std::max(width - 2 * static_cast<int>(std::floor(set.cropRight * w * 0.5f + 0.5f)), b.cl + 2);
    b.ct = std::min(static_cast<int>(std::floor(set.cropTop * h + 0.5f)), height - 1);
    b.cb = std::max(height - static_cast<int>(std::floor(set.cropBottom * h + 0.5f)), b.ct + 1);
    b.dx = set.x * w - 0.5f * w * b.s;
    b.dy = set.y * h - 0.5f * h * b.s;
    b.L = b.dx + static_cast<float>(b.cl) * b.s;
    b.R = b.dx + static_cast<float>(b.cr) * b.s;
    b.T = b.dy + static_cast<float>(b.ct) * b.s;
    b.B = b.dy + static_cast<float>(b.cb) * b.s;
    b.fs = std::max(std::min(b.s, 1.f), kDmeMinFilterScale);
    b.sup = 2.f / b.fs;
    int const need = static_cast<int>(std::ceil(2.f * b.sup));
    b.taps = need <= 4 ? 4 : need <= 8 ? 8 : need <= 12 ? 12 : need <= 16 ? 16 : need <= 24 ? 24 : 32;
    if (b.s > 0.f && b.R > 0.f && b.L < w && b.B > 0.f && b.T < h)
    {
        int x0 = static_cast<int>(std::floor(b.L));
        x0 -= x0 & 1;
        int x1 = static_cast<int>(std::ceil(b.R));
        x1 += x1 & 1;
        x0 = std::max(x0, 0);
        x1 = std::min(x1, width);
        int const y0 = std::max(static_cast<int>(std::floor(b.T)), 0);
        int const y1 = std::min(static_cast<int>(std::ceil(b.B)), height);
        if (x1 > x0 && y1 > y0)
        {
            b.bx = x0;
            b.bw = x1 - x0;
            b.by = y0;
            b.bh = y1 - y0;
        }
    }
    b.cols = b.bw == 0 ? 0 : (kind == DmeKind::Linear ? 3 * b.bw : 2 * b.bw);
    if (b.bh > 0)
    {
        float const c0 = dmeRowIndex(b, 0);
        float const c1 = dmeRowIndex(b, b.bh - 1);
        int const first = dmeFirstTap(c0, b.sup);
        int const last = std::min(dmeLastTap(c1, b.sup), dmeFirstTap(c1, b.sup) + b.taps - 1);
        b.r0 = dmeClampI(first, b.ct, b.cb - 1);
        b.rows = dmeClampI(last, b.ct, b.cb - 1) - b.r0 + 1;
    }
    b.soft = set.soft;
    b.borderWidth = set.borderWidth;
    b.borderSoft = set.borderSoft;
    b.borderOpacity = set.borderOpacity;
    b.borderY = set.borderY;
    b.borderCb = set.borderCb;
    b.borderCr = set.borderCr;
    b.shadowDx = set.shadowDx;
    b.shadowDy = set.shadowDy;
    b.shadowBlur = set.shadowBlur;
    b.shadowOpacity = set.shadowOpacity;
    b.invSoft = b.soft > 0.f ? 1.f / b.soft : 0.f;
    b.invBorderSoft = b.borderSoft > 0.f ? 1.f / b.borderSoft : 0.f;
    b.invBlur = 1.f / (b.shadowBlur > 0.f ? b.shadowBlur : 1.f);
    // Outside this area every α is 0: the compose skips it.
    float pad = b.borderWidth + b.borderSoft + b.soft + 2.f;
    if (b.shadowOpacity > 0.f)
    {
        pad += std::fabs(b.shadowDx) + std::fabs(b.shadowDy) + b.shadowBlur;
    }
    auto const clampF = [](float v, int hi) { return static_cast<int>(std::min(std::max(v, 0.f), static_cast<float>(hi))); };
    b.ex0 = clampF(std::floor(b.L - pad), width);
    b.ex1 = clampF(std::ceil(b.R + pad), width);
    b.ey0 = clampF(std::floor(b.T - pad), height);
    b.ey1 = clampF(std::ceil(b.B + pad), height);
    return b;
}

void dmeTableCpu(DmeBox const& b, std::vector<int>& start, std::vector<float>& w)
{
    int const entries = dmeEntries(b);
    start.assign(static_cast<std::size_t>(entries), 0);
    w.assign(static_cast<std::size_t>(entries) * static_cast<std::size_t>(b.taps), 0.f);
    float taps[kDmeMaxTaps];
    for (int e = 0; e < entries; ++e)
    {
        start[static_cast<std::size_t>(e)] = dmeWeights(dmeEntryIndex(b, e), b.fs, b.sup, b.taps, taps);
        for (int i = 0; i < b.taps; ++i)
        {
            w[static_cast<std::size_t>(i) * static_cast<std::size_t>(entries) + static_cast<std::size_t>(e)] = taps[i];
        }
    }
}

void dmeResampleCpu(DmeBox const& b, DmeSource const& src, std::vector<std::int16_t>& buffer)
{
    buffer.assign(static_cast<std::size_t>(b.bh) * static_cast<std::size_t>(b.cols), 0);
    if (b.bw == 0)
    {
        return;
    }
    std::vector<int> start;
    std::vector<float> weights;
    dmeTableCpu(b, start, weights);
    DmeTable const table{start.data(), weights.data(), dmeEntries(b)};
    std::vector<std::int16_t> mid(static_cast<std::size_t>(b.rows) * static_cast<std::size_t>(b.cols));
    dmeDispatch(b.taps, [&](auto n) {
        constexpr int N = decltype(n)::value;
        for (int e = 0; e < b.cols; ++e)
        {
            int entry = 0;
            int lo = 0;
            int hi = 0;
            int const plane = dmePlaneOf(b, e, entry, lo, hi);
            float w[N];
            int j[N];
            dmeTaps<N>(table, entry, lo, hi, w, j);
            for (int r = 0; r < b.rows; ++r)
            {
                mid[static_cast<std::size_t>(r) * static_cast<std::size_t>(b.cols) + static_cast<std::size_t>(e)] = dmeHSample<N>(src, plane, b.r0 + r, w, j);
            }
        }
        for (int oy = 0; oy < b.bh; ++oy)
        {
            for (int e = 0; e < b.cols; ++e)
            {
                buffer[static_cast<std::size_t>(oy) * static_cast<std::size_t>(b.cols) + static_cast<std::size_t>(e)] = dmeVSample<N>(b, table, mid.data(), e, oy);
            }
        }
    });
}

void dmeLayerCpu(DmeDraw const& d, Planar422& picture)
{
    DmeBox const& b = d.box;
    int const cw = picture.width / 2;
    for (int row = b.ey0; row < b.ey1; ++row)
    {
        DmeAxis const ry = dmeRowAxis(b, row);
        for (int x = b.ex0 & ~1; x < b.ex1; x += 2)
        {
            std::size_t const yi = static_cast<std::size_t>(row) * static_cast<std::size_t>(picture.width) + static_cast<std::size_t>(x);
            std::size_t const ci = static_cast<std::size_t>(row) * static_cast<std::size_t>(cw) + static_cast<std::size_t>(x / 2);
            dmePair(d, ry, x, row, &picture.y[yi], picture.cb[ci], picture.cr[ci]);
        }
    }
}
} // namespace fxeng
