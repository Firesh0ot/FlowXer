// ME state: Program/Preview buses, Cut, Mix and Wipe, re-entry, the downstream keyers. No GPU, no
// MXL: the control thread changes it, the render thread takes one plan per output grain.
#pragma once

#include "core/wipe.hpp"

#include <cstdint>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace fxeng
{
struct Source
{
    enum class Kind
    {
        Input,
        Player,
        Black,
        Me
    };
    Kind kind = Kind::Black;
    // Live input 1..N, clip player 1..C, or the ME whose Program is re-entered; 0 for Black.
    int index = 0;

    bool operator==(Source const& other) const
    {
        return kind == other.kind && index == other.index;
    }
    bool operator!=(Source const& other) const
    {
        return !(*this == other);
    }
};

// "in1".., "cp1".., "black", "me2".."me4"; nothing for anything else.
std::optional<Source> parseSource(std::string const& text);
std::string sourceName(Source source);

// An ME's transition type. A stinger is a cut until stingers play (P3 M3).
enum class Transition
{
    Cut,
    Mix,
    Wipe,
    Stinger
};

// "cut", "mix", "wipe", "stinger"; nothing for anything else.
std::optional<Transition> parseTransition(std::string const& text);
std::string transitionName(Transition type);

// The ME's next transition: what Auto runs, and the wipe a take with type wipe uses.
struct TransitionSettings
{
    Transition type = Transition::Mix;
    // The rate in grains.
    int frames = 25;
    WipeSettings wipe;
};

// What one ME renders for one output grain.
struct MePlan
{
    int me = 1;
    // Program: a, going to b by t while `mixing` (a Mix, or a Wipe when `wiping`).
    Source a;
    Source b;
    float t = 0.f;
    // t of the grain before (the audio crossfade ramps from tPrev to t within the grain).
    float tPrev = 0.f;
    bool mixing = false;
    bool wiping = false;
    WipeSettings wipe;
    Source preview;
};

// A downstream keyer for one output grain: key input `key` over ME `me` with `gain` (0..1).
struct DskPlan
{
    int key = 1;
    int me = 1;
    float gain = 0.f;
};

struct FramePlan
{
    // Render order: ME count .. 1.
    std::vector<MePlan> mes;
    std::vector<DskPlan> dsks;
};

struct MeView
{
    int me = 1;
    Source program;
    Source preview;
    // A transition runs (Mix or Wipe).
    bool mixing = false;
    bool wiping = false;
    // The source a running transition goes to.
    Source target;
    int frames = 0;
    // Grains of the running transition already rendered (0 while it waits for its first grain).
    int position = 0;
    // A T-bar (position-driven) transition, held at t.
    bool manual = false;
    float t = 0.f;
    TransitionSettings next;
};

struct DskView
{
    int key = 1;
    int me = 1;
    // On air (or going on air during an auto).
    bool on = false;
    bool transition = false;
    float gain = 0.f;
};

enum class MixResult
{
    Ok,
    BadMe,
    BadSource,
    BadFrames,
    BadPattern,
    BadPosition,
    Busy
};

class Mixer
{
public:
    Mixer(int mes, int inputs, int players = 0, std::vector<int> dskMes = {});

    // ME m may take the live inputs, the clip players, Black and the Program of ME n when n > m:
    // the MEs render in descending order, so the re-entered picture is from the same output
    // grain and no loop can exist.
    [[nodiscard]] bool sourceAllowed(int me, Source source) const;
    // ME count .. 1.
    [[nodiscard]] std::vector<int> renderOrder() const;

    // Immediate: ends a running transition on that ME.
    MixResult setProgram(int me, Source source);
    // Immediate; a running transition keeps going (and no longer changes Preview at its end).
    MixResult setPreview(int me, Source source);
    // The ME's next transition (type, rate, wipe). A running transition keeps its own.
    MixResult setTransition(int me, TransitionSettings const& settings);
    // Program goes to `target`: at the next grain (a cut, a stinger, or frames 0), or by a Mix or
    // the ME's Wipe over `frames` grains. Preview becomes `preview` when it is given (at the end
    // of the transition). A take during a transition ends that one first (buses as at its end).
    MixResult take(int me, Source target, int frames, std::optional<Source> preview, Transition type = Transition::Mix);
    // Program and Preview swap at the next grain; a running transition ends there.
    MixResult cut(int me);
    // The ME's next transition from Program to Preview over `frames` grains (its rate when not
    // given), then the buses swap. From a held T-bar position it runs the rest at that rate.
    MixResult autoTransition(int me, std::optional<int> frames = std::nullopt);
    // The T-bar: t > 0 starts the ME's next transition (Program to Preview) and holds it at t;
    // t = 1 completes it (the buses swap after that grain); t = 0 ends it without the swap. A
    // stinger does not scrub: t > 0 starts it as an Auto. Busy while an Auto runs.
    MixResult position(int me, float t);

    // Downstream keyer k (1-based): on or off at the next grain, or a fade to the other state over
    // `frames` grains (an auto during an auto turns back from where it is).
    MixResult dskSet(int key, bool on);
    MixResult dskAuto(int key, int frames);

    // Render thread: the plans of output grain `index`. A Mix starts at the first grain after the
    // command and its progress follows the grain index, so skipped grains do not stretch it. A
    // Mix ends (buses swapped) after its last grain.
    FramePlan frame(std::uint64_t index);

    [[nodiscard]] std::vector<MeView> view() const;
    [[nodiscard]] std::vector<DskView> dskView() const;
    // Called (without the lock held) after every change of a bus or a Mix start/end. Set once
    // before the render thread starts.
    void onChange(std::function<void()> callback)
    {
        changed_ = std::move(callback);
    }
    [[nodiscard]] int mes() const
    {
        return mes_;
    }

    static constexpr int kMaxFrames = 1000;

private:
    struct Me
    {
        Source program;
        Source preview;
        Source target;
        std::optional<Source> after;
        int frames = 0;
        bool pending = false;
        bool mixing = false;
        // The running transition is a wipe, with these settings (taken when it starts).
        bool wiping = false;
        WipeSettings wipe;
        std::uint64_t start = 0;
        int position = 0;
        // An Auto runs from t = from (0, or a held T-bar position) to 1.
        float from = 0.f;
        // A T-bar transition: held at t; tLast is the t of the grain before (the audio ramp).
        bool manual = false;
        float t = 0.f;
        float tLast = 0.f;
        TransitionSettings next;
    };
    struct Dsk
    {
        int me = 1;
        bool on = false;
        float from = 0.f;
        float to = 0.f;
        float gain = 0.f;
        int frames = 0;
        bool pending = false;
        bool running = false;
        std::uint64_t start = 0;
    };

    [[nodiscard]] bool validMe(int me) const
    {
        return me >= 1 && me <= mes_;
    }
    // Ends a pending or running transition as if it had run to its end. Lock held.
    static void finishMix(Me& state);
    void notify() const
    {
        if (changed_)
        {
            changed_();
        }
    }

    int mes_;
    int inputs_;
    int players_;
    mutable std::mutex mu_;
    std::vector<Me> state_;
    std::vector<Dsk> dsks_;
    std::function<void()> changed_;
};
} // namespace fxeng
