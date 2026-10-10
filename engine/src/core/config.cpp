#include "core/config.hpp"

#include "core/mosaic_map.hpp"

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

std::vector<InputConfig> parseInputs(std::string const& text)
{
    if (text.empty())
    {
        throw ConfigError("FLOWXER_ENGINE_INPUTS: required (JSON array of {domain, video_flow, audio_flow, label})");
    }
    nlohmann::json list;
    try
    {
        list = nlohmann::json::parse(text);
    }
    catch (std::exception const& ex)
    {
        throw ConfigError("FLOWXER_ENGINE_INPUTS: " + std::string(ex.what()));
    }
    if (!list.is_array() || list.empty() || list.size() > static_cast<std::size_t>(kMaxInputs))
    {
        throw ConfigError("FLOWXER_ENGINE_INPUTS: an array of 1.." + std::to_string(kMaxInputs) + " inputs");
    }
    std::vector<InputConfig> inputs;
    for (auto const& item : list)
    {
        if (!item.is_object())
        {
            throw ConfigError("FLOWXER_ENGINE_INPUTS: every input is an object");
        }
        InputConfig input;
        input.domain = field(item, "domain");
        // "video_flow"/"audio_flow"; "flow"/"audio" are accepted too.
        input.flow = item.contains("video_flow") ? field(item, "video_flow") : field(item, "flow");
        input.label = field(item, "label");
        input.audioFlow = item.contains("audio_flow") ? field(item, "audio_flow") : field(item, "audio");
        input.audioDomain = field(item, "audio_domain");
        if (input.audioDomain.empty())
        {
            input.audioDomain = input.domain;
        }
        if ((!input.flow.empty() && input.domain.empty()) || (!input.audioFlow.empty() && input.audioDomain.empty()))
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
// FLOWXER_ENGINE_OUTPUTS: [{"me": 1, "domain", "domain_id", "pgm_video", "pgm_audio", "pvw_video"}],
// every field optional; MEs not listed take the defaults.
std::vector<OutputConfig> parseOutputs(std::string const& value, int mes, std::string const& domain, std::string const& domainId)
{
    std::vector<OutputConfig> outputs(static_cast<std::size_t>(mes), OutputConfig{domain, domainId, {}, {}, {}});
    if (value.empty())
    {
        return outputs;
    }
    nlohmann::json list = nlohmann::json::parse(value, nullptr, false);
    if (list.is_discarded() || !list.is_array())
    {
        throw ConfigError("FLOWXER_ENGINE_OUTPUTS: a JSON array of {me, domain, domain_id, pgm_video, pgm_audio, pvw_video}");
    }
    for (auto const& item : list)
    {
        if (!item.is_object() || !item.contains("me") || !item["me"].is_number_integer())
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
    cfg.mes = getInt(values, "FLOWXER_ENGINE_MES", cfg.mes, 1, kMaxMes);
    cfg.latency = getInt(values, "FLOWXER_ENGINE_LATENCY", cfg.latency, 1, 8);
    cfg.format = get(values, "FLOWXER_ENGINE_FORMAT", cfg.format);
    if (!parseFormat(cfg.format, cfg.width, cfg.height, cfg.rate))
    {
        throw ConfigError("FLOWXER_ENGINE_FORMAT: one of 720p/1080p/2160p with 25, 2997, 30, 50, 5994, 60 (e.g. 1080p50), is " + cfg.format);
    }
    cfg.outputDomain = get(values, "FLOWXER_ENGINE_OUTPUT_DOMAIN", cfg.outputDomain);
    cfg.outputDomainId = get(values, "FLOWXER_ENGINE_OUTPUT_DOMAIN_ID", cfg.outputDomainId);
    cfg.outputs = parseOutputs(get(values, "FLOWXER_ENGINE_OUTPUTS", ""), cfg.mes, cfg.outputDomain, cfg.outputDomainId);
    cfg.audioChannels = getInt(values, "FLOWXER_ENGINE_AUDIO_CHANNELS", cfg.audioChannels, 1, 64);
    cfg.tallyTsl = get(values, "FLOWXER_TALLY_TSL", cfg.tallyTsl);
    if (!cfg.tallyTsl.empty() && cfg.tallyTsl.rfind("udp://", 0) != 0 && cfg.tallyTsl.rfind("tcp://", 0) != 0)
    {
        throw ConfigError("FLOWXER_TALLY_TSL: udp://host:port or tcp://host:port, is " + cfg.tallyTsl);
    }
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
