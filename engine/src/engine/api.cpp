#include "engine/api.hpp"

#include "engine/engine.hpp"

#include <nlohmann/json.hpp>

#include <optional>

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

json meView(Engine& engine, int me)
{
    for (auto const& view : engine.mixer().view())
    {
        if (view.me == me)
        {
            return json{{"me", view.me}, {"program", sourceName(view.program)}, {"preview", sourceName(view.preview)}, {"mixing", view.mixing},
                {"target", sourceName(view.target)}, {"frames", view.frames}};
        }
    }
    return json::object();
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
    case MixResult::Busy:
        return error(409, "a Mix is running on ME " + std::to_string(me));
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
    if (action == "take")
    {
        auto const s = source("source");
        auto const n = frames(body, 0);
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
        return mixResult(engine, mixer.take(me, *s, *n, preview), me, "ME");
    }
    if (action == "cut")
    {
        return mixResult(engine, mixer.cut(me), me, "ME");
    }
    if (action == "auto")
    {
        auto const n = frames(body, 25);
        if (!n)
        {
            return error(400, "frames must be an integer");
        }
        return mixResult(engine, mixer.autoMix(me, *n), me, "ME");
    }
    return error(404, "unknown action " + action);
}

HttpResponse playerCommand(Engine& engine, int k, std::string const& action, json const& body)
{
    PlayerCommand command;
    if (action == "load")
    {
        command.action = PlayerCommand::Action::Load;
        command.path = text(body, "path");
        command.kind = text(body, "kind");
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
        if (request.path == "/mosaic/map")
        {
            return HttpResponse{200, "application/json", engine.mosaicMapJson()};
        }
        if (request.path == "/livez")
        {
            return HttpResponse{200, "text/plain", "ok\n"};
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
    std::string rest;
    if (int const me = pathNumber(request.path, "/me/", rest); me > 0)
    {
        return meCommand(engine, me, rest, body);
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
    if (int const k = pathNumber(request.path, "/dsk/", rest); k > 0)
    {
        auto& mixer = engine.mixer();
        MixResult result = MixResult::Ok;
        if (rest.empty())
        {
            if (!body.contains("on") || !body["on"].is_boolean())
            {
                return error(400, "on must be true or false");
            }
            result = mixer.dskSet(k, body["on"].get<bool>());
        }
        else if (rest == "auto")
        {
            auto const n = frames(body, 25);
            if (!n)
            {
                return error(400, "frames must be an integer");
            }
            result = mixer.dskAuto(k, *n);
        }
        else
        {
            return error(404, "unknown DSK action " + rest);
        }
        if (result != MixResult::Ok)
        {
            return mixResult(engine, result, k, "DSK");
        }
        for (auto const& dsk : mixer.dskView())
        {
            if (dsk.key == k)
            {
                return reply(200, json{{"dsk", k}, {"on", dsk.on}, {"transition", dsk.transition}, {"gain", dsk.gain}});
            }
        }
        return reply(200, json{{"dsk", k}});
    }
    return error(404, "not found");
}
} // namespace fxeng
