#include "core/mixer.hpp"

#include <algorithm>

namespace fxeng
{
std::optional<Source> parseSource(std::string const& text)
{
    if (text.size() < 3 || text.size() > 4)
    {
        return std::nullopt;
    }
    auto const prefix = text.substr(0, 2);
    auto const digits = text.substr(2);
    if (digits.find_first_not_of("0123456789") != std::string::npos || digits[0] == '0')
    {
        return std::nullopt;
    }
    int const index = std::stoi(digits);
    if (prefix == "in")
    {
        return Source{Source::Kind::Input, index};
    }
    if (prefix == "me")
    {
        return Source{Source::Kind::Me, index};
    }
    return std::nullopt;
}

std::string sourceName(Source source)
{
    return (source.kind == Source::Kind::Input ? "in" : "me") + std::to_string(source.index);
}

Mixer::Mixer(int mes, int inputs)
    : mes_(std::max(1, mes))
    , inputs_(std::max(1, inputs))
    , state_(static_cast<std::size_t>(mes_))
{
    // ME m starts on inputs 2m-1 (Program) and 2m (Preview), wrapped to the inputs there are.
    for (int m = 1; m <= mes_; ++m)
    {
        auto& me = state_[static_cast<std::size_t>(m - 1)];
        me.program = Source{Source::Kind::Input, (2 * m - 2) % inputs_ + 1};
        me.preview = Source{Source::Kind::Input, (2 * m - 1) % inputs_ + 1};
    }
}

bool Mixer::sourceAllowed(int me, Source source) const
{
    if (!validMe(me))
    {
        return false;
    }
    if (source.kind == Source::Kind::Input)
    {
        return source.index >= 1 && source.index <= inputs_;
    }
    return source.index > me && source.index <= mes_;
}

std::vector<int> Mixer::renderOrder() const
{
    std::vector<int> order;
    for (int m = mes_; m >= 1; --m)
    {
        order.push_back(m);
    }
    return order;
}

MixResult Mixer::setProgram(int me, Source source)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    if (!sourceAllowed(me, source))
    {
        return MixResult::BadSource;
    }
    std::lock_guard lock{mu_};
    auto& state = state_[static_cast<std::size_t>(me - 1)];
    if (state.mixing || state.pending)
    {
        return MixResult::Busy;
    }
    state.program = source;
    return MixResult::Ok;
}

MixResult Mixer::setPreview(int me, Source source)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    if (!sourceAllowed(me, source))
    {
        return MixResult::BadSource;
    }
    std::lock_guard lock{mu_};
    auto& state = state_[static_cast<std::size_t>(me - 1)];
    if (state.mixing || state.pending)
    {
        return MixResult::Busy;
    }
    state.preview = source;
    return MixResult::Ok;
}

MixResult Mixer::cut(int me)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    std::lock_guard lock{mu_};
    auto& state = state_[static_cast<std::size_t>(me - 1)];
    std::swap(state.program, state.preview);
    state.mixing = false;
    state.pending = false;
    state.position = 0;
    return MixResult::Ok;
}

MixResult Mixer::autoMix(int me, int frames)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    if (frames < 1 || frames > kMaxFrames)
    {
        return MixResult::BadFrames;
    }
    std::lock_guard lock{mu_};
    auto& state = state_[static_cast<std::size_t>(me - 1)];
    if (state.mixing || state.pending)
    {
        return MixResult::Busy;
    }
    state.frames = frames;
    state.pending = true;
    state.position = 0;
    return MixResult::Ok;
}

std::vector<MePlan> Mixer::frame(std::uint64_t index)
{
    std::vector<MePlan> plans;
    std::lock_guard lock{mu_};
    for (int m = mes_; m >= 1; --m)
    {
        auto& state = state_[static_cast<std::size_t>(m - 1)];
        if (state.pending)
        {
            state.pending = false;
            state.mixing = true;
            state.start = index;
        }
        MePlan plan;
        plan.me = m;
        plan.a = state.program;
        plan.b = state.preview;
        plan.preview = state.preview;
        if (state.mixing)
        {
            auto const done = index >= state.start ? index - state.start + 1 : 1;
            auto const step = static_cast<int>(std::min<std::uint64_t>(done, static_cast<std::uint64_t>(state.frames)));
            plan.mixing = true;
            plan.t = static_cast<float>(step) / static_cast<float>(state.frames);
            state.position = step;
            if (step >= state.frames)
            {
                // The last grain shows Preview in full; from the next one it is Program.
                std::swap(state.program, state.preview);
                state.mixing = false;
                state.position = 0;
            }
        }
        plans.push_back(plan);
    }
    return plans;
}

std::vector<MeView> Mixer::view() const
{
    std::vector<MeView> views;
    std::lock_guard lock{mu_};
    for (int m = 1; m <= mes_; ++m)
    {
        auto const& state = state_[static_cast<std::size_t>(m - 1)];
        MeView view;
        view.me = m;
        view.program = state.program;
        view.preview = state.preview;
        view.mixing = state.mixing || state.pending;
        view.frames = view.mixing ? state.frames : 0;
        view.position = state.position;
        views.push_back(view);
    }
    return views;
}
} // namespace fxeng
