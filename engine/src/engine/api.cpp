#include "engine/api.hpp"

#include "engine/engine.hpp"
#include "mosaic_page.hpp"

#include <nlohmann/json.hpp>

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

json meView(Engine& engine, int me)
{
    for (auto const& view : engine.mixer().view())
    {
        if (view.me == me)
        {
            return json{{"me", view.me}, {"program", sourceName(view.program)}, {"preview", sourceName(view.preview)}, {"mixing", view.mixing},
                {"frames", view.frames}};
        }
    }
    return json::object();
}

HttpResponse meCommand(Engine& engine, HttpRequest const& request)
{
    // /me/{m}/{action}
    auto const rest = request.path.substr(4);
    auto const slash = rest.find('/');
    auto const number = rest.substr(0, slash);
    if (slash == std::string::npos || number.empty() || number.size() > 2 || number.find_first_not_of("0123456789") != std::string::npos)
    {
        return error(404, "use /me/{m}/preview|program|cut|auto");
    }
    int const me = std::stoi(number);
    auto const action = rest.substr(slash + 1);
    json body = request.body.empty() ? json::object() : json::parse(request.body, nullptr, false);
    if (body.is_discarded() || !body.is_object())
    {
        return error(400, "the body must be a JSON object");
    }
    auto& mixer = engine.mixer();
    MixResult result = MixResult::Ok;
    if (action == "preview" || action == "program")
    {
        auto const text = body.contains("source") && body["source"].is_string() ? body["source"].get<std::string>() : std::string{};
        auto const source = parseSource(text);
        if (!source)
        {
            return error(400, "source must be in1..in8 or me2..me4");
        }
        result = action == "preview" ? mixer.setPreview(me, *source) : mixer.setProgram(me, *source);
    }
    else if (action == "cut")
    {
        result = mixer.cut(me);
    }
    else if (action == "auto")
    {
        int frames = 25;
        if (body.contains("frames"))
        {
            if (!body["frames"].is_number_integer())
            {
                return error(400, "frames must be an integer");
            }
            frames = body["frames"].get<int>();
        }
        result = mixer.autoMix(me, frames);
    }
    else
    {
        return error(404, "unknown action " + action);
    }
    switch (result)
    {
    case MixResult::Ok:
        return reply(200, meView(engine, me));
    case MixResult::BadMe:
        return error(404, "no ME " + std::to_string(me));
    case MixResult::BadSource:
        return error(400, "ME " + std::to_string(me) + " takes the inputs and the Program of a higher ME (re-entry n > m)");
    case MixResult::BadFrames:
        return error(400, "frames must be 1.." + std::to_string(Mixer::kMaxFrames));
    case MixResult::Busy:
        return error(409, "a Mix is running on ME " + std::to_string(me));
    }
    return error(500, "unexpected");
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
        if (request.path == "/" || request.path == "/mosaic")
        {
            return HttpResponse{200, "text/html; charset=utf-8", kMosaicPage};
        }
        if (request.path == "/livez")
        {
            return HttpResponse{200, "text/plain", "ok\n"};
        }
        return error(404, "not found");
    }
    if (request.method == "POST" && request.path.rfind("/me/", 0) == 0)
    {
        return meCommand(engine, request);
    }
    return error(405, "method not allowed");
}
} // namespace fxeng
