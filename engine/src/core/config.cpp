#include "core/config.hpp"

#include <nlohmann/json.hpp>

#include <fstream>

namespace fxeng
{
namespace
{
using Values = std::map<std::string, std::string>;

std::string get(Values const& values, std::string const& key, std::string const& fallback)
{
    auto const it = values.find(key);
    return it == values.end() || it->second.empty() ? fallback : it->second;
}

int getInt(Values const& values, std::string const& key, int fallback, int lo, int hi)
{
    auto const text = get(values, key, "");
    if (text.empty())
    {
        return fallback;
    }
    std::size_t used = 0;
    int value = 0;
    try
    {
        value = std::stoi(text, &used);
    }
    catch (std::exception const&)
    {
        throw ConfigError(key + ": not a number: " + text);
    }
    if (used != text.size() || value < lo || value > hi)
    {
        throw ConfigError(key + ": must be " + std::to_string(lo) + ".." + std::to_string(hi) + ", is " + text);
    }
    return value;
}

bool getBool(Values const& values, std::string const& key, bool fallback)
{
    auto const text = get(values, key, "");
    if (text.empty())
    {
        return fallback;
    }
    if (text == "true" || text == "1" || text == "on")
    {
        return true;
    }
    if (text == "false" || text == "0" || text == "off")
    {
        return false;
    }
    throw ConfigError(key + ": must be true or false, is " + text);
}

// The file's keys, every value as text (arrays and objects as JSON).
Values readFile(std::string const& path)
{
    std::ifstream in(path);
    if (!in)
    {
        throw ConfigError("FLOWXER_ENGINE_CONFIG: cannot read " + path);
    }
    nlohmann::json root;
    try
    {
        in >> root;
    }
    catch (std::exception const& ex)
    {
        throw ConfigError("FLOWXER_ENGINE_CONFIG: " + std::string(ex.what()));
    }
    if (!root.is_object())
    {
        throw ConfigError("FLOWXER_ENGINE_CONFIG: not a JSON object");
    }
    Values values;
    for (auto const& [key, value] : root.items())
    {
        values[key] = value.is_string() ? value.get<std::string>() : value.dump();
    }
    return values;
}

// A string field of a JSON object, or empty.
std::string field(nlohmann::json const& item, char const* key)
{
    auto const it = item.find(key);
    return it != item.end() && it->is_string() ? it->get<std::string>() : std::string{};
}

nlohmann::json parseArray(std::string const& key, std::string const& text)
{
    if (text.empty())
    {
        return nlohmann::json::array();
    }
    nlohmann::json list = nlohmann::json::parse(text, nullptr, false);
    if (list.is_discarded() || !list.is_array())
    {
        throw ConfigError(key + ": a JSON array");
    }
    for (auto const& item : list)
    {
        if (!item.is_object())
        {
            throw ConfigError(key + ": every entry is a JSON object");
        }
    }
    return list;
}

// FLOWXER_ENGINE_INPUTS: [{"label", "domain", "video_flow", "audio_flow", "audio_domain"}], the
// live inputs in slot order. The routes are optional: FlowXer routes the inputs at run time.
std::vector<InputConfig> parseInputs(std::string const& text)
{
    std::vector<InputConfig> inputs;
    for (auto const& item : parseArray("FLOWXER_ENGINE_INPUTS", text))
    {
        InputConfig input;
        input.label = field(item, "label");
        input.video.domain = field(item, "domain");
        // "video_flow"/"audio_flow"; "flow"/"audio" are accepted too.
        input.video.flow = item.contains("video_flow") ? field(item, "video_flow") : field(item, "flow");
        input.audio.flow = item.contains("audio_flow") ? field(item, "audio_flow") : field(item, "audio");
        input.audio.domain = field(item, "audio_domain");
        if (input.audio.domain.empty())
        {
            input.audio.domain = input.video.domain;
        }
        if ((!input.video.flow.empty() && input.video.domain.empty()) || (!input.audio.flow.empty() && input.audio.domain.empty()))
        {
            throw ConfigError("FLOWXER_ENGINE_INPUTS: input " + std::to_string(inputs.size() + 1) + " has a flow but no \"domain\"");
        }
        if (input.label.empty())
        {
            input.label = "Input " + std::to_string(inputs.size() + 1);
        }
        inputs.push_back(std::move(input));
    }
    return inputs;
}

// FLOWXER_ENGINE_PLAYERS: [{"label"}], the clip players in slot order.
std::vector<PlayerConfig> parsePlayers(std::string const& text)
{
    std::vector<PlayerConfig> players;
    for (auto const& item : parseArray("FLOWXER_ENGINE_PLAYERS", text))
    {
        PlayerConfig player;
        player.label = field(item, "label");
        if (player.label.empty())
        {
            player.label = "Clip " + std::to_string(players.size() + 1);
        }
        players.push_back(std::move(player));
    }
    return players;
}

// FLOWXER_ENGINE_KEYS: [{"label", "domain", "flow", "me", "stage": "upstream" | "downstream"}], one
// keyer per entry (default a DSK of ME 1).
std::vector<KeyConfig> parseKeys(std::string const& text, int mes)
{
    std::vector<KeyConfig> keys;
    for (auto const& item : parseArray("FLOWXER_ENGINE_KEYS", text))
    {
        KeyConfig key;
        key.label = field(item, "label");
        key.route.domain = field(item, "domain");
        key.route.flow = field(item, "flow");
        if (key.label.empty())
        {
            key.label = "DSK " + std::to_string(keys.size() + 1) + " Key";
        }
        if (item.contains("me"))
        {
            if (!item["me"].is_number_integer() || item["me"].get<int>() < 1 || item["me"].get<int>() > mes)
            {
                throw ConfigError("FLOWXER_ENGINE_KEYS: \"me\" must be 1.." + std::to_string(mes));
            }
            key.me = item["me"].get<int>();
        }
        auto const stage = field(item, "stage");
        if (!stage.empty() && stage != "upstream" && stage != "downstream")
        {
            throw ConfigError("FLOWXER_ENGINE_KEYS: \"stage\" must be upstream or downstream");
        }
        key.upstream = stage == "upstream";
        if (!key.route.flow.empty() && key.route.domain.empty())
        {
            throw ConfigError("FLOWXER_ENGINE_KEYS: key " + std::to_string(keys.size() + 1) + " has a flow but no \"domain\"");
        }
        keys.push_back(std::move(key));
    }
    if (keys.size() > static_cast<std::size_t>(kMaxKeys))
    {
        throw ConfigError("FLOWXER_ENGINE_KEYS: at most " + std::to_string(kMaxKeys) + " keyers");
    }
    return keys;
}

// FLOWXER_ENGINE_OUTPUTS: [{"me": 1, "domain", "domain_id", "pgm_video", "pgm_audio", "pvw_video",
// "label", "group"}], every field but "me" optional; MEs not listed take the defaults.
std::vector<OutputConfig> parseOutputs(std::string const& value, int mes, std::string const& domain, std::string const& domainId)
{
    std::vector<OutputConfig> outputs(static_cast<std::size_t>(mes), OutputConfig{domain, domainId, {}, {}, {}, {}, {}});
    for (auto const& item : parseArray("FLOWXER_ENGINE_OUTPUTS", value))
    {
        if (!item.contains("me") || !item["me"].is_number_integer())
        {
            throw ConfigError("FLOWXER_ENGINE_OUTPUTS: every entry needs \"me\" (1.." + std::to_string(mes) + ")");
        }
        int const me = item["me"].get<int>();
        if (me < 1 || me > mes)
        {
            throw ConfigError("FLOWXER_ENGINE_OUTPUTS: me " + std::to_string(me) + " is not 1.." + std::to_string(mes));
        }
        auto& out = outputs[static_cast<std::size_t>(me - 1)];
        if (!field(item, "domain").empty())
        {
            out.domain = field(item, "domain");
            out.domainId = field(item, "domain_id");
        }
        else if (!field(item, "domain_id").empty())
        {
            out.domainId = field(item, "domain_id");
        }
        out.pgmVideo = field(item, "pgm_video");
        out.pgmAudio = field(item, "pgm_audio");
        out.pvwVideo = field(item, "pvw_video");
        out.label = field(item, "label");
        out.group = field(item, "group");
    }
    return outputs;
}
} // namespace

bool parseFormat(std::string const& token, int& width, int& height, Rate& rate)
{
    auto const p = token.find('p');
    if (p == std::string::npos || p == 0)
    {
        return false;
    }
    auto const lines = token.substr(0, p);
    auto const fps = token.substr(p + 1);
    if (lines == "720")
    {
        width = 1280;
        height = 720;
    }
    else if (lines == "1080")
    {
        width = 1920;
        height = 1080;
    }
    else if (lines == "2160")
    {
        width = 3840;
        height = 2160;
    }
    else
    {
        return false;
    }
    if (fps == "25" || fps == "50" || fps == "30" || fps == "60")
    {
        rate = Rate{std::stoi(fps), 1};
    }
    else if (fps == "2997")
    {
        rate = Rate{30000, 1001};
    }
    else if (fps == "5994")
    {
        rate = Rate{60000, 1001};
    }
    else
    {
        return false;
    }
    return true;
}

Config loadConfig(std::map<std::string, std::string> const& env)
{
    Values values;
    auto const file = get(env, "FLOWXER_ENGINE_CONFIG", "");
    if (!file.empty())
    {
        values = readFile(file);
    }
    for (auto const& [key, value] : env)
    {
        if (!value.empty())
        {
            values[key] = value;
        }
    }
    Config cfg;
    cfg.inputs = parseInputs(get(values, "FLOWXER_ENGINE_INPUTS", ""));
    cfg.players = parsePlayers(get(values, "FLOWXER_ENGINE_PLAYERS", ""));
    if (cfg.inputs.size() + cfg.players.size() > static_cast<std::size_t>(kMaxSources))
    {
        throw ConfigError("FLOWXER_ENGINE_INPUTS + FLOWXER_ENGINE_PLAYERS: at most " + std::to_string(kMaxSources) + " sources");
    }
    cfg.mes = getInt(values, "FLOWXER_ENGINE_MES", cfg.mes, 1, kMaxMes);
    cfg.keys = parseKeys(get(values, "FLOWXER_ENGINE_KEYS", ""), cfg.mes);
    cfg.stingerPlayers = getInt(values, "FLOWXER_ENGINE_STINGER_PLAYERS", cfg.stingerPlayers, 0, kMaxStingerPlayers);
    cfg.latency = getInt(values, "FLOWXER_ENGINE_LATENCY", cfg.latency, 1, kMaxLatency);
    cfg.format = get(values, "FLOWXER_ENGINE_FORMAT", cfg.format);
    if (!parseFormat(cfg.format, cfg.width, cfg.height, cfg.rate))
    {
        throw ConfigError("FLOWXER_ENGINE_FORMAT: one of 720p/1080p/2160p with 25, 2997, 30, 50, 5994, 60 (e.g. 1080p50), is " + cfg.format);
    }
    cfg.outputDomain = get(values, "FLOWXER_ENGINE_OUTPUT_DOMAIN", cfg.outputDomain);
    cfg.outputDomainId = get(values, "FLOWXER_ENGINE_OUTPUT_DOMAIN_ID", cfg.outputDomainId);
    cfg.outputs = parseOutputs(get(values, "FLOWXER_ENGINE_OUTPUTS", ""), cfg.mes, cfg.outputDomain, cfg.outputDomainId);
    cfg.pvwOutputs = getBool(values, "FLOWXER_ENGINE_PVW_OUTPUTS", cfg.pvwOutputs);
    cfg.audioChannels = getInt(values, "FLOWXER_ENGINE_AUDIO_CHANNELS", cfg.audioChannels, 1, 64);
    cfg.audioWaitMs = getInt(values, "FLOWXER_ENGINE_AUDIO_WAIT_MS", cfg.audioWaitMs, 0, 40);
    cfg.label = get(values, "FLOWXER_ENGINE_LABEL", cfg.label);
    cfg.workFormat = get(values, "FLOWXER_ENGINE_WORK_FORMAT", cfg.workFormat);
    if (cfg.workFormat != "yuv16" && cfg.workFormat != "rgba16f")
    {
        throw ConfigError("FLOWXER_ENGINE_WORK_FORMAT: yuv16 or rgba16f, is " + cfg.workFormat);
    }
    cfg.gpu = getInt(values, "FLOWXER_ENGINE_GPU", cfg.gpu, 0, 63);
    cfg.httpBind = get(values, "FLOWXER_ENGINE_HTTP_BIND", cfg.httpBind);
    cfg.httpPort = getInt(values, "FLOWXER_ENGINE_HTTP_PORT", cfg.httpPort, 1, 65535);
    cfg.holdGrains = getInt(values, "FLOWXER_ENGINE_HOLD_GRAINS", cfg.holdGrains, 0, 3000000);
    cfg.mosaic = getBool(values, "FLOWXER_ENGINE_MOSAIC", cfg.mosaic);
    cfg.mosaicFps = getInt(values, "FLOWXER_ENGINE_MOSAIC_FPS", cfg.mosaicFps, 1, 60);
    cfg.mosaicKbps = getInt(values, "FLOWXER_ENGINE_MOSAIC_KBPS", cfg.mosaicKbps, 200, 50000);
    cfg.publishUrl = get(values, "PREVIEW_PUBLISH_URL", cfg.publishUrl);
    while (!cfg.publishUrl.empty() && cfg.publishUrl.back() == '/')
    {
        cfg.publishUrl.pop_back();
    }
    if (!cfg.publishUrl.empty() && cfg.publishUrl.rfind("rtsp://", 0) != 0)
    {
        throw ConfigError("PREVIEW_PUBLISH_URL: an rtsp:// URL, is " + cfg.publishUrl);
    }
    cfg.pathPrefix = get(values, "PREVIEW_PATH_PREFIX", cfg.pathPrefix);
    while (!cfg.pathPrefix.empty() && (cfg.pathPrefix.back() == '/' || cfg.pathPrefix.front() == '/'))
    {
        cfg.pathPrefix.erase(cfg.pathPrefix.back() == '/' ? cfg.pathPrefix.size() - 1 : 0, 1);
    }
    cfg.whepBase = get(values, "PREVIEW_WHEP_URL", cfg.whepBase);
    cfg.hlsBase = get(values, "PREVIEW_HLS_URL", cfg.hlsBase);
    cfg.mediamtxBin = get(values, "MEDIAMTX_BIN", cfg.mediamtxBin);
    cfg.stateDir = get(values, "FLOWXER_ENGINE_STATE_DIR", cfg.stateDir);
    cfg.rtspPort = getInt(values, "MEDIAMTX_RTSP_PORT", cfg.rtspPort, 1, 65535);
    cfg.whepPort = getInt(values, "MEDIAMTX_WHEP_PORT", cfg.whepPort, 1, 65535);
    cfg.hlsPort = getInt(values, "MEDIAMTX_HLS_PORT", cfg.hlsPort, 1, 65535);
    cfg.icePort = getInt(values, "MEDIAMTX_ICE_PORT", cfg.icePort, 1, 65535);
    cfg.apiPort = getInt(values, "MEDIAMTX_API_PORT", cfg.apiPort, 1, 65535);
    cfg.publicIp = get(values, "MEDIAMTX_PUBLIC_IP", cfg.publicIp);
    return cfg;
}
} // namespace fxeng
