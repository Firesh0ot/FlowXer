// Minimal HTTP/1.1 server (one thread, poll, Connection: close) for the control API.
// Adapted from mxl-multiviewer src/ops/httpserver.cpp (Apache-2.0, same author).
#pragma once

#include <functional>
#include <map>
#include <string>

namespace fxeng
{
struct HttpRequest
{
    std::string method;
    std::string path;
    std::string query;
    std::string body;
    std::map<std::string, std::string> headers;
};

struct HttpResponse
{
    int status = 200;
    std::string contentType = "application/json";
    std::string body;
};

using HttpHandler = std::function<HttpResponse(HttpRequest const&)>;

class HttpServer
{
public:
    HttpServer();
    ~HttpServer();
    HttpServer(HttpServer const&) = delete;
    HttpServer& operator=(HttpServer const&) = delete;

    // Throws std::runtime_error when the address cannot be bound.
    void start(std::string const& bind, int port, HttpHandler handler);
    void stop();

private:
    struct Impl;
    Impl* impl_;
};
} // namespace fxeng
