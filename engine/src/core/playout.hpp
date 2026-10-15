// A clip player's transport: which clip frame it shows in each output grain. No GPU, no files:
// the control thread changes it, the render thread advances it once per output grain, the
// decoder reads where to decode from.
#pragma once

#include <cstdint>
#include <mutex>
#include <optional>
#include <string>

namespace fxeng
{
class Playout
{
public:
    enum class State
    {
        // No media: black.
        Empty,
        // Holds a frame, waiting for play (or play on cut).
        Cued,
        Playing,
        Paused,
        // At the end without loop: holds the last frame.
        Ended
    };

    struct Step
    {
        // The clip frame to show; -1: black (empty).
        long long frame = -1;
        // A playing frame (its sound plays); false while held.
        bool playing = false;
        // The jump this frame belongs to, and its place in play order after the jump (0: the
        // jump frame; a loop counts on past the end). The decoder numbers its frames the same way.
        std::uint64_t generation = 0;
        long long seq = 0;
    };

    // Where the decoder decodes from: the jump frame (seq 0) and the ones after it in play
    // order. `generation` changes on every jump (load, cue, stop, play from the end); a loop's
    // wrap is no jump (the decoder wraps itself).
    struct Cursor
    {
        std::uint64_t generation = 0;
        long long start = 0;
        long long frames = 0;
        bool loop = false;
        bool empty = true;
    };

    // A clip of `frames` frames (a still: 1), cued on its first frame.
    void load(long long frames, bool loop, bool playOnCut);
    void unload();
    void setLoop(bool loop);
    void setPlayOnCut(bool playOnCut);
    // Shows `frame` (clamped to the clip) and holds it.
    void cue(long long frame);
    void play();
    void pause();
    // Back to the first frame and hold.
    void stop();

    // Render thread, once per output grain (grains in between that were skipped advance a
    // playing clip too, so it stays on the TAI timeline). `onAir`: the player is in an ME's
    // Program; going on air starts a cued, paused or ended clip when play on cut is set.
    Step advance(std::uint64_t index, bool onAir);

    [[nodiscard]] Cursor cursor() const;
    [[nodiscard]] State state() const;
    // The frame shown now.
    [[nodiscard]] long long position() const;
    // The output grain that shows the clip's last frame: while it plays without loop (once it has
    // shown a frame), or after it ended there; nothing otherwise (empty, cued, paused, looping).
    // The control plane times a clip player's end action by it (docs/clip-end.md).
    [[nodiscard]] std::optional<std::uint64_t> endIndex() const;
    [[nodiscard]] long long frames() const;
    [[nodiscard]] bool loop() const;
    [[nodiscard]] bool playOnCut() const;

private:
    void jump(long long frame);

    mutable std::mutex mu_;
    State state_ = State::Empty;
    long long frames_ = 0;
    // The frame shown now, and the one a playing clip shows at the next advance; their play-order
    // numbers; the frame of the last jump.
    long long shown_ = 0;
    long long position_ = 0;
    long long shownSeq_ = 0;
    long long nextSeq_ = 0;
    long long jumpFrame_ = 0;
    bool loop_ = false;
    bool playOnCut_ = false;
    bool onAir_ = false;
    bool started_ = false;
    std::uint64_t lastIndex_ = 0;
    // The grain that showed the last frame when the clip ended by playing.
    std::optional<std::uint64_t> endedAt_;
    std::uint64_t generation_ = 1;
};

char const* stateName(Playout::State state);
} // namespace fxeng
