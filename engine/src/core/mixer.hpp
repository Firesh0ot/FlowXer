// ME state: Program/Preview buses, Cut, Mix, Wipe and Stinger, re-entry, the keyers (upstream and
// downstream), Fade to Black. No GPU, no MXL: the control thread changes it, the render thread
// takes one plan per output grain.
#pragma once

#include "core/dme.hpp"
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

// An ME's transition type.
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

// A stinger of a take: stinger player `player` (1-based) with its loaded stinger's frames and cut
// frame (the background switches there).
struct Sting
{
    int player = 0;
    int frames = 0;
    int cut = 0;
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
    // A stinger from a to b: stinger player `stinger` (0: none) shows its frame `stingerFrame`
    // over the upstream keys; the background is b from the cut point (`cutDone`). The background
    // sound crossfades from a to b by tPrev..t (4 grains around the cut point).
    int stinger = 0;
    int stingerFrame = 0;
    bool cutDone = false;
    Source preview;
    // Fade to Black over the ME's whole output (after the DSKs): its gain now and at the grain
    // before (the audio ramps between them).
    float ftb = 0.f;
    float ftbPrev = 0.f;

    // The background picture (a, or b after a stinger's cut point; a Mix or Wipe adds b).
    [[nodiscard]] Source background() const
    {
        return stinger > 0 && cutDone ? b : a;
    }
};

// A keyer's settings: a linear key from its key input (v210a fill + key), a luma key of a
// source (the fill is the source itself): α = clamp(((Y − 64) / 876 − clip) / soft), inverted,
// times the gain (pixel.hpp lumaAlpha), or a pip: a source as an opaque picture in a box. A
// linear key's α is key / 1023 · gain. With dme.enabled (always for a pip; upstream keyers only)
// the fill and key are cropped, scaled and placed in a box (core/dme.hpp, docs/engine-p4.md).
struct KeySettings
{
    bool luma = false;
    bool pip = false;
    Source source;
    float clip = 0.f;
    float soft = 0.f;
    float gain = 1.f;
    bool invert = false;
    DmeSettings dme;
};

// Where a keyer keys: over ME `me`, before the transition (upstream) or after it (a DSK).
struct KeyerSpec
{
    int me = 1;
    bool upstream = false;
};

// A keyer for one output grain: keyer `key` (1-based, in the configuration's order) with its fade
// `gain` (0..1, on, off or fading) and settings.
struct KeyPlan
{
    int key = 1;
    int me = 1;
    bool upstream = false;
    float gain = 0.f;
    KeySettings settings;
};

struct FramePlan
{
    // Render order: ME count .. 1.
    std::vector<MePlan> mes;
    std::vector<KeyPlan> keys;
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
    // The stinger player of a running (or waiting) stinger, else 0.
    int stinger = 0;
    TransitionSettings next;
};

struct KeyView
{
    int key = 1;
    int me = 1;
    bool upstream = false;
    // On air (or going on air during an auto).
    bool on = false;
    bool transition = false;
    float gain = 0.f;
    KeySettings settings;
};

enum class MixResult
{
    Ok,
    BadMe,
    BadSource,
    BadFrames,
    BadPattern,
    BadPosition,
    BadKey,
    BadDme,
    Busy
};

class Mixer
{
public:
    Mixer(int mes, int inputs, int players = 0, std::vector<KeyerSpec> keyers = {});

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
    // Program goes to `target`: at the next grain (a cut, or frames 0), by a Mix or the ME's Wipe
    // over `frames` grains, or by the stinger `sting` (a stinger without one is a cut). Preview
    // becomes `preview` when it is given (at the end of the transition). A take during a
    // transition ends that one first (buses as at its end).
    MixResult take(int me, Source target, int frames, std::optional<Source> preview, Transition type = Transition::Mix, Sting sting = {});
    // Program and Preview swap at the next grain; a running transition ends there.
    MixResult cut(int me);
    // The ME's next transition from Program to Preview over `frames` grains (its rate when not
    // given), then the buses swap. From a held T-bar position it runs the rest at that rate.
    MixResult autoTransition(int me, std::optional<int> frames = std::nullopt);
    // The T-bar: t > 0 starts the ME's next transition (Program to Preview) and holds it at t;
    // t = 1 completes it (the buses swap after that grain); t = 0 ends it without the swap. A
    // stinger does not scrub: t > 0 starts it as an Auto. Busy while an Auto runs.
    MixResult position(int me, float t);

    // Keyer k (1-based): on or off at the next grain, or a fade to the other state over `frames`
    // grains (an auto during an auto turns back from where it is).
    MixResult keySet(int key, bool on);
    MixResult keyAuto(int key, int frames);
    // Its mode and settings; a luma key's or a pip's source must be one its ME may take. A pip
    // and a box (dme) only on upstream keyers, with the ranges of dmeValid.
    MixResult keySettings(int key, KeySettings const& settings);
    // Fade to Black of ME m's whole output, keys included: on or off at the next grain, or a fade
    // over `frames` grains (an auto during an auto turns back).
    MixResult ftbSet(int me, bool on);
    MixResult ftbAuto(int me, int frames);
    [[nodiscard]] float ftbGain(int me) const;

    // Render thread: the plans of output grain `index`. A Mix starts at the first grain after the
    // command and its progress follows the grain index, so skipped grains do not stretch it. A
    // Mix ends (buses swapped) after its last grain. A stinger starts at the first grain its
    // player is ready for (`stingerReady[p - 1]`: its first frame decoded), at the latest after
    // kStingerWait grains (then it is a cut); started at grain s it shows its frame f in grain
    // s + f, switches the background at s + cut and ends after its last frame.
    FramePlan frame(std::uint64_t index, std::vector<bool> const& stingerReady = {});

    [[nodiscard]] std::vector<MeView> view() const;
    [[nodiscard]] std::vector<KeyView> keyView() const;
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
    // Grains stingers waited for their first frame, stingers that became cuts after waiting, and
    // stingers that played (their first frame went on air).
    [[nodiscard]] std::uint64_t stingerWaits() const;
    [[nodiscard]] std::uint64_t stingerCuts() const;
    [[nodiscard]] std::uint64_t stingersPlayed() const;

    static constexpr int kMaxFrames = 1000;
    static constexpr int kStingerWait = 50;

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
        // A stinger (player 0: none); `waiting` while its player is not ready (since `start`).
        Sting sting;
        bool waiting = false;
        TransitionSettings next;
    };
    // A keyer's (with its place and settings) or an FTB's fade.
    struct Fade
    {
        int me = 1;
        bool upstream = false;
        KeySettings settings;
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
    // A keyer's or an FTB's fade: on/off, auto, and its gain at grain `index` (true when a fade
    // ended). Lock held.
    static void fadeSet(Fade& fade, bool on);
    static void fadeAuto(Fade& fade, int frames);
    static bool fadeAdvance(Fade& fade, std::uint64_t index);
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
    std::vector<Fade> keys_;
    // One Fade to Black per ME.
    std::vector<Fade> ftbs_;
    std::uint64_t stingerWaits_ = 0;
    std::uint64_t stingerCuts_ = 0;
    std::uint64_t stingersPlayed_ = 0;
    std::function<void()> changed_;
};
} // namespace fxeng
