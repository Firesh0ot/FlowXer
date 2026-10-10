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

std::vector<InputConfig> parseInputs(std::string const& text)
{
    if (text.empty())
    {
        throw ConfigError("FLOWXER_ENGINE_INPUTS: required (JSON array of {domain, flow, label})");
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
        if (!item.is_object() || !item.contains("domain") || !item.contains("flow") || !item["domain"].is_string() || !item["flow"].is_string())
        {
            throw ConfigError("FLOWXER_ENGINE_INPUTS: every input needs \"domain\" and \"flow\"");
        }
        InputConfig input;
        input.domain = item["domain"].get<std::string>();
        input.flow = item["flow"].get<std::string>();
        input.label = item.contains("label") && item["label"].is_string() ? item["label"].get<std::string>()
                                                                           : "Input " + std::to_string(inputs.size() + 1);
        inputs.push_back(std::move(input));
    }
    return inputs;
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
    cfg.label = get(values, "FLOWXER_ENGINE_LABEL", cfg.label);
    cfg.workFormat = get(values, "FLOWXER_ENGINE_WORK_FORMAT", cfg.workFormat);
    if (cfg.workFormat != "yuv16" && cfg.workFormat != "rgba16f")
    {
        throw ConfigError("FLOWXER_ENGINE_WORK_FORMAT: yuv16 or rgba16f, is " + cfg.workFormat);
    }
    cfg.gpu = getInt(values, "FLOWXER_ENGINE_GPU", cfg.gpu, 0, 63);
    cfg.httpBind = get(values, "FLOWXER_ENGINE_HTTP_BIND", cfg.httpBind);
    cfg.httpPort = getInt(values, "FLOWXER_ENGINE_HTTP_PORT", cfg.httpPort, 1, 65535);
    cfg.holdGrains = getInt(values, "FLOWXER_ENGINE_HOLD_GRAINS", cfg.holdGrains, 1, 3000);
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
