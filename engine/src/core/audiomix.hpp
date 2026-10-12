// P5 audio (docs/engine-p5.md §3–§6): per ME a PGM mix of strips (live inputs, clip players,
// re-entered MEs, the stinger), each with a fader, a mute and audio follow video (AFV), a master
// fader per ME and Fade to Black; a delay line and a channel map per live input. Every change
// ramps over one grain, a Cut crossfades over its grain. CPU only: fx-audio runs it, the unit
// tests check it sample by sample. Built without FMA contraction, as all of fxeng-core.
#pragma once

#include "core/audio.hpp"
#include "core/mixer.hpp"

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace fxeng
{
// A fader runs from −90 dB (off: −∞) to +10 dB.
constexpr float kFaderOffDb = -90.f;
constexpr float kFaderMaxDb = 10.f;
// Live input delays: at most this (FLOWXER_ENGINE_AUDIO_MAX_DELAY_MS ≤ 5000).
constexpr int kMaxAudioDelayMs = 5000;

// −90 dB and below is 0 (off), 0 dB exactly 1.
float dbToGain(float db);
// For the status: 0 (and anything at or below −90 dB) is −90.
float gainToDb(float gain);

// The S-shaped step of a change over one grain: w(s) = 0.5 − 0.5 · cos(π · (s + 1) / n), 0 … 1 with
// no slope at either end (a linear ramp's corners are audible above 5 kHz on a full-range change).
float smoothStep(std::size_t s, std::size_t n);
// One grain of a linear ramp: g0 + (g1 − g0) · (s + 1) / n at sample s (P4's Fade to Black).
void linearRamp(float g0, float g1, std::size_t n, float* out);
// One grain of a level change (fader, mute, AFV, master): g0 + (g1 − g0) · w(s), exactly g1 at the end.
void smoothRamp(float g0, float g1, std::size_t n, float* out);
// A transition's equal-power curve (P4): t moves linearly from tPrev to t within the grain, a = cos
// and b = sin of t · 90° (crossfade()).
void transitionCurve(float tPrev, float t, std::size_t n, float* a, float* b);
// An AFV weight from w0 to w1 on the equal-power curve over one grain (a Cut): down as
// w1 + (w0 − w1) · cos(u · 90°), up as w0 + (w1 − w0) · sin(u · 90°), u = w(s) (smoothStep); exactly
// w1 at the end.
void cutCurve(float w0, float w1, std::size_t n, float* out);
// A crossfade from `from` to `to` over n samples (a delay or channel map change):
// from + (to − from) · w(s), exactly `to` at the end.
void crossfadeSmooth(float const* from, float const* to, std::size_t n, float* out);

// A strip of an ME's mix: live input k, clip player k, the re-entered Program of ME k, or the stinger.
struct StripRef
{
    enum class Kind
    {
        Input,
        Player,
        Me,
        Stinger
    };
    Kind kind = Kind::Input;
    // 1-based; 0 for the stinger.
    int index = 1;

    bool operator==(StripRef const& other) const
    {
        return kind == other.kind && index == other.index;
    }
};

// "in3", "cp1", "me2", "stinger"; nothing for anything else.
std::optional<StripRef> parseStrip(std::string const& text);
std::string stripName(StripRef strip);

struct StripSettings
{
    float faderDb = 0.f;
    bool mute = false;
    // On: heard only while its source is on the ME's Program (and along a transition's curve).
    // Off: always in the mix at its fader level. The stinger strip has no AFV.
    bool afv = true;
};

// The samples a live input delayed by `delay` samples gives output grain `index` (latency L):
// input grain i − L's span on the 48 kHz index, `delay` samples earlier (docs/engine-p5.md §6).
SampleRange delayedSamples(Rate rate, std::uint64_t index, int latency, std::uint64_t delay);

// A live input's channel map, resolved: per PGM channel the input channel (1-based), 0 silent.
// `map` empty is the default: 1:1, and a 1-channel flow on every channel. Entries beyond the flow's
// channels are silent.
std::vector<int> channelMap(std::vector<int> const& map, std::size_t pgmChannels, std::size_t inputChannels);

// A live input's delay line: a ring of every channel of its flow, indexed by the 48 kHz sample
// index (MXL's, on TAI). It holds [start, end); reads outside that are silence.
class DelayLine
{
public:
    void reset(std::size_t channels, std::size_t length);
    [[nodiscard]] std::size_t channels() const
    {
        return channels_;
    }
    [[nodiscard]] std::size_t length() const
    {
        return length_;
    }
    // Writing continues at `from`: a gap (or the first write) drops what it held.
    void begin(std::uint64_t from);
    // `count` samples of `channel` from sample index `index` (between begin and commit).
    void put(std::size_t channel, std::uint64_t index, float const* data, std::size_t count);
    // It holds up to `end` now (the oldest beyond its length are gone).
    void commit(std::uint64_t end);
    [[nodiscard]] bool holds(std::uint64_t first, std::uint64_t end) const;
    [[nodiscard]] bool started() const
    {
        return started_;
    }
    [[nodiscard]] std::uint64_t start() const
    {
        return start_;
    }
    [[nodiscard]] std::uint64_t end() const
    {
        return end_;
    }
    // Samples [first, first + count) of a channel; what the line does not hold is silence.
    void read(std::size_t channel, std::uint64_t first, std::size_t count, float* out) const;
    // The input's sound for an output: per PGM channel c, input channel map[c] (resolved by
    // channelMap) of [first, first + n), channel-major into out (map.size() × n).
    void readMapped(std::vector<int> const& map, std::uint64_t first, std::size_t n, float* out) const;

private:
    std::size_t channels_ = 0;
    std::size_t length_ = 0;
    std::vector<float> data_;
    std::uint64_t start_ = 0;
    std::uint64_t end_ = 0;
    bool started_ = false;
};

// One grain of sound for the mix: per source a channel-major buffer (channels × n); nullptr is
// silence.
struct MixSources
{
    std::vector<float const*> inputs;
    std::vector<float const*> players;
    // Per stinger player, the sound of its frame in this grain.
    std::vector<float const*> stingers;
};

// A strip after a grain: its gain in PGM at the end of the grain, and whether it is heard (that gain
// above −90 dB; the stinger strip only while its stinger runs).
struct StripState
{
    float gain = 0.f;
    bool heard = false;
};

class AudioMix
{
public:
    AudioMix(int mes, int inputs, int players, std::size_t channels);

    enum class Result
    {
        Ok,
        BadMe,
        NoStrip,
        BadValue,
        AfvOnStinger
    };

    // Control thread; each change ramps over the next grain the audio thread mixes.
    Result setMaster(int me, float db);
    // A strip of ME m: live inputs, clip players, the Programs of MEs n > m, the stinger.
    Result setStrip(int me, StripRef strip, StripSettings const& settings);
    [[nodiscard]] bool hasStrip(int me, StripRef strip) const;
    [[nodiscard]] float master(int me) const;
    [[nodiscard]] std::optional<StripSettings> strip(int me, StripRef strip) const;
    // ME m's strips in slot order: inputs, clip players, re-entered MEs, the stinger.
    [[nodiscard]] std::vector<StripRef> strips(int me) const;

    // Audio thread: one grain of every ME, the plans in render order (ME n before ME m, so a
    // re-entered Program is the same grain). out[m − 1] becomes ME m's PGM (channels × n).
    void grain(std::vector<MePlan> const& plans, MixSources const& sources, std::size_t n, std::vector<std::vector<float>>& out);
    // ME m's strips (strips(m) order) after the last grain; any thread.
    [[nodiscard]] std::vector<StripState> stripStates(int me) const;
    // Audio thread: the live inputs some ME may hear in the grain of these plans (worth a bounded
    // wait for their samples): on Program or in a transition with AFV on, AFV off at a level, or
    // heard in the last grain (still ramping out).
    [[nodiscard]] std::vector<bool> heardInputs(std::vector<MePlan> const& plans) const;

    [[nodiscard]] std::size_t channels() const
    {
        return channels_;
    }
    [[nodiscard]] int mes() const
    {
        return mes_;
    }

private:
    struct MeSettings
    {
        float masterDb = 0.f;
        std::vector<StripSettings> strips;
    };
    // Where each strip ended the last grain: its level (fader and mute), its AFV blend (1 on,
    // 0 off) and its AFV weight; the master's level.
    struct MeRun
    {
        // Set by the first grain, which starts where its settings are (no ramp).
        bool primed = false;
        float master = 1.f;
        std::vector<float> level;
        std::vector<float> afv;
        std::vector<float> weight;
        std::vector<StripState> states;
    };

    // The strip's slot on ME m (0-based), or −1 when ME m has no such strip.
    [[nodiscard]] int slot(int me, StripRef strip) const;
    // The strip of a source on ME m (Black: −1).
    [[nodiscard]] int sourceSlot(int me, Source source) const;
    [[nodiscard]] StripRef refOf(int slot) const;

    int mes_;
    int inputs_;
    int players_;
    std::size_t channels_;
    std::size_t slots_;
    mutable std::mutex mu_;
    std::vector<MeSettings> settings_;
    // Audio thread only.
    std::vector<MeRun> run_;
    std::vector<float> zeros_;
    std::vector<float> curveA_;
    std::vector<float> curveB_;
    std::vector<float> ramp_;
    std::vector<float> blend_;
    std::vector<float> weight_;
    std::vector<float> gain_;
    mutable std::mutex stateMu_;
    std::vector<std::vector<StripState>> published_;
};
} // namespace fxeng
