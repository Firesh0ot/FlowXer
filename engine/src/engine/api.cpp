#include "engine/api.hpp"

#include "engine/engine.hpp"

#include "core/pixel.hpp"

#include <nlohmann/json.hpp>

#include <cstdio>
#include <optional>
#include <utility>
#include <vector>

namespace fxeng
{
namespace
{
using json = nlohmann::json;

HttpResponse reply(int status, json const& body)
{
    return HttpResponse{status, "application/json", body.dump()};
}

HttpResponse error(int status, std::string const& message)
{
    return reply(status, json{{"error", message}});
}

// "/<prefix>/<number>/<rest>": the number (1-99) and the rest ("" when there is none), or 0.
int pathNumber(std::string const& path, std::string const& prefix, std::string& rest)
{
    if (path.rfind(prefix, 0) != 0)
    {
        return 0;
    }
    auto const tail = path.substr(prefix.size());
    auto const slash = tail.find('/');
    auto const number = tail.substr(0, slash);
    rest = slash == std::string::npos ? std::string{} : tail.substr(slash + 1);
    if (number.empty() || number.size() > 2 || number.find_first_not_of("0123456789") != std::string::npos)
    {
        return 0;
    }
    return std::stoi(number);
}

std::string text(json const& body, char const* key)
{
    auto const it = body.find(key);
    return it != body.end() && it->is_string() ? it->get<std::string>() : std::string{};
}

Route routeOf(json const& body)
{
    return Route{text(body, "domain"), text(body, "flow")};
}

// "#RRGGBB" as 10-bit BT.709 limited-range Y'CbCr.
bool parseColour(std::string const& text, std::uint16_t& y, std::uint16_t& cb, std::uint16_t& cr)
{
    unsigned r = 0;
    unsigned g = 0;
    unsigned b = 0;
    if (text.size() != 7 || text[0] != '#' || text.find_first_not_of("0123456789abcdefABCDEF", 1) != std::string::npos ||
        std::sscanf(text.c_str() + 1, "%2x%2x%2x", &r, &g, &b) != 3)
    {
        return false;
    }
    rgbToYcbcr(Rgb{static_cast<float>(r) / 255.f, static_cast<float>(g) / 255.f, static_cast<float>(b) / 255.f}, y, cb, cr);
    return true;
}

// A keyer's box (docs/engine-p4.md §3): {enabled, x, y, size, soft, crop {left, right, top,
// bottom}, border {width, soft, color, opacity}, shadow {dx, dy, blur, opacity}}; missing fields
// keep their defaults. An error text, or empty.
std::string parseDme(json const& body, DmeSettings& dme)
{
    try
    {
        dme.enabled = body.value("enabled", dme.enabled);
        dme.x = body.value("x", dme.x);
        dme.y = body.value("y", dme.y);
        dme.size = body.value("size", dme.size);
        dme.soft = body.value("soft", dme.soft);
        if (auto const it = body.find("crop"); it != body.end())
        {
            dme.cropLeft = it->value("left", dme.cropLeft);
            dme.cropRight = it->value("right", dme.cropRight);
            dme.cropTop = it->value("top", dme.cropTop);
            dme.cropBottom = it->value("bottom", dme.cropBottom);
        }
        if (auto const it = body.find("border"); it != body.end())
        {
            dme.borderWidth = it->value("width", dme.borderWidth);
            dme.borderSoft = it->value("soft", dme.borderSoft);
            dme.borderOpacity = it->value("opacity", dme.borderOpacity);
            if (it->contains("color") && !parseColour(text(*it, "color"), dme.borderY, dme.borderCb, dme.borderCr))
            {
                return "dme border color must be #RRGGBB";
            }
        }
        if (auto const it = body.find("shadow"); it != body.end())
        {
            dme.shadowDx = it->value("dx", dme.shadowDx);
            dme.shadowDy = it->value("dy", dme.shadowDy);
            dme.shadowBlur = it->value("blur", dme.shadowBlur);
            dme.shadowOpacity = it->value("opacity", dme.shadowOpacity);
        }
    }
    catch (json::exception const&)
    {
        return "dme fields must be numbers (enabled a boolean)";
    }
    return {};
}

json dmeJson(DmeSettings const& d)
{
    return json{{"enabled", d.enabled}, {"x", d.x}, {"y", d.y}, {"size", d.size}, {"soft", d.soft},
        {"crop", {{"left", d.cropLeft}, {"right", d.cropRight}, {"top", d.cropTop}, {"bottom", d.cropBottom}}},
        {"border", {{"width", d.borderWidth}, {"soft", d.borderSoft}, {"opacity", d.borderOpacity}, {"ycbcr", {d.borderY, d.borderCb, d.borderCr}}}},
        {"shadow", {{"dx", d.shadowDx}, {"dy", d.shadowDy}, {"blur", d.shadowBlur}, {"opacity", d.shadowOpacity}}}};
}

json transitionJson(TransitionSettings const& next)
{
    auto const& w = next.wipe;
    return json{{"type", transitionName(next.type)}, {"frames", next.frames}, {"pattern", w.pattern}, {"reverse", w.reverse}, {"soft", w.soft},
        {"border", w.border}, {"border_ycbcr", {w.borderY, w.borderCb, w.borderCr}}, {"ties", next.ties}};
}

json meView(Engine& engine, int me)
{
    for (auto const& view : engine.mixer().view())
    {
        if (view.me == me)
        {
            return json{{"me", view.me}, {"program", sourceName(view.program)}, {"preview", sourceName(view.preview)}, {"mixing", view.mixing},
                {"wiping", view.wiping}, {"target", sourceName(view.target)}, {"frames", view.frames}, {"manual", view.manual}, {"t", view.t},
                {"stinger", view.stinger}, {"start", view.start}, {"tied", view.tied},
                {"deferred_at", view.deferred ? json(*view.deferred) : json(nullptr)}, {"transition", transitionJson(view.next)}};
        }
    }
    return json::object();
}

std::optional<KeyView> keyerView(Engine& engine, int k)
{
    for (auto const& key : engine.mixer().keyView())
    {
        if (key.key == k)
        {
            return key;
        }
    }
    return std::nullopt;
}

// A keyframe: the box fields over `box` (the keyer's box: missing fields keep its values), its
// frames and ease. An error text, or empty.
std::string parseKeyframe(json const& body, DmeSettings const& box, DmeKeyframe& keyframe)
{
    if (!body.is_object())
    {
        return "each keyframe must be an object";
    }
    keyframe.box = box;
    if (auto const failed = parseDme(body, keyframe.box); !failed.empty())
    {
        return failed;
    }
    if (body.contains("frames"))
    {
        if (!body["frames"].is_number_integer())
        {
            return "a keyframe's frames must be an integer";
        }
        keyframe.frames = body["frames"].get<int>();
    }
    if (body.contains("ease"))
    {
        auto const ease = parseEase(text(body, "ease"));
        if (!ease)
        {
            return "ease must be linear, ease, ease-in, ease-out or hold";
        }
        keyframe.ease = *ease;
    }
    return {};
}

HttpResponse mixResult(Engine& engine, MixResult result, int me, std::string const& what)
{
    switch (result)
    {
    case MixResult::Ok:
        return reply(200, meView(engine, me));
    case MixResult::BadMe:
        return error(404, "no " + what + " " + std::to_string(me));
    case MixResult::BadSource:
        return error(400, "ME " + std::to_string(me) + " takes the inputs, the clip players, black and the Program of a higher ME (re-entry n > m)");
    case MixResult::BadFrames:
        return error(400, "frames must be 1.." + std::to_string(Mixer::kMaxFrames));
    case MixResult::BadPattern:
        return error(400, "pattern must be a wipe code of P3, soft and border 0..1");
    case MixResult::BadPosition:
        return error(400, "t must be 0..1");
    case MixResult::BadKey:
        return error(400, "clip, soft and gain must be 0..1");
    case MixResult::BadDme:
        return error(400, "pip and dme only on upstream keyers, dme within its ranges (docs/engine-p4.md §3); a run needs a box");
    case MixResult::BadKeyframes:
        return error(400, "at most 8 keyframes; a run needs its keyframe (a loop 2 keyframes)");
    case MixResult::BadTies:
        return error(400, "ties must be upstream keyers of ME " + std::to_string(me) + ", each once");
    case MixResult::Busy:
        return error(409, "a transition is running on ME " + std::to_string(me));
    }
    return error(500, "unexpected");
}

std::optional<int> frames(json const& body, int fallback)
{
    if (!body.contains("frames"))
    {
        return fallback;
    }
    if (!body["frames"].is_number_integer())
    {
        return std::nullopt;
    }
    return body["frames"].get<int>();
}

HttpResponse meCommand(Engine& engine, int me, std::string const& action, json const& body)
{
    auto& mixer = engine.mixer();
    auto const source = [&](char const* key) { return parseSource(text(body, key)); };
    if (action == "preview" || action == "program")
    {
        auto const s = source("source");
        if (!s)
        {
            return error(400, "source must be in<n>, cp<n>, black or me<n>");
        }
        return mixResult(engine, action == "preview" ? mixer.setPreview(me, *s) : mixer.setProgram(me, *s), me, "ME");
    }
    if (action == "transition")
    {
        TransitionSettings next;
        for (auto const& view : mixer.view())
        {
            if (view.me == me)
            {
                next = view.next;
            }
        }
        auto const type = body.contains("type") ? parseTransition(text(body, "type")) : next.type;
        auto const n = frames(body, next.frames);
        if (!type || *type == Transition::Cut || !n)
        {
            return error(400, "type must be mix, wipe or stinger and frames an integer");
        }
        next.type = *type;
        next.frames = *n;
        auto& wipe = next.wipe;
        try
        {
            wipe.pattern = body.value("pattern", wipe.pattern);
            wipe.reverse = body.value("reverse", wipe.reverse);
            wipe.soft = body.value("soft", wipe.soft);
            wipe.border = body.value("border", wipe.border);
        }
        catch (json::exception const&)
        {
            return error(400, "pattern must be an integer, reverse a boolean, soft and border numbers");
        }
        if (body.contains("border_color") && !parseColour(text(body, "border_color"), wipe.borderY, wipe.borderCb, wipe.borderCr))
        {
            return error(400, "border_color must be #RRGGBB");
        }
        if (auto const it = body.find("ties"); it != body.end())
        {
            if (!it->is_array())
            {
                return error(400, "ties must be an array of keyer numbers");
            }
            next.ties.clear();
            for (auto const& k : *it)
            {
                if (!k.is_number_integer())
                {
                    return error(400, "ties must be an array of keyer numbers");
                }
                next.ties.push_back(k.get<int>());
            }
        }
        return mixResult(engine, mixer.setTransition(me, next), me, "ME");
    }
    if (action == "take")
    {
        auto const s = source("source");
        auto const n = frames(body, 0);
        // P2's take with frames > 0 is a mix.
        auto const type = body.contains("type") ? parseTransition(text(body, "type")) : Transition::Mix;
        if (!type)
        {
            return error(400, "type must be cut, mix, wipe or stinger");
        }
        std::optional<Source> preview;
        if (body.contains("preview") && !body["preview"].is_null())
        {
            preview = source("preview");
            if (!preview)
            {
                return error(400, "preview must be in<n>, cp<n>, black or me<n>");
            }
        }
        if (!s || !n)
        {
            return error(400, "take needs a source (in<n>, cp<n>, black, me<n>) and integer frames");
        }
        if (body.contains("ties") && !body["ties"].is_boolean())
        {
            return error(400, "ties must be true or false");
        }
        // A stinger: its player's loaded stinger and cut frame (a player without one cuts).
        Sting sting;
        if (*type == Transition::Stinger && body.contains("stinger"))
        {
            if (!body["stinger"].is_number_integer() || (body.contains("cut") && !body["cut"].is_number_integer()))
            {
                return error(400, "stinger (the player) and cut must be integers");
            }
            sting.player = body["stinger"].get<int>();
            sting.cut = body.value("cut", 0);
            sting.frames = static_cast<int>(engine.stingerFrames(sting.player));
            if (sting.frames < 0)
            {
                return error(404, "no stinger player " + std::to_string(sting.player));
            }
        }
        if (auto const it = body.find("at"); it != body.end() && !it->is_null())
        {
            // Held until that output grain (docs/clip-end.md).
            if (!it->is_number_unsigned())
            {
                return error(400, "at must be an output grain index");
            }
            return mixResult(engine, mixer.takeAt(it->get<std::uint64_t>(), me, *s, *n, preview, *type, sting, body.value("ties", false)), me, "ME");
        }
        return mixResult(engine, mixer.take(me, *s, *n, preview, *type, sting, body.value("ties", false)), me, "ME");
    }
    if (action == "cut")
    {
        return mixResult(engine, mixer.cut(me), me, "ME");
    }
    if (action == "auto")
    {
        if (body.contains("frames") && !body["frames"].is_number_integer())
        {
            return error(400, "frames must be an integer");
        }
        auto const n = body.contains("frames") ? std::optional<int>(body["frames"].get<int>()) : std::nullopt;
        return mixResult(engine, mixer.autoTransition(me, n), me, "ME");
    }
    if (action == "ftb" || action == "ftb/auto")
    {
        MixResult result = MixResult::Ok;
        if (action == "ftb")
        {
            if (!body.contains("on") || !body["on"].is_boolean())
            {
                return error(400, "on must be true or false");
            }
            result = mixer.ftbSet(me, body["on"].get<bool>());
        }
        else
        {
            auto const n = frames(body, 25);
            if (!n)
            {
                return error(400, "frames must be an integer");
            }
            result = mixer.ftbAuto(me, *n);
        }
        return result == MixResult::Ok ? reply(200, json{{"me", me}, {"ftb", mixer.ftbGain(me)}}) : mixResult(engine, result, me, "ME");
    }
    if (action == "position")
    {
        if (!body.contains("t") || !body["t"].is_number())
        {
            return error(400, "t must be a number 0..1");
        }
        return mixResult(engine, mixer.position(me, body["t"].get<float>()), me, "ME");
    }
    return error(404, "unknown action " + action);
}

// ME m's audio settings as the status shows them (docs/engine-p5.md §8).
json meAudioJson(Engine& engine, int me)
{
    auto const all = json::parse(engine.audioJson(), nullptr, false);
    if (all.is_discarded() || !all.contains("mes"))
    {
        return json::object();
    }
    for (auto const& item : all["mes"])
    {
        if (item.value("me", 0) == me)
        {
            return item;
        }
    }
    return json::object();
}

// POST /me/{m}/audio {master_db, strips: {"in1": {fader_db, mute, afv}, …}}: any subset, all or
// nothing; each change ramps over the next grain.
HttpResponse meAudio(Engine& engine, int me, json const& body)
{
    auto& mix = engine.audioMix();
    if (me < 1 || me > mix.mes())
    {
        return error(404, "no ME " + std::to_string(me));
    }
    auto const level = [](json const& value) { return value.is_number() && value.get<double>() >= kFaderOffDb && value.get<double>() <= kFaderMaxDb; };
    std::optional<float> master;
    if (body.contains("master_db"))
    {
        if (!level(body["master_db"]))
        {
            return error(400, "master_db must be a number -90..10");
        }
        master = body["master_db"].get<float>();
    }
    std::vector<std::pair<StripRef, StripSettings>> changes;
    if (body.contains("strips"))
    {
        if (!body["strips"].is_object())
        {
            return error(400, "strips must be an object of strip ids (in<k>, cp<k>, me<n>, stinger)");
        }
        for (auto const& [name, item] : body["strips"].items())
        {
            auto const ref = parseStrip(name);
            if (!ref || !mix.hasStrip(me, *ref))
            {
                return error(404, "ME " + std::to_string(me) + " has no strip " + name);
            }
            if (!item.is_object())
            {
                return error(400, "strip " + name + " must be an object {fader_db, mute, afv}");
            }
            auto set = mix.strip(me, *ref).value_or(StripSettings{});
            if (item.contains("fader_db"))
            {
                if (!level(item["fader_db"]))
                {
                    return error(400, "fader_db must be a number -90..10");
                }
                set.faderDb = item["fader_db"].get<float>();
            }
            if (item.contains("mute"))
            {
                if (!item["mute"].is_boolean())
                {
                    return error(400, "mute must be true or false");
                }
                set.mute = item["mute"].get<bool>();
            }
            if (item.contains("afv"))
            {
                if (ref->kind == StripRef::Kind::Stinger)
                {
                    return error(400, "the stinger strip has no afv");
                }
                if (!item["afv"].is_boolean())
                {
                    return error(400, "afv must be true or false");
                }
                set.afv = item["afv"].get<bool>();
            }
            changes.emplace_back(*ref, set);
        }
    }
    if (master)
    {
        mix.setMaster(me, *master);
    }
    for (auto const& [ref, set] : changes)
    {
        mix.setStrip(me, ref, set);
    }
    return reply(200, meAudioJson(engine, me));
}

// POST /input/{k}/audio {delay_ms, map}: the delay (ms, whole samples) and channel map (one entry
// per PGM channel, 0 silent; [] the default 1:1).
HttpResponse inputAudio(Engine& engine, int k, json const& body)
{
    std::optional<double> delay;
    std::optional<std::vector<int>> map;
    if (body.contains("delay_ms"))
    {
        if (!body["delay_ms"].is_number())
        {
            return error(400, "delay_ms must be a number");
        }
        delay = body["delay_ms"].get<double>();
    }
    if (body.contains("map"))
    {
        if (!body["map"].is_array())
        {
            return error(400, "map must be an array of input channels (1-based, 0 silent)");
        }
        map.emplace();
        for (auto const& v : body["map"])
        {
            if (!v.is_number_integer())
            {
                return error(400, "map must be an array of input channels (1-based, 0 silent)");
            }
            map->push_back(v.get<int>());
        }
    }
    auto const failed = engine.setInputAudio(k, delay, map);
    if (!failed.empty())
    {
        return error(failed.rfind("no input", 0) == 0 ? 404 : 400, failed);
    }
    auto const all = json::parse(engine.audioJson(), nullptr, false);
    if (!all.is_discarded() && all.contains("inputs") && k <= static_cast<int>(all["inputs"].size()))
    {
        return reply(200, all["inputs"][static_cast<std::size_t>(k - 1)]);
    }
    return reply(200, json{{"input", k}});
}

HttpResponse playerCommand(Engine& engine, int k, std::string const& action, json const& body)
{
    PlayerCommand command;
    if (action == "load")
    {
        command.action = PlayerCommand::Action::Load;
        command.path = text(body, "path");
        command.kind = text(body, "kind");
        command.audio = text(body, "audio");
        command.channels = body.contains("channels") && body["channels"].is_number_integer() ? body["channels"].get<int>() : 0;
        command.frames = body.contains("frames") && body["frames"].is_number_integer() ? body["frames"].get<long long>() : 0;
        command.loop = body.value("loop", false);
        command.playOnCut = body.value("play_on_cut", false);
    }
    else if (action == "cue")
    {
        command.action = PlayerCommand::Action::Cue;
        command.frame = body.contains("frame") && body["frame"].is_number_integer() ? body["frame"].get<long long>() : 0;
    }
    else if (action == "play")
    {
        command.action = PlayerCommand::Action::Play;
    }
    else if (action == "pause")
    {
        command.action = PlayerCommand::Action::Pause;
    }
    else if (action == "stop")
    {
        command.action = PlayerCommand::Action::Stop;
    }
    else
    {
        return error(404, "unknown player action " + action);
    }
    auto const failed = engine.playerCommand(k, command);
    if (!failed.empty())
    {
        return error(failed.rfind("no clip player", 0) == 0 ? 404 : 409, failed);
    }
    return reply(200, json{{"player", k}, {"action", action}});
}
} // namespace

json keyerJson(KeyView const& key)
{
    auto const& run = key.run;
    return json{{"key", key.key}, {"me", key.me}, {"stage", key.upstream ? "upstream" : "downstream"}, {"on", key.on}, {"transition", key.transition},
        {"tie", key.tie > 0 ? json(key.tie) : json(nullptr)}, {"gain", key.gain}, {"mode", key.settings.pip ? "pip" : key.settings.luma ? "luma" : "linear"}, {"fly", key.settings.fly},
        {"dme", dmeJson(key.settings.dme)}, {"keyframes", key.keyframes.size()},
        {"run", {{"running", run.running}, {"fly", run.fly}, {"loop", loopName(run.loop)}, {"start", run.start}, {"step", run.step},
                    {"keyframe", run.keyframe >= 0 ? json(run.keyframe) : json(nullptr)}, {"on_path", run.onPath}, {"u", run.u}}}};
}

HttpResponse handleRequest(Engine& engine, HttpRequest const& request)
{
    if (request.method == "GET")
    {
        if (request.path == "/status")
        {
            return HttpResponse{200, "application/json", engine.statusJson()};
        }
        if (request.path == "/metrics")
        {
            return HttpResponse{200, "text/plain; version=0.0.4", engine.metricsText()};
        }
        if (request.path == "/players")
        {
            return HttpResponse{200, "application/json", engine.playersJson()};
        }
        if (request.path == "/mosaic/map")
        {
            return HttpResponse{200, "application/json", engine.mosaicMapJson()};
        }
        if (request.path == "/audio")
        {
            return HttpResponse{200, "application/json", engine.audioJson()};
        }
        if (request.path == "/audio/meters")
        {
            return HttpResponse{200, "application/json", engine.metersJson()};
        }
        if (request.path == "/livez")
        {
            return HttpResponse{200, "text/plain", "ok\n"};
        }
        if (request.path.rfind("/grab/", 0) == 0)
        {
            auto const digits = request.path.substr(6);
            if (!digits.empty() && digits.size() <= 9 && digits.find_first_not_of("0123456789") == std::string::npos)
            {
                auto const grab = engine.grabJson(std::stoi(digits));
                if (!grab.empty())
                {
                    return HttpResponse{200, "application/json", grab};
                }
            }
            return error(404, "no grab " + digits);
        }
        return error(404, "not found");
    }
    if (request.method != "POST" && request.method != "PUT")
    {
        return error(405, "method not allowed");
    }
    json body = request.body.empty() ? json::object() : json::parse(request.body, nullptr, false);
    if (body.is_discarded() || !body.is_object())
    {
        return error(400, "the body must be a JSON object");
    }
    if (request.path == "/grab")
    {
        // A frame grab of the next output grain (docs/engine-p4.md §6): queued here, copied by
        // the writer thread with that grain's downloads, written by the grab thread.
        auto const bus = body.contains("bus") ? text(body, "bus") : std::string("pgm");
        if (bus != "pgm" && bus != "pvw")
        {
            return error(400, "bus must be pgm or pvw");
        }
        std::string failed;
        int const id = engine.grab(text(body, "source"), bus == "pvw", text(body, "path"), failed);
        if (id == 0)
        {
            return error(failed.rfind("too many", 0) == 0 ? 409 : 400, failed);
        }
        return HttpResponse{200, "application/json", engine.grabJson(id)};
    }
    std::string rest;
    if (int const me = pathNumber(request.path, "/me/", rest); me > 0)
    {
        if (rest == "audio")
        {
            return meAudio(engine, me, body);
        }
        if (rest == "audio/loudness/reset")
        {
            return engine.resetLoudness(me) ? reply(200, json{{"me", me}, {"loudness_reset", true}}) : error(404, "no ME " + std::to_string(me));
        }
        // A held take of this ME (a clip player's end action) is made before any other command
        // of the ME, so the commands keep the control plane's order.
        if (!(rest == "take" && body.contains("at") && !body["at"].is_null()))
        {
            engine.mixer().runDeferred(me);
        }
        return meCommand(engine, me, rest, body);
    }
    if (int const k = pathNumber(request.path, "/input/", rest); k > 0 && rest == "audio")
    {
        return inputAudio(engine, k, body);
    }
    if (int const k = pathNumber(request.path, "/input/", rest); k > 0 && rest.empty())
    {
        auto const video = body.contains("video") && body["video"].is_object() ? routeOf(body["video"]) : Route{};
        auto const audio = body.contains("audio") && body["audio"].is_object() ? routeOf(body["audio"]) : Route{};
        if ((!video.flow.empty() && video.domain.empty()) || (!audio.flow.empty() && audio.domain.empty()))
        {
            return error(400, "a routed flow needs its domain directory");
        }
        return engine.routeInput(k, video, audio) ? reply(200, json{{"input", k}}) : error(404, "no input " + std::to_string(k));
    }
    if (int const k = pathNumber(request.path, "/key/", rest); k > 0 && rest.empty())
    {
        auto const route = routeOf(body);
        if (!route.flow.empty() && route.domain.empty())
        {
            return error(400, "a routed flow needs its domain directory");
        }
        return engine.routeKey(k, route) ? reply(200, json{{"key", k}}) : error(404, "no key input " + std::to_string(k));
    }
    if (int const k = pathNumber(request.path, "/player/", rest); k > 0)
    {
        return playerCommand(engine, k, rest, body);
    }
    if (int const k = pathNumber(request.path, "/stinger/", rest); k > 0 && rest == "load")
    {
        // A stinger onto stinger player k (path "" unloads): cued on its first frame, played by
        // a take with type stinger.
        PlayerCommand command;
        command.action = PlayerCommand::Action::Load;
        command.kind = "stinger";
        command.path = text(body, "path");
        command.alpha = text(body, "alpha");
        command.audio = text(body, "audio");
        command.channels = body.contains("channels") && body["channels"].is_number_integer() ? body["channels"].get<int>() : 0;
        command.frames = body.contains("frames") && body["frames"].is_number_integer() ? body["frames"].get<long long>() : 0;
        command.playOnCut = true;
        auto const failed = engine.stingerCommand(k, command);
        if (!failed.empty())
        {
            return error(failed.rfind("no stinger player", 0) == 0 ? 404 : 409, failed);
        }
        return reply(200, json{{"stinger", k}, {"frames", engine.stingerFrames(k)}});
    }
    if (int const k = pathNumber(request.path, "/keyer/", rest); k > 0)
    {
        auto& mixer = engine.mixer();
        // A held take may tie this keyer: it is made first.
        mixer.runDeferred();
        MixResult result = MixResult::Ok;
        if (rest.empty())
        {
            if (!body.contains("on") || !body["on"].is_boolean())
            {
                return error(400, "on must be true or false");
            }
            result = mixer.keySet(k, body["on"].get<bool>());
        }
        else if (rest == "auto")
        {
            auto const n = frames(body, 25);
            if (!n)
            {
                return error(400, "frames must be an integer");
            }
            result = mixer.keyAuto(k, *n);
        }
        else if (rest == "settings")
        {
            // Mode, source, clip, soft, gain, invert and fly as given (defaults when missing); a
            // `dme` changes the given fields of the keyer's box (without one the box stays).
            KeySettings set;
            auto const mode = text(body, "mode");
            if (mode != "linear" && mode != "luma" && mode != "pip")
            {
                return error(400, "mode must be linear, luma or pip");
            }
            set.luma = mode == "luma";
            set.pip = mode == "pip";
            if (set.luma || set.pip)
            {
                auto const source = parseSource(text(body, "source"));
                if (!source)
                {
                    return error(400, "a luma key or a pip needs a source (in<n>, cp<n>, black, me<n>)");
                }
                set.source = *source;
            }
            json patch = json::object();
            if (auto const it = body.find("dme"); it != body.end())
            {
                DmeSettings probe;
                if (!it->is_object())
                {
                    return error(400, "dme must be an object");
                }
                if (auto const failed = parseDme(*it, probe); !failed.empty())
                {
                    return error(400, failed);
                }
                patch = *it;
            }
            try
            {
                set.clip = body.value("clip", set.clip);
                set.soft = body.value("soft", set.soft);
                set.gain = body.value("gain", set.gain);
                set.invert = body.value("invert", set.invert);
                set.fly = body.value("fly", set.fly);
            }
            catch (json::exception const&)
            {
                return error(400, "clip, soft and gain must be numbers, invert and fly booleans");
            }
            result = mixer.keySettings(k, set, [&patch](DmeSettings& box) { parseDme(patch, box); });
            if (result == MixResult::Ok)
            {
                // The box buffer, on this thread when the box is first enabled.
                engine.prepareBox(k);
            }
        }
        else if (rest == "keyframes")
        {
            auto const key = keyerView(engine, k);
            auto const it = body.find("keyframes");
            if (!key)
            {
                return error(404, "no keyer " + std::to_string(k));
            }
            if (it == body.end() || !it->is_array())
            {
                return error(400, "keyframes must be an array");
            }
            std::vector<DmeKeyframe> keyframes;
            for (auto const& item : *it)
            {
                DmeKeyframe keyframe;
                if (auto const failed = parseKeyframe(item, key->settings.dme, keyframe); !failed.empty())
                {
                    return error(400, failed);
                }
                keyframes.push_back(keyframe);
            }
            result = mixer.keyKeyframes(k, keyframes);
            if (result == MixResult::BadFrames)
            {
                return error(400, "a keyframe's frames must be 1.." + std::to_string(Mixer::kMaxFrames));
            }
        }
        else if (rest == "run")
        {
            if ((body.contains("to") && !body["to"].is_number_integer()) || (body.contains("frames") && !body["frames"].is_number_integer()) ||
                (body.contains("reverse") && !body["reverse"].is_boolean()))
            {
                return error(400, "to and frames must be integers, reverse a boolean");
            }
            auto const loop = body.contains("loop") ? parseLoop(text(body, "loop")) : DmeLoop::Off;
            if (!loop)
            {
                return error(400, "loop must be off, wrap or pingpong");
            }
            auto const to = body.contains("to") ? std::optional<int>(body["to"].get<int>()) : std::nullopt;
            auto const n = body.contains("frames") ? std::optional<int>(body["frames"].get<int>()) : std::nullopt;
            if (n && (*n != 0 || *loop != DmeLoop::Off))
            {
                return error(400, "frames must be 0 (a jump to the keyframe), without a loop");
            }
            result = mixer.keyRun(k, to, body.value("reverse", false), *loop, n);
        }
        else if (rest == "stop")
        {
            result = mixer.keyStop(k);
        }
        else
        {
            return error(404, "unknown keyer action " + rest);
        }
        if (result != MixResult::Ok)
        {
            return mixResult(engine, result, k, "keyer");
        }
        auto const key = keyerView(engine, k);
        return reply(200, key ? keyerJson(*key) : json{{"key", k}});
    }
    return error(404, "not found");
}
} // namespace fxeng
