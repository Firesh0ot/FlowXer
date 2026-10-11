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

std::optional<Transition> parseTransition(std::string const& text)
{
    for (auto type : {Transition::Cut, Transition::Mix, Transition::Wipe, Transition::Stinger})
    {
        if (text == transitionName(type))
        {
            return type;
        }
    }
    return std::nullopt;
}

std::string transitionName(Transition type)
{
    switch (type)
    {
    case Transition::Cut:
        return "cut";
    case Transition::Mix:
        return "mix";
    case Transition::Wipe:
        return "wipe";
    case Transition::Stinger:
        break;
    }
    return "stinger";
}

Mixer::Mixer(int mes, int inputs, int players, std::vector<KeyerSpec> keyers)
    : mes_(std::max(1, mes))
    , inputs_(std::max(0, inputs))
    , players_(std::max(0, players))
    , state_(static_cast<std::size_t>(mes_))
    , ftbs_(static_cast<std::size_t>(mes_))
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
    for (auto const& spec : keyers)
    {
        Fade key;
        key.me = std::clamp(spec.me, 1, mes_);
        key.upstream = spec.upstream;
        keys_.push_back(key);
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
    state.wiping = false;
    state.manual = false;
    state.from = 0.f;
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
        state.wiping = false;
        state.manual = false;
        state.from = 0.f;
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

MixResult Mixer::setTransition(int me, TransitionSettings const& settings)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    if (settings.frames < 1 || settings.frames > kMaxFrames)
    {
        return MixResult::BadFrames;
    }
    if (!wipeCodeValid(settings.wipe.pattern) || !(settings.wipe.soft >= 0.f && settings.wipe.soft <= 1.f) ||
        !(settings.wipe.border >= 0.f && settings.wipe.border <= 1.f))
    {
        return MixResult::BadPattern;
    }
    {
        std::lock_guard lock{mu_};
        state_[static_cast<std::size_t>(me - 1)].next = settings;
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::take(int me, Source target, int frames, std::optional<Source> preview, Transition type)
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
        if (frames == 0 || type == Transition::Cut || type == Transition::Stinger)
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
            state.wiping = type == Transition::Wipe;
            state.wipe = state.next.wipe;
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

MixResult Mixer::autoTransition(int me, std::optional<int> frames)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    if (frames && (*frames < 1 || *frames > kMaxFrames))
    {
        return MixResult::BadFrames;
    }
    Source program;
    Source preview;
    TransitionSettings next;
    bool resumed = false;
    {
        std::lock_guard lock{mu_};
        auto& state = state_[static_cast<std::size_t>(me - 1)];
        if (state.manual)
        {
            // From the held T-bar position: the rest at the rate, from the next grain.
            int const rate = frames.value_or(state.next.frames);
            state.frames = std::max(1, static_cast<int>(std::lround(static_cast<float>(rate) * (1.f - state.t))));
            state.from = state.t;
            state.manual = false;
            state.mixing = false;
            state.pending = true;
            state.position = 0;
            resumed = true;
        }
        else if (state.mixing || state.pending)
        {
            return MixResult::Busy;
        }
        else
        {
            program = state.program;
            preview = state.preview;
            next = state.next;
        }
    }
    if (resumed)
    {
        notify();
        return MixResult::Ok;
    }
    return take(me, preview, frames.value_or(next.frames), program, next.type);
}

MixResult Mixer::position(int me, float t)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    if (!(t >= 0.f && t <= 1.f))
    {
        return MixResult::BadPosition;
    }
    bool sting = false;
    {
        std::lock_guard lock{mu_};
        auto& state = state_[static_cast<std::size_t>(me - 1)];
        if (state.pending || (state.mixing && !state.manual))
        {
            return MixResult::Busy;
        }
        if (!state.mixing)
        {
            if (t <= 0.f)
            {
                return MixResult::Ok;
            }
            sting = state.next.type == Transition::Stinger || state.next.type == Transition::Cut;
            if (!sting)
            {
                state.target = state.preview;
                state.after = state.program;
                state.frames = state.next.frames;
                state.wiping = state.next.type == Transition::Wipe;
                state.wipe = state.next.wipe;
                state.mixing = true;
                state.manual = true;
                state.tLast = 0.f;
            }
        }
        state.t = t;
    }
    if (sting)
    {
        return autoTransition(me);
    }
    notify();
    return MixResult::Ok;
}

void Mixer::fadeSet(Fade& fade, bool on)
{
    fade.on = on;
    fade.pending = false;
    fade.running = false;
    fade.gain = on ? 1.f : 0.f;
}

void Mixer::fadeAuto(Fade& fade, int frames)
{
    // An auto during an auto turns back from the gain it has reached.
    fade.on = !fade.on;
    fade.from = fade.gain;
    fade.to = fade.on ? 1.f : 0.f;
    fade.frames = std::max(1, static_cast<int>(static_cast<float>(frames) * std::abs(fade.to - fade.from) + 0.5f));
    fade.pending = true;
    fade.running = false;
}

bool Mixer::fadeAdvance(Fade& fade, std::uint64_t index)
{
    if (fade.pending)
    {
        fade.pending = false;
        fade.running = true;
        fade.start = index;
    }
    if (!fade.running)
    {
        return false;
    }
    auto const done = index >= fade.start ? index - fade.start + 1 : 1;
    float const t = std::min(1.f, static_cast<float>(done) / static_cast<float>(fade.frames));
    fade.gain = fade.from + (fade.to - fade.from) * t;
    if (t >= 1.f)
    {
        fade.running = false;
        fade.gain = fade.to;
        return true;
    }
    return false;
}

MixResult Mixer::ftbSet(int me, bool on)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    {
        std::lock_guard lock{mu_};
        fadeSet(ftbs_[static_cast<std::size_t>(me - 1)], on);
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::ftbAuto(int me, int frames)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    if (frames < 1 || frames > kMaxFrames)
    {
        return MixResult::BadFrames;
    }
    {
        std::lock_guard lock{mu_};
        fadeAuto(ftbs_[static_cast<std::size_t>(me - 1)], frames);
    }
    notify();
    return MixResult::Ok;
}

float Mixer::ftbGain(int me) const
{
    std::lock_guard lock{mu_};
    return validMe(me) ? ftbs_[static_cast<std::size_t>(me - 1)].gain : 0.f;
}

MixResult Mixer::keySet(int key, bool on)
{
    if (key < 1 || key > static_cast<int>(keys_.size()))
    {
        return MixResult::BadMe;
    }
    {
        std::lock_guard lock{mu_};
        fadeSet(keys_[static_cast<std::size_t>(key - 1)], on);
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::keySettings(int key, KeySettings const& settings)
{
    if (key < 1 || key > static_cast<int>(keys_.size()))
    {
        return MixResult::BadMe;
    }
    auto const in01 = [](float v) { return v >= 0.f && v <= 1.f; };
    if (!in01(settings.clip) || !in01(settings.soft) || !in01(settings.gain))
    {
        return MixResult::BadKey;
    }
    if (settings.luma && !sourceAllowed(keys_[static_cast<std::size_t>(key - 1)].me, settings.source))
    {
        return MixResult::BadSource;
    }
    {
        std::lock_guard lock{mu_};
        keys_[static_cast<std::size_t>(key - 1)].settings = settings;
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::keyAuto(int key, int frames)
{
    if (key < 1 || key > static_cast<int>(keys_.size()))
    {
        return MixResult::BadMe;
    }
    if (frames < 1 || frames > kMaxFrames)
    {
        return MixResult::BadFrames;
    }
    {
        std::lock_guard lock{mu_};
        fadeAuto(keys_[static_cast<std::size_t>(key - 1)], frames);
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
        plan.wiping = state.mixing && state.wiping;
        plan.wipe = state.wipe;
        if (state.mixing && state.manual)
        {
            // The T-bar: held at t; the grain at t = 1 shows the target in full, then the buses
            // swap; at t = 0 the transition ends without the swap.
            plan.mixing = true;
            plan.t = state.t;
            plan.tPrev = state.tLast;
            state.tLast = state.t;
            if (state.t >= 1.f)
            {
                finishMix(state);
                ended = true;
            }
            else if (state.t <= 0.f)
            {
                state.mixing = false;
                state.manual = false;
                state.wiping = false;
                state.after.reset();
                ended = true;
            }
        }
        else if (state.mixing)
        {
            // An Auto from `from` (0, or a held T-bar position) to 1; its progress follows the
            // grain index.
            auto const done = index >= state.start ? index - state.start + 1 : 1;
            auto const step = static_cast<int>(std::min<std::uint64_t>(done, static_cast<std::uint64_t>(state.frames)));
            float const span = 1.f - state.from;
            plan.mixing = true;
            plan.t = step >= state.frames ? 1.f : state.from + span * (static_cast<float>(step) / static_cast<float>(state.frames));
            plan.tPrev = state.from + span * (static_cast<float>(step - 1) / static_cast<float>(state.frames));
            state.position = step;
            if (step >= state.frames)
            {
                // The last grain shows the target in full; from the next one it is Program.
                finishMix(state);
                ended = true;
            }
        }
        auto& ftb = ftbs_[static_cast<std::size_t>(m - 1)];
        plan.ftbPrev = ftb.gain;
        ended = fadeAdvance(ftb, index) || ended;
        plan.ftb = ftb.gain;
        out.mes.push_back(plan);
    }
    int key = 1;
    for (auto& keyer : keys_)
    {
        ended = fadeAdvance(keyer, index) || ended;
        out.keys.push_back(KeyPlan{key++, keyer.me, keyer.upstream, keyer.gain, keyer.settings});
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
        view.wiping = view.mixing && state.wiping;
        view.target = view.mixing ? state.target : state.program;
        view.frames = view.mixing ? state.frames : 0;
        view.position = state.position;
        view.manual = state.manual;
        view.t = state.manual ? state.t : 0.f;
        view.next = state.next;
        views.push_back(view);
    }
    return views;
}

std::vector<KeyView> Mixer::keyView() const
{
    std::vector<KeyView> views;
    std::lock_guard lock{mu_};
    int key = 1;
    for (auto const& keyer : keys_)
    {
        views.push_back(KeyView{key++, keyer.me, keyer.upstream, keyer.on, keyer.pending || keyer.running, keyer.gain, keyer.settings});
    }
    return views;
}
} // namespace fxeng
