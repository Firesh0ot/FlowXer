#include "engine/player.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstring>
#include <mutex>

namespace fxeng
{
void copyPlayerAudio(PlayerFrame const& frame, std::vector<float>& dst, std::size_t n, std::size_t ch)
{
    dst.assign(ch * n, 0.f);
    std::size_t const samples = std::min(frame.samples, n);
    for (std::size_t c = 0; c < std::min(ch, frame.channels); ++c)
    {
        std::memcpy(dst.data() + c * n, frame.audio.data() + c * frame.samples, samples * sizeof(float));
    }
}

// Milestone 1: the player holds no media and shows black.
struct ClipPlayer::Impl
{
    PlayerSetup setup;
    mutable std::mutex mu;
};

ClipPlayer::ClipPlayer(PlayerSetup setup)
    : impl_(std::make_unique<Impl>())
{
    impl_->setup = std::move(setup);
}

ClipPlayer::~ClipPlayer() = default;

void ClipPlayer::start()
{
}

void ClipPlayer::stop()
{
}

std::shared_ptr<PlayerFrame> ClipPlayer::frameFor(std::uint64_t index, bool /*onAir*/)
{
    auto frame = std::make_shared<PlayerFrame>();
    frame->index = index;
    return frame;
}

std::string ClipPlayer::command(PlayerCommand const& /*command*/)
{
    return "clip player " + std::to_string(impl_->setup.number) + ": no media support yet";
}

std::string ClipPlayer::statusJson() const
{
    return nlohmann::json{{"player", impl_->setup.number}, {"label", impl_->setup.label}, {"state", "empty"}}.dump();
}

std::size_t ClipPlayer::deviceBytes() const
{
    return 0;
}
} // namespace fxeng
