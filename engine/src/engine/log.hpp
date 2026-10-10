// One JSON object per line on stderr.
#pragma once

#include <nlohmann/json.hpp>

#include <cstdio>
#include <string>

namespace fxeng
{
inline void logLine(char const* level, std::string const& event, nlohmann::json fields = nlohmann::json::object())
{
    fields["level"] = level;
    fields["event"] = event;
    std::fprintf(stderr, "%s\n", fields.dump().c_str());
}

inline void logInfo(std::string const& event, nlohmann::json fields = nlohmann::json::object())
{
    logLine("info", event, std::move(fields));
}

inline void logWarn(std::string const& event, nlohmann::json fields = nlohmann::json::object())
{
    logLine("warn", event, std::move(fields));
}

inline void logError(std::string const& event, nlohmann::json fields = nlohmann::json::object())
{
    logLine("error", event, std::move(fields));
}
} // namespace fxeng
