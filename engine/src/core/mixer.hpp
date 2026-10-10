// ME state: Program/Preview buses, Cut and Mix, re-entry. No GPU, no MXL: the control
// thread changes it, the render thread takes one plan per output grain.
#pragma once

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
        Me
    };
    Kind kind = Kind::Input;
    // Input 1..8, or the ME whose Program is re-entered.
    int index = 1;

    bool operator==(Source const& other) const
    {
        return kind == other.kind && index == other.index;
    }
};

// "in1".."in8", "me2".."me4"; nothing for anything else.
std::optional<Source> parseSource(std::string const& text);
std::string sourceName(Source source);

// What one ME renders for one output grain.
struct MePlan
{
    int me = 1;
    // Program: a, dissolving to b by t while `mixing`.
    Source a;
    Source b;
    float t = 0.f;
    // t of the grain before (the audio crossfade ramps from tPrev to t within the grain).
    float tPrev = 0.f;
    bool mixing = false;
    Source preview;
};

struct MeView
{
    int me = 1;
    Source program;
    Source preview;
    bool mixing = false;
    int frames = 0;
    // Grains of the running Mix already rendered (0 while it waits for its first grain).
    int position = 0;
};

enum class MixResult
{
    Ok,
    BadMe,
    BadSource,
    BadFrames,
    Busy
};

class Mixer
{
public:
    Mixer(int mes, int inputs);

    // ME m may take inputs and the Program of ME n when n > m: the MEs render in descending
    // order, so the re-entered picture is from the same output grain and no loop can exist.
    [[nodiscard]] bool sourceAllowed(int me, Source source) const;
    // ME count .. 1.
    [[nodiscard]] std::vector<int> renderOrder() const;

    MixResult setProgram(int me, Source source);
    MixResult setPreview(int me, Source source);
    // Swaps Program and Preview at the next grain; a running Mix ends there.
    MixResult cut(int me);
    // Dissolve Program to Preview over `frames` grains, then swap the buses.
    MixResult autoMix(int me, int frames);

    // Render thread: the plans of output grain `index` in render order. A Mix starts at the
    // first grain after the command and its progress follows the grain index, so skipped
    // grains do not stretch it. A Mix ends (buses swapped) after its last grain.
    std::vector<MePlan> frame(std::uint64_t index);

    [[nodiscard]] std::vector<MeView> view() const;
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
        int frames = 0;
        bool pending = false;
        bool mixing = false;
        std::uint64_t start = 0;
        int position = 0;
    };

    [[nodiscard]] bool validMe(int me) const
    {
        return me >= 1 && me <= mes_;
    }

    int mes_;
    int inputs_;
    void notify() const
    {
        if (changed_)
        {
            changed_();
        }
    }

    mutable std::mutex mu_;
    std::vector<Me> state_;
    std::function<void()> changed_;
};
} // namespace fxeng
