#include "core/playout.hpp"

#include <algorithm>

namespace fxeng
{
char const* stateName(Playout::State state)
{
    switch (state)
    {
    case Playout::State::Empty:
        return "empty";
    case Playout::State::Cued:
        return "cued";
    case Playout::State::Playing:
        return "playing";
    case Playout::State::Paused:
        return "paused";
    case Playout::State::Ended:
        break;
    }
    return "ended";
}

void Playout::jump(long long frame)
{
    position_ = std::clamp(frame, 0LL, std::max(0LL, frames_ - 1));
    shown_ = position_;
    jumpFrame_ = position_;
    shownSeq_ = nextSeq_ = 0;
    ++generation_;
    started_ = false;
    endedAt_.reset();
}

void Playout::load(long long frames, bool loop, bool playOnCut)
{
    std::lock_guard lock{mu_};
    frames_ = std::max(1LL, frames);
    loop_ = loop;
    playOnCut_ = playOnCut;
    state_ = State::Cued;
    jump(0);
}

void Playout::unload()
{
    std::lock_guard lock{mu_};
    frames_ = 0;
    state_ = State::Empty;
    jump(0);
}

void Playout::setLoop(bool loop)
{
    std::lock_guard lock{mu_};
    loop_ = loop;
}

void Playout::setPlayOnCut(bool playOnCut)
{
    std::lock_guard lock{mu_};
    playOnCut_ = playOnCut;
}

void Playout::cue(long long frame)
{
    std::lock_guard lock{mu_};
    if (state_ == State::Empty)
    {
        return;
    }
    state_ = State::Cued;
    jump(frame);
}

void Playout::play()
{
    std::lock_guard lock{mu_};
    if (state_ == State::Empty || state_ == State::Playing)
    {
        return;
    }
    if (state_ == State::Ended)
    {
        jump(0);
    }
    state_ = State::Playing;
    started_ = false;
    endedAt_.reset();
}

void Playout::pause()
{
    std::lock_guard lock{mu_};
    if (state_ == State::Playing)
    {
        state_ = State::Paused;
    }
}

void Playout::stop()
{
    std::lock_guard lock{mu_};
    if (state_ == State::Empty)
    {
        return;
    }
    state_ = State::Cued;
    jump(0);
}

Playout::Step Playout::advance(std::uint64_t index, bool onAir)
{
    std::lock_guard lock{mu_};
    bool const rising = onAir && !onAir_;
    onAir_ = onAir;
    if (rising && playOnCut_ && (state_ == State::Cued || state_ == State::Paused || state_ == State::Ended))
    {
        if (state_ == State::Ended)
        {
            jump(0);
        }
        state_ = State::Playing;
        started_ = false;
    }
    if (state_ == State::Empty)
    {
        return Step{-1, false, generation_, 0};
    }
    if (state_ != State::Playing)
    {
        lastIndex_ = index;
        return Step{shown_, false, generation_, shownSeq_};
    }
    // Grains the render thread skipped: the clip stays on the TAI timeline.
    if (started_ && index > lastIndex_ + 1)
    {
        auto const skipped = static_cast<long long>(index - lastIndex_ - 1);
        position_ += skipped;
        nextSeq_ += skipped;
        if (position_ >= frames_)
        {
            if (loop_)
            {
                position_ %= frames_;
            }
            else
            {
                // Its last frame was due in a skipped grain.
                endedAt_ = lastIndex_ + 1 + static_cast<std::uint64_t>(frames_ - 1 - (position_ - skipped));
                nextSeq_ -= position_ - (frames_ - 1);
                position_ = shown_ = frames_ - 1;
                shownSeq_ = nextSeq_;
                state_ = State::Ended;
                lastIndex_ = index;
                return Step{shown_, false, generation_, shownSeq_};
            }
        }
    }
    started_ = true;
    lastIndex_ = index;
    shown_ = position_;
    shownSeq_ = nextSeq_;
    Step const step{shown_, true, generation_, shownSeq_};
    if (position_ + 1 < frames_)
    {
        ++position_;
        ++nextSeq_;
    }
    else if (loop_ && frames_ > 1)
    {
        position_ = 0;
        ++nextSeq_;
    }
    else
    {
        // The last frame was shown: hold it.
        state_ = State::Ended;
        endedAt_ = index;
    }
    return step;
}

Playout::Cursor Playout::cursor() const
{
    std::lock_guard lock{mu_};
    return Cursor{generation_, jumpFrame_, frames_, loop_, state_ == State::Empty};
}

Playout::State Playout::state() const
{
    std::lock_guard lock{mu_};
    return state_;
}

long long Playout::position() const
{
    std::lock_guard lock{mu_};
    return shown_;
}

std::optional<std::uint64_t> Playout::endIndex() const
{
    std::lock_guard lock{mu_};
    if (state_ == State::Ended)
    {
        return endedAt_;
    }
    if (state_ != State::Playing || loop_ || !started_)
    {
        return std::nullopt;
    }
    return lastIndex_ + static_cast<std::uint64_t>(frames_ - 1 - shown_);
}

long long Playout::frames() const
{
    std::lock_guard lock{mu_};
    return frames_;
}

bool Playout::loop() const
{
    std::lock_guard lock{mu_};
    return loop_;
}

bool Playout::playOnCut() const
{
    std::lock_guard lock{mu_};
    return playOnCut_;
}
} // namespace fxeng
