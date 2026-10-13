#include "engine/http.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <vector>

namespace fxeng
{
namespace
{
constexpr std::size_t kMaxRequestBytes = 64u * 1024u;
constexpr std::size_t kMaxConnections = 64;

bool sendAll(int fd, std::string const& data)
{
    std::size_t sent = 0;
    while (sent < data.size())
    {
        auto const n = ::send(fd, data.data() + sent, data.size() - sent, MSG_NOSIGNAL);
        if (n <= 0)
        {
            return false;
        }
        sent += static_cast<std::size_t>(n);
    }
    return true;
}

std::string statusText(int status)
{
    switch (status)
    {
    case 200:
        return "OK";
    case 400:
        return "Bad Request";
    case 404:
        return "Not Found";
    case 405:
        return "Method Not Allowed";
    case 409:
        return "Conflict";
    case 413:
        return "Payload Too Large";
    default:
        return "Error";
    }
}

// A complete request at the start of `buffer`, or false while more bytes are needed.
bool parseRequest(std::string& buffer, HttpRequest& request, bool& tooLarge)
{
    auto const headerEnd = buffer.find("\r\n\r\n");
    if (headerEnd == std::string::npos)
    {
        tooLarge = buffer.size() > kMaxRequestBytes;
        return false;
    }
    std::istringstream lines(buffer.substr(0, headerEnd));
    std::string line;
    std::getline(lines, line);
    std::istringstream first(line);
    std::string target;
    first >> request.method >> target;
    auto const q = target.find('?');
    request.path = target.substr(0, q);
    request.query = q == std::string::npos ? std::string{} : target.substr(q + 1);
    std::size_t bodyLen = 0;
    while (std::getline(lines, line))
    {
        if (!line.empty() && line.back() == '\r')
        {
            line.pop_back();
        }
        auto const colon = line.find(':');
        if (colon == std::string::npos)
        {
            continue;
        }
        std::string key = line.substr(0, colon);
        std::transform(key.begin(), key.end(), key.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        std::string value = line.substr(colon + 1);
        value.erase(0, value.find_first_not_of(' '));
        if (key == "content-length")
        {
            bodyLen = static_cast<std::size_t>(std::strtoul(value.c_str(), nullptr, 10));
        }
        request.headers[key] = value;
    }
    if (bodyLen > kMaxRequestBytes)
    {
        tooLarge = true;
        return false;
    }
    if (buffer.size() < headerEnd + 4 + bodyLen)
    {
        return false;
    }
    request.body = buffer.substr(headerEnd + 4, bodyLen);
    return true;
}
} // namespace

struct HttpServer::Impl
{
    int listenFd = -1;
    std::atomic<bool> run{false};
    std::thread thread;
    HttpHandler handler;
    struct Conn
    {
        int fd = -1;
        std::string buffer;
    };
    std::vector<Conn> conns;

    void reply(int fd, HttpResponse const& response)
    {
        sendAll(fd, "HTTP/1.1 " + std::to_string(response.status) + " " + statusText(response.status) + "\r\nContent-Type: " + response.contentType +
                        "\r\nContent-Length: " + std::to_string(response.body.size()) +
                        "\r\nCache-Control: no-store\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n" + response.body);
    }

    void loop()
    {
        pthread_setname_np(pthread_self(), "fx-http");
        while (run.load())
        {
            std::vector<pollfd> fds;
            fds.push_back(pollfd{listenFd, POLLIN, 0});
            for (auto const& conn : conns)
            {
                fds.push_back(pollfd{conn.fd, POLLIN, 0});
            }
            // Blocks until a socket is readable; the timeout only lets stop() end the loop.
            if (::poll(fds.data(), static_cast<nfds_t>(fds.size()), 500) <= 0)
            {
                continue;
            }
            std::vector<int> done;
            for (std::size_t i = 1; i < fds.size(); ++i)
            {
                if ((fds[i].revents & (POLLIN | POLLHUP | POLLERR)) == 0)
                {
                    continue;
                }
                auto& conn = conns[i - 1];
                char buf[4096];
                auto const n = ::recv(conn.fd, buf, sizeof(buf), 0);
                if (n <= 0)
                {
                    done.push_back(conn.fd);
                    continue;
                }
                conn.buffer.append(buf, static_cast<std::size_t>(n));
                HttpRequest request;
                bool tooLarge = false;
                if (parseRequest(conn.buffer, request, tooLarge))
                {
                    reply(conn.fd, handler ? handler(request) : HttpResponse{404, "application/json", "{}"});
                    done.push_back(conn.fd);
                }
                else if (tooLarge)
                {
                    reply(conn.fd, HttpResponse{413, "application/json", "{\"error\":\"request too large\"}"});
                    done.push_back(conn.fd);
                }
            }
            if ((fds[0].revents & POLLIN) != 0)
            {
                int const fd = ::accept(listenFd, nullptr, nullptr);
                if (fd >= 0 && conns.size() < kMaxConnections)
                {
                    conns.push_back(Conn{fd, {}});
                }
                else if (fd >= 0)
                {
                    ::close(fd);
                }
            }
            for (int fd : done)
            {
                ::close(fd);
            }
            conns.erase(std::remove_if(conns.begin(), conns.end(), [&](Conn const& c) { return std::find(done.begin(), done.end(), c.fd) != done.end(); }),
                conns.end());
        }
        for (auto const& conn : conns)
        {
            ::close(conn.fd);
        }
        conns.clear();
    }
};

HttpServer::HttpServer()
    : impl_(new Impl)
{
}

HttpServer::~HttpServer()
{
    stop();
    delete impl_;
}

void HttpServer::start(std::string const& bind, int port, HttpHandler handler)
{
    stop();
    impl_->handler = std::move(handler);
    impl_->listenFd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (impl_->listenFd < 0)
    {
        throw std::runtime_error("socket failed");
    }
    int const one = 1;
    ::setsockopt(impl_->listenFd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<std::uint16_t>(port));
    if (::inet_pton(AF_INET, bind.c_str(), &addr.sin_addr) != 1 || ::bind(impl_->listenFd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
        ::listen(impl_->listenFd, 64) != 0)
    {
        ::close(impl_->listenFd);
        impl_->listenFd = -1;
        throw std::runtime_error("cannot listen on " + bind + ":" + std::to_string(port) + ": " + std::strerror(errno));
    }
    impl_->run.store(true);
    impl_->thread = std::thread([this] { impl_->loop(); });
}

void HttpServer::stop()
{
    if (!impl_->run.exchange(false))
    {
        return;
    }
    if (impl_->thread.joinable())
    {
        impl_->thread.join();
    }
    ::close(impl_->listenFd);
    impl_->listenFd = -1;
}
} // namespace fxeng
