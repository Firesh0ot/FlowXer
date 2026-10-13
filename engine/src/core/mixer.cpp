#include "core/mixer.hpp"

#include "core/audio.hpp"

#include <algorithm>
#include <cmath>
#include <utility>

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
    , deferred_(static_cast<std::size_t>(mes_))
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
    state.sting = Sting{};
    state.waiting = false;
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
        state.sting = Sting{};
        state.waiting = false;
        state.program = source;
        endTies(me, true);
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
    if (!tiesValid(me, settings.ties))
    {
        return MixResult::BadTies;
    }
    {
        std::lock_guard lock{mu_};
        state_[static_cast<std::size_t>(me - 1)].next = settings;
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::take(int me, Source target, int frames, std::optional<Source> preview, Transition type, Sting sting, bool ties)
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
        endTies(me, true);
        std::vector<int> const tied = ties ? state.next.ties : std::vector<int>{};
        if (type == Transition::Stinger && sting.player > 0 && sting.frames > 0)
        {
            state.target = target;
            state.after = preview;
            state.frames = sting.frames;
            state.sting = sting;
            state.sting.cut = std::clamp(sting.cut, 0, sting.frames);
            state.pending = true;
            for (int k : tied)
            {
                tieStart(keys_[static_cast<std::size_t>(k - 1)], me, true);
            }
        }
        else if (frames == 0 || type == Transition::Cut || type == Transition::Stinger)
        {
            state.program = target;
            if (preview)
            {
                state.preview = *preview;
            }
            for (int k : tied)
            {
                auto& keyer = keys_[static_cast<std::size_t>(k - 1)];
                cutKey(keyer, !keyer.on);
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
            for (int k : tied)
            {
                tieStart(keys_[static_cast<std::size_t>(k - 1)], me, false);
            }
        }
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::takeAt(std::uint64_t at, int me, Source target, int frames, std::optional<Source> preview, Transition type, Sting sting, bool ties)
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
    std::lock_guard lock{mu_};
    deferred_[static_cast<std::size_t>(me - 1)] = Deferred{at, target, frames, preview, type, sting, ties};
    return MixResult::Ok;
}

void Mixer::runHeld(std::uint64_t index, bool all, std::optional<int> me)
{
    std::vector<std::pair<int, Deferred>> due;
    {
        std::lock_guard lock{mu_};
        for (int m = 1; m <= mes_; ++m)
        {
            auto& held = deferred_[static_cast<std::size_t>(m - 1)];
            if (held && (me ? m == *me : true) && (all || held->at <= index))
            {
                due.emplace_back(m, *held);
                held.reset();
            }
        }
    }
    for (auto const& [m, d] : due)
    {
        take(m, d.target, d.frames, d.preview, d.type, d.sting, d.ties);
        std::lock_guard lock{mu_};
        state_[static_cast<std::size_t>(m - 1)].heldMade = all ? 0 : index;
    }
}

void Mixer::runDeferred(std::optional<int> me)
{
    runHeld(0, true, me);
}

MixResult Mixer::cut(int me)
{
    if (!validMe(me))
    {
        return MixResult::BadMe;
    }
    Source program;
    Source preview;
    bool running = false;
    {
        std::lock_guard lock{mu_};
        auto const& state = state_[static_cast<std::size_t>(me - 1)];
        program = state.program;
        preview = state.preview;
        running = state.pending || state.mixing;
    }
    // A Cut during a transition ends it there: the buses and the tied keyers as at its end.
    return take(me, preview, 0, program, Transition::Cut, {}, !running);
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
    return take(me, preview, frames.value_or(next.frames), program, next.type, {}, true);
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
                for (int k : state.next.ties)
                {
                    tieStart(keys_[static_cast<std::size_t>(k - 1)], me, false);
                }
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

bool Mixer::canFly(Fade const& key)
{
    return key.settings.fly && key.upstream && (key.settings.pip || key.settings.dme.enabled) && key.keyframes.size() >= 2;
}

void Mixer::placeOnPath(Fade& key)
{
    key.keyframe = dmeKeyframeOf(key.keyframes, key.settings.dme);
    key.onPath = key.keyframe >= 0;
    key.u = key.onPath ? dmeKeyTime(key.keyframes, key.keyframe) : 0.f;
}

void Mixer::jumpTo(Fade& key, int j)
{
    key.runPending = false;
    key.runActive = false;
    key.settings.dme = dmeWithState(key.settings.dme, key.keyframes[static_cast<std::size_t>(j)].box);
    key.onPath = true;
    key.u = dmeKeyTime(key.keyframes, j);
    key.keyframe = j;
}

void Mixer::startRun(Fade& key, int to, DmeLoop loop, bool reverse)
{
    key.run = dmeRunFrom(key.keyframes, key.settings.dme, key.onPath, key.u, to, loop, reverse);
    key.runPending = true;
    key.runActive = false;
    key.runStep = 0;
}

void Mixer::stopRun(Fade& key)
{
    bool const tiedFly = key.tieMe > 0 && key.tieFly && !key.tieAtCut;
    if (!key.on && ((key.flying && (key.runPending || key.runActive)) || tiedFly))
    {
        // A fly off that stops (its own or a tied one): the box is still visible, so the keyer
        // stays on air.
        key.on = true;
    }
    key.runPending = false;
    key.runActive = false;
    key.flying = false;
    if (tiedFly)
    {
        // Out of its tie: on air where the box is.
        untie(key);
        key.gain = 1.f;
    }
}

bool Mixer::runAdvance(Fade& key, std::uint64_t index)
{
    if (key.runPending)
    {
        key.runPending = false;
        key.runActive = true;
        key.runStart = index;
    }
    if (!key.runActive)
    {
        return false;
    }
    key.runStep = index >= key.runStart ? index - key.runStart + 1 : 1;
    auto const state = dmeRunAt(key.keyframes, key.run, key.runStep, key.settings.dme);
    key.settings.dme = state.box;
    key.onPath = state.onPath;
    key.u = state.u;
    key.keyframe = state.keyframe;
    if (state.done)
    {
        key.runActive = false;
    }
    return state.done;
}

void Mixer::cutKey(Fade& key, bool on)
{
    untie(key);
    if (canFly(key))
    {
        // A cut of a flying keyer jumps: on at the last keyframe, off at keyframe 0.
        stopRun(key);
        jumpTo(key, on ? static_cast<int>(key.keyframes.size()) - 1 : 0);
    }
    fadeSet(key, on);
}

void Mixer::tieStart(Fade& key, int me, bool atCut)
{
    key.tieMe = me;
    key.tieAtCut = atCut;
    if (atCut)
    {
        // With a stinger the keyer cuts at its cut point (as it is then).
        return;
    }
    // The keyer goes to its other state with the transition: its own fade or fly stops where it
    // is, a fly on starts at keyframe 0 unless it turns back a fly off.
    bool const turningBack = key.flying && (key.runPending || key.runActive);
    key.tieFly = canFly(key);
    key.tieGain0 = key.gain;
    key.pending = false;
    key.running = false;
    key.runPending = false;
    key.runActive = false;
    key.flying = false;
    key.on = !key.on;
    if (!key.tieFly)
    {
        key.tieFrom = key.gain;
        key.tieTo = key.on ? 1.f : 0.f;
        return;
    }
    if (key.on && !turningBack)
    {
        jumpTo(key, 0);
    }
    key.tieTarget = key.on ? static_cast<int>(key.keyframes.size()) - 1 : 0;
    key.tieOnPath = key.onPath;
    key.tieU0 = key.u;
    key.tieU1 = dmeKeyTime(key.keyframes, key.tieTarget);
    key.tieBox = key.settings.dme;
}

void Mixer::tieAdvance(Fade& key, float t)
{
    if (!key.tieFly)
    {
        key.gain = tieAt(key.tieFrom, key.tieTo, t);
        return;
    }
    // A fly: on air while it runs; the keyframes' timeline squeezed into the transition (a
    // fractional position), or straight to the target keyframe with its ease from off the path.
    key.gain = 1.f;
    auto const& target = key.keyframes[static_cast<std::size_t>(key.tieTarget)];
    if (key.tieOnPath)
    {
        key.u = tieAt(key.tieU0, key.tieU1, t);
        key.settings.dme = dmePathAt(key.keyframes, key.u, key.settings.dme);
        key.onPath = true;
    }
    else
    {
        key.settings.dme = dmeBetween(key.settings.dme, key.tieBox, target.box, easeAt(target.ease, t));
        key.onPath = t >= 1.f;
        key.u = key.tieU1;
    }
    key.keyframe = key.tieTarget;
}

void Mixer::tieEnd(Fade& key, bool complete)
{
    if (key.tieMe == 0)
    {
        return;
    }
    if (key.tieAtCut)
    {
        // Its stinger ended before its cut point (or became a cut): the keyer cuts with it.
        untie(key);
        if (complete)
        {
            cutKey(key, !key.on);
        }
        return;
    }
    tieAdvance(key, complete ? 1.f : 0.f);
    if (!complete)
    {
        // The T-bar back at 0: as it was.
        key.on = !key.on;
        key.gain = key.tieGain0;
    }
    else if (key.tieFly && !key.on)
    {
        // A fly off ends at keyframe 0: off from the next grain.
        key.gain = 0.f;
    }
    untie(key);
}

void Mixer::untie(Fade& key)
{
    key.tieMe = 0;
    key.tieAtCut = false;
}

void Mixer::endTies(int me, bool complete)
{
    for (auto& keyer : keys_)
    {
        if (keyer.tieMe == me)
        {
            tieEnd(keyer, complete);
        }
    }
}

bool Mixer::tiesValid(int me, std::vector<int> const& ties) const
{
    for (auto it = ties.begin(); it != ties.end(); ++it)
    {
        int const k = *it;
        bool const known = k >= 1 && k <= static_cast<int>(keys_.size());
        if (!known || keys_[static_cast<std::size_t>(k - 1)].me != me || !keys_[static_cast<std::size_t>(k - 1)].upstream ||
            std::find(ties.begin(), it, k) != it)
        {
            return false;
        }
    }
    return true;
}

MixResult Mixer::keySet(int key, bool on)
{
    if (key < 1 || key > static_cast<int>(keys_.size()))
    {
        return MixResult::BadMe;
    }
    {
        std::lock_guard lock{mu_};
        cutKey(keys_[static_cast<std::size_t>(key - 1)], on);
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::keySettings(int key, KeySettings const& settings, std::function<void(DmeSettings&)> const& editBox)
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
    auto const& spec = keys_[static_cast<std::size_t>(key - 1)];
    if (settings.luma && settings.pip)
    {
        return MixResult::BadKey;
    }
    if ((settings.luma || settings.pip) && !sourceAllowed(spec.me, settings.source))
    {
        return MixResult::BadSource;
    }
    {
        std::lock_guard lock{mu_};
        auto& keyer = keys_[static_cast<std::size_t>(key - 1)];
        DmeSettings box = keyer.settings.dme;
        if (editBox)
        {
            editBox(box);
        }
        else
        {
            box = settings.dme;
        }
        box.enabled = settings.pip || box.enabled;
        if (box.enabled && (!spec.upstream || !dmeValid(box)))
        {
            return MixResult::BadDme;
        }
        bool const moved = !dmeSameState(box, keyer.settings.dme);
        keyer.settings = settings;
        keyer.settings.dme = box;
        if (moved)
        {
            stopRun(keyer);
            placeOnPath(keyer);
        }
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::keyKeyframes(int key, std::vector<DmeKeyframe> const& keyframes)
{
    if (key < 1 || key > static_cast<int>(keys_.size()))
    {
        return MixResult::BadMe;
    }
    if (!keys_[static_cast<std::size_t>(key - 1)].upstream)
    {
        return MixResult::BadDme;
    }
    if (keyframes.size() > static_cast<std::size_t>(kMaxKeyframes))
    {
        return MixResult::BadKeyframes;
    }
    for (auto const& keyframe : keyframes)
    {
        if (keyframe.frames < 1 || keyframe.frames > kMaxFrames)
        {
            return MixResult::BadFrames;
        }
        if (!dmeValid(keyframe.box))
        {
            return MixResult::BadDme;
        }
    }
    {
        std::lock_guard lock{mu_};
        auto& keyer = keys_[static_cast<std::size_t>(key - 1)];
        stopRun(keyer);
        keyer.keyframes = keyframes;
        placeOnPath(keyer);
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::keyRun(int key, std::optional<int> to, bool reverse, DmeLoop loop, std::optional<int> frames)
{
    if (key < 1 || key > static_cast<int>(keys_.size()))
    {
        return MixResult::BadMe;
    }
    if (frames && (*frames != 0 || loop != DmeLoop::Off))
    {
        // Only frames 0 (a jump), and not with a loop.
        return MixResult::BadFrames;
    }
    {
        std::lock_guard lock{mu_};
        auto& keyer = keys_[static_cast<std::size_t>(key - 1)];
        if (!keyer.upstream || !(keyer.settings.pip || keyer.settings.dme.enabled))
        {
            return MixResult::BadDme;
        }
        int const n = static_cast<int>(keyer.keyframes.size());
        int const j = to.value_or(reverse ? 0 : n - 1);
        if (n == 0 || j < 0 || j >= n || (loop != DmeLoop::Off && n < 2))
        {
            return MixResult::BadKeyframes;
        }
        stopRun(keyer);
        if (frames)
        {
            jumpTo(keyer, j);
        }
        else
        {
            startRun(keyer, j, loop, reverse);
        }
    }
    notify();
    return MixResult::Ok;
}

MixResult Mixer::keyStop(int key)
{
    if (key < 1 || key > static_cast<int>(keys_.size()))
    {
        return MixResult::BadMe;
    }
    {
        std::lock_guard lock{mu_};
        stopRun(keys_[static_cast<std::size_t>(key - 1)]);
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
        auto& keyer = keys_[static_cast<std::size_t>(key - 1)];
        // Out of a tied transition where it is (a tied fly turns back as its own does).
        bool const tiedFly = keyer.tieMe > 0 && keyer.tieFly && !keyer.tieAtCut;
        untie(keyer);
        if (canFly(keyer))
        {
            // A fly: the keyer is on (gain 1) while its box runs. Auto on starts at keyframe 0
            // (the off state) unless it turns back a fly off; every fly runs from where the box
            // is.
            bool const turningBack = (keyer.flying && (keyer.runPending || keyer.runActive)) || tiedFly;
            keyer.on = !keyer.on;
            keyer.pending = false;
            keyer.running = false;
            keyer.gain = 1.f;
            if (keyer.on && !turningBack)
            {
                jumpTo(keyer, 0);
            }
            startRun(keyer, keyer.on ? static_cast<int>(keyer.keyframes.size()) - 1 : 0, DmeLoop::Off, false);
            keyer.flying = true;
        }
        else
        {
            fadeAuto(keyer, frames);
        }
    }
    notify();
    return MixResult::Ok;
}

FramePlan Mixer::frame(std::uint64_t index, std::vector<bool> const& stingerReady)
{
    runHeld(index, false, std::nullopt);
    FramePlan out;
    bool ended = false;
    std::unique_lock lock{mu_};
    std::vector<TieStep> ties(static_cast<std::size_t>(mes_));
    for (int m = mes_; m >= 1; --m)
    {
        auto& state = state_[static_cast<std::size_t>(m - 1)];
        auto& tie = ties[static_cast<std::size_t>(m - 1)];
        if (state.pending && state.sting.player > 0)
        {
            // A stinger waits for its first frame (counted), at most kStingerWait grains; then
            // it is a cut.
            auto const p = static_cast<std::size_t>(state.sting.player - 1);
            if (!state.waiting)
            {
                state.waiting = true;
                state.start = index;
            }
            if (p < stingerReady.size() && stingerReady[p])
            {
                state.waiting = false;
            }
            else if (index - state.start < static_cast<std::uint64_t>(kStingerWait))
            {
                ++stingerWaits_;
            }
            else
            {
                ++stingerCuts_;
                finishMix(state);
                tie.cut = true;
                ended = true;
            }
        }
        if (state.pending && !state.waiting)
        {
            state.pending = false;
            state.mixing = true;
            state.start = index;
            if (state.sting.player > 0)
            {
                ++stingersPlayed_;
            }
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
            tie.active = true;
            tie.t = state.t;
            if (state.t >= 1.f)
            {
                finishMix(state);
                tie.end = true;
                ended = true;
            }
            else if (state.t <= 0.f)
            {
                state.mixing = false;
                state.manual = false;
                state.wiping = false;
                state.after.reset();
                tie.active = false;
                tie.back = true;
                ended = true;
            }
        }
        else if (state.mixing && state.sting.player > 0)
        {
            // A stinger: its frame f in grain start + f, the background b from the cut point, the
            // buses swapped after its last frame.
            int const f = static_cast<int>(std::min<std::uint64_t>(index >= state.start ? index - state.start : 0,
                static_cast<std::uint64_t>(state.frames - 1)));
            plan.stinger = state.sting.player;
            plan.stingerFrame = f;
            plan.cutDone = f >= state.sting.cut;
            plan.tPrev = stingerFadeT(f - 1, state.sting.cut, state.frames);
            plan.t = stingerFadeT(f, state.sting.cut, state.frames);
            state.position = f + 1;
            tie.cut = plan.cutDone;
            if (f >= state.frames - 1)
            {
                finishMix(state);
                tie.end = true;
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
            tie.active = true;
            tie.t = plan.t;
            if (step >= state.frames)
            {
                // The last grain shows the target in full; from the next one it is Program.
                finishMix(state);
                tie.end = true;
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
        // A tied keyer: with its ME's transition (cut before this grain, follow its t, back where
        // it was, or end after this grain).
        bool tieEnded = false;
        if (keyer.tieMe > 0)
        {
            auto const& tie = ties[static_cast<std::size_t>(keyer.tieMe - 1)];
            if (keyer.tieAtCut)
            {
                if (tie.cut)
                {
                    cutKey(keyer, !keyer.on);
                    ended = true;
                }
                tieEnded = tie.end && !tie.cut;
            }
            else if (tie.back)
            {
                tieEnd(keyer, false);
                ended = true;
            }
            else if (tie.active)
            {
                tieAdvance(keyer, tie.t);
                tieEnded = tie.end;
            }
        }
        ended = fadeAdvance(keyer, index) || ended;
        bool const runEnded = runAdvance(keyer, index);
        out.keys.push_back(KeyPlan{key++, keyer.me, keyer.upstream, keyer.gain, keyer.settings});
        if (tieEnded)
        {
            tieEnd(keyer, true);
            ended = true;
        }
        if (runEnded)
        {
            // A fly off ends: this grain showed keyframe 0, from the next one the keyer is off.
            if (keyer.flying && !keyer.on)
            {
                keyer.gain = 0.f;
            }
            keyer.flying = false;
            ended = true;
        }
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
        view.stinger = view.mixing ? state.sting.player : 0;
        view.start = state.start;
        for (std::size_t k = 0; k < keys_.size(); ++k)
        {
            if (keys_[k].tieMe == m)
            {
                view.tied.push_back(static_cast<int>(k) + 1);
            }
        }
        view.next = state.next;
        if (auto const& held = deferred_[static_cast<std::size_t>(m - 1)])
        {
            view.deferred = held->at;
        }
        view.deferredMade = state.heldMade;
        views.push_back(view);
    }
    return views;
}

std::uint64_t Mixer::stingerWaits() const
{
    std::lock_guard lock{mu_};
    return stingerWaits_;
}

std::uint64_t Mixer::stingerCuts() const
{
    std::lock_guard lock{mu_};
    return stingerCuts_;
}

std::uint64_t Mixer::stingersPlayed() const
{
    std::lock_guard lock{mu_};
    return stingersPlayed_;
}

std::vector<KeyView> Mixer::keyView() const
{
    std::vector<KeyView> views;
    std::lock_guard lock{mu_};
    int key = 1;
    for (auto const& keyer : keys_)
    {
        bool const running = keyer.runPending || keyer.runActive;
        KeyRunView run{running, running && keyer.flying, keyer.run.loop, keyer.runStart, keyer.runStep, keyer.keyframe, keyer.onPath, keyer.u};
        if (!running)
        {
            run.loop = DmeLoop::Off;
            run.keyframe = dmeKeyframeOf(keyer.keyframes, keyer.settings.dme);
        }
        views.push_back(KeyView{key++, keyer.me, keyer.upstream, keyer.on, keyer.pending || keyer.running || run.fly || keyer.tieMe > 0,
            keyer.tieMe, keyer.gain, keyer.settings, keyer.keyframes, run});
    }
    return views;
}
} // namespace fxeng
