#include "core/mixer.hpp"

#include <algorithm>
#include <cmath>

namespace fxeng
{
std::optional<Source> parseSource(std::string const& text)
{
    if (text == "black")
    {
        return Source{Source::Kind::Black, 0};
    }
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
    if (prefix == "cp")
    {
        return Source{Source::Kind::Player, index};
    }
    if (prefix == "me")
    {
        return Source{Source::Kind::Me, index};
    }
    return std::nullopt;
}

std::string sourceName(Source source)
{
    switch (source.kind)
    {
    case Source::Kind::Input:
        return "in" + std::to_string(source.index);
    case Source::Kind::Player:
        return "cp" + std::to_string(source.index);
    case Source::Kind::Me:
        return "me" + std::to_string(source.index);
    case Source::Kind::Black:
        break;
    }
    return "black";
}

Mixer::Mixer(int mes, int inputs, int players, std::vector<int> dskMes)
    : mes_(std::max(1, mes))
    , inputs_(std::max(0, inputs))
    , players_(std::max(0, players))
    , state_(static_cast<std::size_t>(mes_))
{
    // ME m starts on inputs 2m-1 (Program) and 2m (Preview), wrapped to the inputs there are;
    // Black without inputs. FlowXer sets the buses right after the start.
    for (int m = 1; m <= mes_; ++m)
    {
        auto& me = state_[static_cast<std::size_t>(m - 1)];
        if (inputs_ > 0)
        {
            me.program = Source{Source::Kind::Input, (2 * m - 2) % inputs_ + 1};
            me.preview = Source{Source::Kind::Input, (2 * m - 1) % inputs_ + 1};
        }
    }
    for (int me : dskMes)
    {
        Dsk dsk;
        dsk.me = std::clamp(me, 1, mes_);
        dsks_.push_back(dsk);
    }
}

bool Mixer::sourceAllowed(int me, Source source) const
{
    if (!validMe(me))
    {
        return false;
    }
    switch (source.kind)
    {
    case Source::Kind::Input:
        return source.index >= 1 && source.index <= inputs_;
    case Source::Kind::Player:
        return source.index >= 1 && source.index <= players_;
    case Source::Kind::Black:
        return true;
    case Source::Kind::Me:
        return source.index > me && source.index <= mes_;
    }
    return false;
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

void Mixer::finishMix(Me& state)
{
    if (state.pending || state.mixing)
    {
        state.program = state.target;
        if (state.after)
        {
            state.preview = *state.after;
        }
    }
    state.pending = false;
    state.mixing = false;
    state.after.reset();
    state.position = 0;
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
    {
        std::lock_guard lock{mu_};
        auto& state = state_[static_cast<std::size_t>(me - 1)];
        state.pending = false;
        state.mixing = false;
        state.after.reset();
        state.position = 0;
        state.program = source;
    }
    notify();
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
    {
        std::lock_guard lock{mu_};
        auto& state = state_[static_cast<std::size_t>(me - 1)];
        state.preview = source;
        // The operator's choice wins over the swap at the end of a running Mix.
        state.after.reset();
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::take(int me, Source target, int frames, std::optional<Source> preview)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    if (!sourceAllowed(me, target) || (preview && !sourceAllowed(me, *preview)))
    {
        return MixResult::BadSource;
    }
    if (frames < 0 || frames > kMaxFrames)
    {
        return MixResult::BadFrames;
    }
    {
        std::lock_guard lock{mu_};
        auto& state = state_[static_cast<std::size_t>(me - 1)];
        finishMix(state);
        if (frames == 0)
        {
            state.program = target;
            if (preview)
            {
                state.preview = *preview;
            }
        }
        else
        {
            state.target = target;
            state.after = preview;
            state.frames = frames;
            state.pending = true;
        }
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::cut(int me)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    Source program;
    Source preview;
    {
        std::lock_guard lock{mu_};
        auto const& state = state_[static_cast<std::size_t>(me - 1)];
        program = state.program;
        preview = state.preview;
    }
    return take(me, preview, 0, program);
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
    Source program;
    Source preview;
    {
        std::lock_guard lock{mu_};
        auto const& state = state_[static_cast<std::size_t>(me - 1)];
        if (state.mixing || state.pending)
        {
            return MixResult::Busy;
        }
        program = state.program;
        preview = state.preview;
    }
    return take(me, preview, frames, program);
}

MixResult Mixer::dskSet(int key, bool on)
{
    if (key < 1 || key > static_cast<int>(dsks_.size()))
    {
        return MixResult::BadMe;
    }
    {
        std::lock_guard lock{mu_};
        auto& dsk = dsks_[static_cast<std::size_t>(key - 1)];
        dsk.on = on;
        dsk.pending = false;
        dsk.running = false;
        dsk.gain = on ? 1.f : 0.f;
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::dskAuto(int key, int frames)
{
    if (key < 1 || key > static_cast<int>(dsks_.size()))
    {
        return MixResult::BadMe;
    }
    if (frames < 1 || frames > kMaxFrames)
    {
        return MixResult::BadFrames;
    }
    {
        std::lock_guard lock{mu_};
        auto& dsk = dsks_[static_cast<std::size_t>(key - 1)];
        // An auto during an auto turns back from the gain it has reached.
        dsk.on = !dsk.on;
        dsk.from = dsk.gain;
        dsk.to = dsk.on ? 1.f : 0.f;
        dsk.frames = std::max(1, static_cast<int>(static_cast<float>(frames) * std::abs(dsk.to - dsk.from) + 0.5f));
        dsk.pending = true;
        dsk.running = false;
    }
    notify();
    return MixResult::Ok;
}

FramePlan Mixer::frame(std::uint64_t index)
{
    FramePlan out;
    bool ended = false;
    std::unique_lock lock{mu_};
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
        plan.b = state.mixing ? state.target : state.preview;
        plan.preview = state.preview;
        if (state.mixing)
        {
            auto const done = index >= state.start ? index - state.start + 1 : 1;
            auto const step = static_cast<int>(std::min<std::uint64_t>(done, static_cast<std::uint64_t>(state.frames)));
            plan.mixing = true;
            plan.t = static_cast<float>(step) / static_cast<float>(state.frames);
            plan.tPrev = static_cast<float>(step - 1) / static_cast<float>(state.frames);
            state.position = step;
            if (step >= state.frames)
            {
                // The last grain shows the target in full; from the next one it is Program.
                finishMix(state);
                ended = true;
            }
        }
        out.mes.push_back(plan);
    }
    int key = 1;
    for (auto& dsk : dsks_)
    {
        if (dsk.pending)
        {
            dsk.pending = false;
            dsk.running = true;
            dsk.start = index;
        }
        if (dsk.running)
        {
            auto const done = index >= dsk.start ? index - dsk.start + 1 : 1;
            float const t = std::min(1.f, static_cast<float>(done) / static_cast<float>(dsk.frames));
            dsk.gain = dsk.from + (dsk.to - dsk.from) * t;
            if (t >= 1.f)
            {
                dsk.running = false;
                dsk.gain = dsk.to;
                ended = true;
            }
        }
        out.dsks.push_back(DskPlan{key++, dsk.me, dsk.gain});
    }
    lock.unlock();
    if (ended)
    {
        notify();
    }
    return out;
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
        view.target = view.mixing ? state.target : state.program;
        view.frames = view.mixing ? state.frames : 0;
        view.position = state.position;
        views.push_back(view);
    }
    return views;
}

std::vector<DskView> Mixer::dskView() const
{
    std::vector<DskView> views;
    std::lock_guard lock{mu_};
    int key = 1;
    for (auto const& dsk : dsks_)
    {
        views.push_back(DskView{key++, dsk.me, dsk.on, dsk.pending || dsk.running, dsk.gain});
    }
    return views;
}
} // namespace fxeng
