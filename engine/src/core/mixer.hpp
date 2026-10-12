// ME state: Program/Preview buses, Cut, Mix, Wipe and Stinger, re-entry, the keyers (upstream and
// downstream), Fade to Black. No GPU, no MXL: the control thread changes it, the render thread
// takes one plan per output grain.
#pragma once

#include "core/dme.hpp"
#include "core/keyframes.hpp"
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
    // Keyer ties (docs/engine-p4.md §5): the ME's upstream keyers (engine keyer numbers) that Cut,
    // Auto and the T-bar turn on or off with the background.
    std::vector<int> ties;
};

// A tied keyer's gain (a fade) or timeline position (a fly) at the transition's t: `from` at 0 and
// `to` at 1 exactly.
inline float tieAt(float from, float to, float t)
{
    return from * (1.f - t) + to * t;
}

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
// With `fly` the keyer's Auto flies its box along its keyframes instead of fading it.
struct KeySettings
{
    bool luma = false;
    bool pip = false;
    Source source;
    float clip = 0.f;
    float soft = 0.f;
    float gain = 1.f;
    bool invert = false;
    bool fly = false;
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
    // The first grain of the running (or the last) transition.
    std::uint64_t start = 0;
    // The keyers tied to the running transition.
    std::vector<int> tied;
    TransitionSettings next;
};

// A keyer's run along its keyframes (docs/engine-p4.md §5).
struct KeyRunView
{
    // A run waits for its first grain or runs; `fly`: it is the keyer's Auto.
    bool running = false;
    bool fly = false;
    DmeLoop loop = DmeLoop::Off;
    // Its first grain and the step of the last grain rendered (1 = the first).
    std::uint64_t start = 0;
    std::uint64_t step = 0;
    // The keyframe the box moves to, or (not running) the one it is at; −1: none.
    int keyframe = -1;
    // On the keyframes' path at timeline position u (grains), or off it (edited by hand).
    bool onPath = false;
    float u = 0.f;
};

struct KeyView
{
    int key = 1;
    int me = 1;
    bool upstream = false;
    // On air (or going on air during an auto, a fly or a tied transition).
    bool on = false;
    bool transition = false;
    // The ME whose transition it is tied to (0: none).
    int tie = 0;
    float gain = 0.f;
    // The box in `settings.dme` is the one of the last grain rendered.
    KeySettings settings;
    std::vector<DmeKeyframe> keyframes;
    KeyRunView run;
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
    BadKeyframes,
    BadTies,
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
    // The ME's next transition (type, rate, wipe, ties: upstream keyers of that ME, each once). A
    // running transition keeps its own.
    MixResult setTransition(int me, TransitionSettings const& settings);
    // Program goes to `target`: at the next grain (a cut, or frames 0), by a Mix or the ME's Wipe
    // over `frames` grains, or by the stinger `sting` (a stinger without one is a cut). Preview
    // becomes `preview` when it is given (at the end of the transition). A take during a
    // transition ends that one first (buses and tied keyers as at its end). With `ties` the ME's
    // tied keyers go on or off with it: with a cut at once; with a Mix or Wipe a fading keyer fades
    // with its t and a flying keyer flies along its keyframes with t (squeezed into the
    // transition's frames); with a stinger they cut at its cut point.
    MixResult take(int me, Source target, int frames, std::optional<Source> preview, Transition type = Transition::Mix, Sting sting = {},
        bool ties = false);
    // Program and Preview swap at the next grain and the tied keyers cut; a running transition ends
    // there instead (buses and tied keyers as at its end).
    MixResult cut(int me);
    // The ME's next transition from Program to Preview over `frames` grains (its rate when not
    // given), with its tied keyers, then the buses swap. From a held T-bar position it runs the
    // rest at that rate.
    MixResult autoTransition(int me, std::optional<int> frames = std::nullopt);
    // The T-bar: t > 0 starts the ME's next transition (Program to Preview, with its tied keyers)
    // and holds it at t; t = 1 completes it (the buses swap after that grain); t = 0 ends it
    // without the swap (the tied keyers as they were). A stinger does not scrub: t > 0 starts it
    // as an Auto. Busy while an Auto runs.
    MixResult position(int me, float t);

    // Keyer k (1-based): on or off at the next grain, or a fade to the other state over `frames`
    // grains (an auto during an auto turns back from where it is). Either takes the keyer out of a
    // tied transition where it is. A keyer that flies (`fly`, a
    // box, at least 2 keyframes): its Auto on turns it on and runs its box from keyframe 0 to the
    // last; Auto off runs from where the box is to keyframe 0 and turns it off after that grain;
    // an Auto during a fly turns back from where the box is. Cut on and off jump to the last
    // keyframe and to keyframe 0.
    MixResult keySet(int key, bool on);
    MixResult keyAuto(int key, int frames);
    // Its mode and settings; a luma key's or a pip's source must be one its ME may take. A pip
    // and a box (dme) only on upstream keyers, with the ranges of dmeValid. `editBox` changes the
    // keyer's current box (without it the box becomes settings.dme). A change of an animated box
    // field stops a run (the box holds there, with the change).
    MixResult keySettings(int key, KeySettings const& settings, std::function<void(DmeSettings&)> const& editBox = {});
    // Its keyframes (0..8, upstream keyers): a run stops where the box is.
    MixResult keyKeyframes(int key, std::vector<DmeKeyframe> const& keyframes);
    // A run of its box (a box and keyframes needed): from where the box is to keyframe `to` (the
    // last, keyframe 0 with `reverse`), through the keyframes in between at their own frames and
    // ease, starting at the next grain; a loop (wrap, pingpong; 2 keyframes) runs until stopped;
    // `frames` 0 jumps to keyframe `to` at the next grain. A fly in progress ends where it is.
    MixResult keyRun(int key, std::optional<int> to, bool reverse, DmeLoop loop, std::optional<int> frames);
    // Stops a run: the box holds where it is (a keyer that was flying off stays on air).
    MixResult keyStop(int key);
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
    // A keyer's (with its place and settings) or an FTB's fade; a keyer's keyframes and run.
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
        std::vector<DmeKeyframe> keyframes;
        // Where the box is: on the path at u (keyframe `keyframe` when exactly at one), or off it.
        bool onPath = false;
        float u = 0.f;
        int keyframe = -1;
        // A run: waiting for its first grain, or running since runStart (runStep: the last step).
        bool runPending = false;
        bool runActive = false;
        std::uint64_t runStart = 0;
        std::uint64_t runStep = 0;
        DmeRun run;
        // The run is the keyer's Auto (a fly; off when `on` is false).
        bool flying = false;
        // Tied to ME `tieMe`'s transition (0: none). With a stinger it cuts at the cut point
        // (tieAtCut). Else a fade from gain tieFrom to tieTo with the transition's t, or (tieFly) a
        // fly to keyframe tieTarget: on the path from u tieU0 to tieU1, or off it from box tieBox.
        // tieGain0: its gain before (a T-bar back at 0 returns there).
        int tieMe = 0;
        bool tieAtCut = false;
        bool tieFly = false;
        float tieFrom = 0.f;
        float tieTo = 0.f;
        float tieGain0 = 0.f;
        bool tieOnPath = false;
        float tieU0 = 0.f;
        float tieU1 = 0.f;
        int tieTarget = 0;
        DmeSettings tieBox;
    };
    // What ME m's transition does to its tied keyers in one grain: they follow its t (`active`),
    // cut (`cut`: the stinger's cut point, before the grain), end with it (`end`, after the grain)
    // or go back to where they were (`back`: the T-bar at 0).
    struct TieStep
    {
        bool active = false;
        float t = 0.f;
        bool cut = false;
        bool end = false;
        bool back = false;
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
    // A keyer's box along its keyframes (lock held): whether its Auto flies; the keyframe the
    // box is at (if any); a jump to keyframe j; a run from where the box is; a stop where it is;
    // the box of grain `index` (true when a run ended at that grain).
    static bool canFly(Fade const& key);
    static void placeOnPath(Fade& key);
    static void jumpTo(Fade& key, int j);
    static void startRun(Fade& key, int to, DmeLoop loop, bool reverse);
    static void stopRun(Fade& key);
    static bool runAdvance(Fade& key, std::uint64_t index);
    // Keyer ties (lock held): a cut of a keyer (to its last keyframe or keyframe 0 when it flies);
    // a keyer tied to ME me's transition (to its stinger's cut point with atCut); its state at the
    // transition's t; its end (complete, or back where it was); out of its tie where it is; the end
    // of every keyer tied to ME me; whether `ties` are upstream keyers of ME me, each once.
    static void cutKey(Fade& key, bool on);
    static void tieStart(Fade& key, int me, bool atCut);
    static void tieAdvance(Fade& key, float t);
    static void tieEnd(Fade& key, bool complete);
    static void untie(Fade& key);
    void endTies(int me, bool complete);
    [[nodiscard]] bool tiesValid(int me, std::vector<int> const& ties) const;
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
