// DME keyframes (docs/engine-p4.md §5): up to 8 box states per keyer, each reached over its own
// `frames` with an ease, and runs along them (to a keyframe, back, looping). Evaluated on the CPU
// for every output grain from the grain index (core/mixer); the GPU only sees the box of its
// grain. Only + − × ÷, floor and exact binary scaling (no libm transcendentals, no FMA
// contraction): every machine computes the same box for the same grain, so the CPU reference of
// the tests equals the engine bit for bit.
#pragma once

#include "core/dme.hpp"

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace fxeng
{
// How a keyframe is reached from the one before: linear; ease (smoothstep in and out), ease-in,
// ease-out (quadratic); hold (stays, then jumps at the end).
enum class Ease
{
    Linear,
    Ease,
    EaseIn,
    EaseOut,
    Hold
};

std::optional<Ease> parseEase(std::string const& text);
std::string easeName(Ease ease);
// The eased fraction at time fraction t of a segment (0 → 0, 1 → 1 exactly).
float easeAt(Ease ease, float t);

// A keyframe: the box state (the animated fields of its box), and for keyframe j the time from
// keyframe j − 1 (`frames` grains, 1..1000) with its ease. Keyframe 0's frames and ease are used
// when a run reaches it from a box that is not on the keyframes' path.
struct DmeKeyframe
{
    DmeSettings box;
    int frames = 25;
    Ease ease = Ease::Linear;
};

constexpr int kMaxKeyframes = 8;
// Sizes below this interpolate in log space as if they were this (a box of 19 px at 1080p): a fly
// from size 0 starts as a dot instead of waiting at 0.
constexpr float kDmeLogSizeFloor = 0.01f;

// The animated fields: x, y, size, crop, soft, border width, soft and opacity, shadow. Not
// animated: enabled and the border colour.
bool dmeSameState(DmeSettings const& a, DmeSettings const& b);
// `box` with the animated fields of `state`.
DmeSettings dmeWithState(DmeSettings box, DmeSettings const& state);
// The state between a (w = 0, exactly a) and b (w = 1, exactly b) over `box`: linear, size in log
// space.
DmeSettings dmeBetween(DmeSettings const& box, DmeSettings const& a, DmeSettings const& b, float w);
// Size in log space (exact at both ends).
float dmeLogSize(float a, float b, float w);

// The keyframes' timeline: keyframe 0 at 0, keyframe j at t(j − 1) + frames(j) grains.
float dmeKeyTime(std::vector<DmeKeyframe> const& keyframes, int j);
// The box on the path at timeline position u (clamped to the timeline), over `box`.
DmeSettings dmePathAt(std::vector<DmeKeyframe> const& keyframes, float u, DmeSettings const& box);
// The keyframe the box is at (exactly its state), or −1.
int dmeKeyframeOf(std::vector<DmeKeyframe> const& keyframes, DmeSettings const& box);

enum class DmeLoop
{
    Off,
    Wrap,
    PingPong
};

std::optional<DmeLoop> parseLoop(std::string const& text);
std::string loopName(DmeLoop loop);

// A run: a lead-in from a box off the path straight to keyframe `leadTo` (over that keyframe's
// frames and ease; leadFrames 0: none), then along the path at one timeline grain per grain from
// u0: to u1 (loop off) or looping. Wrap: up to the last keyframe, then the next grain jumps to
// keyframe 0 (down to 0 and back to the last with reverse). Pingpong: up and down (down first
// with reverse).
struct DmeRun
{
    DmeSettings from;
    int leadFrames = 0;
    int leadTo = 0;
    Ease leadEase = Ease::Linear;
    float u0 = 0.f;
    float u1 = 0.f;
    DmeLoop loop = DmeLoop::Off;
    bool reverse = false;
};

// A run's box at one step: on the path at u (after the lead-in), the keyframe it moves to (or
// reached), and done at the last step of a run that ends (loop off).
struct DmeRunState
{
    DmeSettings box;
    bool onPath = false;
    float u = 0.f;
    int keyframe = 0;
    bool done = false;
};

// The run from a box (on the path at u when onPath, else off it) to keyframe `to` (loop off), or a
// loop that first goes to the last keyframe (keyframe 0 with reverse). Needs a keyframe `to`
// (2 keyframes for a loop).
DmeRun dmeRunFrom(std::vector<DmeKeyframe> const& keyframes, DmeSettings const& box, bool onPath, float u, int to, DmeLoop loop, bool reverse);
// The run's state at step f (1: its first grain; a run started at grain s shows step f in grain
// s + f − 1), over `box`.
DmeRunState dmeRunAt(std::vector<DmeKeyframe> const& keyframes, DmeRun const& run, std::uint64_t f, DmeSettings const& box);
} // namespace fxeng
