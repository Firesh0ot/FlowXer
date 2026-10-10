// The control API (localhost by default):
//   POST /me/{m}/preview {"source": "in3"}   POST /me/{m}/program {"source": "me2"}
//   POST /me/{m}/cut                          POST /me/{m}/auto {"frames": 25}
//   GET /status, /metrics (Prometheus), /mosaic/map, /mosaic (test page), /livez
#pragma once

#include "engine/http.hpp"

namespace fxeng
{
class Engine;

HttpResponse handleRequest(Engine& engine, HttpRequest const& request);
} // namespace fxeng
