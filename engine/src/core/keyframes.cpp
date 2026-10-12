#include "core/keyframes.hpp"

#include <algorithm>
#include <cmath>

namespace fxeng
{
namespace
{
constexpr double kLn2 = 0.69314718055994530942;

// ln x (x > 0) and e^x with only + − × ÷ and exact binary scaling (frexp, ldexp, floor): no libm
// approximation that could differ between machines or library versions. Both are far more
// precise than the float result needs.
double lnPositive(double x)
{
    int e = 0;
    double const m = std::frexp(x, &e);
    // ln m = 2 atanh(s) = 2 (s + s³/3 + s⁵/5 + …), s = (m − 1) / (m + 1), |s| ≤ 1/3.
    double const s = (m - 1.0) / (m + 1.0);
    double const s2 = s * s;
    double term = s;
    double sum = 0.0;
    for (int k = 1; k < 64; k += 2)
    {
        sum += term / static_cast<double>(k);
        term *= s2;
    }
    return 2.0 * sum + static_cast<double>(e) * kLn2;
}

double expOf(double x)
{
    double const k = std::floor(x / kLn2 + 0.5);
    double const r = x - k * kLn2;
    double term = 1.0;
    double sum = 1.0;
    for (int i = 1; i < 30; ++i)
    {
        term *= r / static_cast<double>(i);
        sum += term;
    }
    return std::ldexp(sum, static_cast<int>(k));
}

float lerp(float a, float b, float w)
{
    // Exact at both ends: a · 1 + b · 0 and a · 0 + b.
    return a * (1.f - w) + b * w;
}
} // namespace

std::optional<Ease> parseEase(std::string const& text)
{
    for (auto ease : {Ease::Linear, Ease::Ease, Ease::EaseIn, Ease::EaseOut, Ease::Hold})
    {
        if (text == easeName(ease))
        {
            return ease;
        }
    }
    return std::nullopt;
}

std::string easeName(Ease ease)
{
    switch (ease)
    {
    case Ease::Linear:
        return "linear";
    case Ease::Ease:
        return "ease";
    case Ease::EaseIn:
        return "ease-in";
    case Ease::EaseOut:
        return "ease-out";
    case Ease::Hold:
        break;
    }
    return "hold";
}

float easeAt(Ease ease, float t)
{
    t = t < 0.f ? 0.f : (t > 1.f ? 1.f : t);
    switch (ease)
    {
    case Ease::Linear:
        return t;
    case Ease::Ease:
        return t * t * (3.f - 2.f * t);
    case Ease::EaseIn:
        return t * t;
    case Ease::EaseOut:
        return t * (2.f - t);
    case Ease::Hold:
        break;
    }
    return t < 1.f ? 0.f : 1.f;
}

bool dmeSameState(DmeSettings const& a, DmeSettings const& b)
{
    return a.x == b.x && a.y == b.y && a.size == b.size && a.cropLeft == b.cropLeft && a.cropRight == b.cropRight && a.cropTop == b.cropTop &&
           a.cropBottom == b.cropBottom && a.soft == b.soft && a.borderWidth == b.borderWidth && a.borderSoft == b.borderSoft &&
           a.borderOpacity == b.borderOpacity && a.shadowDx == b.shadowDx && a.shadowDy == b.shadowDy && a.shadowBlur == b.shadowBlur &&
           a.shadowOpacity == b.shadowOpacity;
}

DmeSettings dmeWithState(DmeSettings box, DmeSettings const& state)
{
    return dmeBetween(box, state, state, 0.f);
}

float dmeLogSize(float a, float b, float w)
{
    if (w <= 0.f || a == b)
    {
        return a;
    }
    if (w >= 1.f)
    {
        return b;
    }
    if (a < kDmeLogSizeFloor && b < kDmeLogSizeFloor)
    {
        return lerp(a, b, w);
    }
    double const la = lnPositive(std::max(a, kDmeLogSizeFloor));
    double const lb = lnPositive(std::max(b, kDmeLogSizeFloor));
    return static_cast<float>(expOf(la + (lb - la) * static_cast<double>(w)));
}

DmeSettings dmeBetween(DmeSettings const& box, DmeSettings const& a, DmeSettings const& b, float w)
{
    DmeSettings out = box;
    out.x = lerp(a.x, b.x, w);
    out.y = lerp(a.y, b.y, w);
    out.size = dmeLogSize(a.size, b.size, w);
    out.cropLeft = lerp(a.cropLeft, b.cropLeft, w);
    out.cropRight = lerp(a.cropRight, b.cropRight, w);
    out.cropTop = lerp(a.cropTop, b.cropTop, w);
    out.cropBottom = lerp(a.cropBottom, b.cropBottom, w);
    out.soft = lerp(a.soft, b.soft, w);
    out.borderWidth = lerp(a.borderWidth, b.borderWidth, w);
    out.borderSoft = lerp(a.borderSoft, b.borderSoft, w);
    out.borderOpacity = lerp(a.borderOpacity, b.borderOpacity, w);
    out.shadowDx = lerp(a.shadowDx, b.shadowDx, w);
    out.shadowDy = lerp(a.shadowDy, b.shadowDy, w);
    out.shadowBlur = lerp(a.shadowBlur, b.shadowBlur, w);
    out.shadowOpacity = lerp(a.shadowOpacity, b.shadowOpacity, w);
    return out;
}

float dmeKeyTime(std::vector<DmeKeyframe> const& keyframes, int j)
{
    float t = 0.f;
    for (int i = 1; i <= j && i < static_cast<int>(keyframes.size()); ++i)
    {
        t += static_cast<float>(keyframes[static_cast<std::size_t>(i)].frames);
    }
    return t;
}

DmeSettings dmePathAt(std::vector<DmeKeyframe> const& keyframes, float u, DmeSettings const& box)
{
    if (keyframes.empty())
    {
        return box;
    }
    if (keyframes.size() == 1 || u <= 0.f)
    {
        return dmeWithState(box, keyframes.front().box);
    }
    float t0 = 0.f;
    for (std::size_t j = 1; j < keyframes.size(); ++j)
    {
        auto const& key = keyframes[j];
        float const t1 = t0 + static_cast<float>(key.frames);
        if (u <= t1)
        {
            return dmeBetween(box, keyframes[j - 1].box, key.box, easeAt(key.ease, (u - t0) / static_cast<float>(key.frames)));
        }
        t0 = t1;
    }
    return dmeWithState(box, keyframes.back().box);
}

int dmeKeyframeOf(std::vector<DmeKeyframe> const& keyframes, DmeSettings const& box)
{
    for (std::size_t j = 0; j < keyframes.size(); ++j)
    {
        if (dmeSameState(keyframes[j].box, box))
        {
            return static_cast<int>(j);
        }
    }
    return -1;
}

std::optional<DmeLoop> parseLoop(std::string const& text)
{
    for (auto loop : {DmeLoop::Off, DmeLoop::Wrap, DmeLoop::PingPong})
    {
        if (text == loopName(loop))
        {
            return loop;
        }
    }
    return std::nullopt;
}

std::string loopName(DmeLoop loop)
{
    switch (loop)
    {
    case DmeLoop::Off:
        return "off";
    case DmeLoop::Wrap:
        return "wrap";
    case DmeLoop::PingPong:
        break;
    }
    return "pingpong";
}

DmeRun dmeRunFrom(std::vector<DmeKeyframe> const& keyframes, DmeSettings const& box, bool onPath, float u, int to, DmeLoop loop, bool reverse)
{
    DmeRun run;
    run.loop = loop;
    run.reverse = reverse;
    int const last = static_cast<int>(keyframes.size()) - 1;
    int const j = loop == DmeLoop::Off ? std::clamp(to, 0, std::max(last, 0)) : (reverse ? 0 : last);
    float const tj = dmeKeyTime(keyframes, j);
    run.u1 = tj;
    if (onPath)
    {
        run.u0 = u;
        return run;
    }
    run.from = box;
    run.leadTo = j;
    run.leadFrames = j >= 0 && j <= last ? keyframes[static_cast<std::size_t>(j)].frames : 0;
    run.leadEase = j >= 0 && j <= last ? keyframes[static_cast<std::size_t>(j)].ease : Ease::Linear;
    run.u0 = tj;
    return run;
}

DmeRunState dmeRunAt(std::vector<DmeKeyframe> const& keyframes, DmeRun const& run, std::uint64_t f, DmeSettings const& box)
{
    DmeRunState s;
    int const n = static_cast<int>(keyframes.size());
    if (n == 0)
    {
        s.box = box;
        s.done = true;
        return s;
    }
    auto const lead = static_cast<std::uint64_t>(std::max(run.leadFrames, 0));
    if (lead > 0 && f <= lead)
    {
        float const t = static_cast<float>(f) / static_cast<float>(lead);
        s.box = dmeBetween(box, run.from, keyframes[static_cast<std::size_t>(run.leadTo)].box, easeAt(run.leadEase, t));
        s.keyframe = run.leadTo;
        s.onPath = f == lead;
        s.u = run.u0;
        s.done = s.onPath && run.loop == DmeLoop::Off && run.u0 == run.u1;
        return s;
    }
    double const g = static_cast<double>(f - lead);
    double const T = static_cast<double>(dmeKeyTime(keyframes, n - 1));
    double const u0 = static_cast<double>(run.u0);
    double u = u0;
    bool up = true;
    switch (run.loop)
    {
    case DmeLoop::Off:
    {
        double const u1 = static_cast<double>(run.u1);
        up = u1 >= u0;
        if (g >= std::abs(u1 - u0))
        {
            u = u1;
            s.done = true;
        }
        else
        {
            u = up ? u0 + g : u0 - g;
        }
        break;
    }
    case DmeLoop::Wrap:
        // Up to T, then the next grain shows 0 (a cycle of T + 1 grains); mirrored with reverse.
        up = !run.reverse;
        if (T > 0.0)
        {
            double const p = std::min(std::fmod(up ? u0 + g : T - u0 + g, T + 1.0), T);
            u = up ? p : T - p;
        }
        break;
    case DmeLoop::PingPong:
        // A triangle wave of period 2T; reverse starts on its way down.
        if (T > 0.0)
        {
            double const p = std::fmod(run.reverse ? 2.0 * T - u0 + g : u0 + g, 2.0 * T);
            up = p <= T;
            u = up ? p : 2.0 * T - p;
        }
        break;
    }
    s.onPath = true;
    s.u = static_cast<float>(u);
    s.box = dmePathAt(keyframes, s.u, box);
    // The keyframe it moves to (or has reached).
    s.keyframe = up ? n - 1 : 0;
    for (int j = 0; j < n; ++j)
    {
        float const tj = dmeKeyTime(keyframes, j);
        if (up && tj >= s.u)
        {
            s.keyframe = j;
            break;
        }
        if (!up && tj <= s.u)
        {
            s.keyframe = j;
        }
    }
    return s;
}
} // namespace fxeng
