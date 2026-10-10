// Host-side measurements: CPU per thread (from /proc) and GPU / NVENC load (NVML, loaded
// at run time so the binary starts without it).
#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace fxeng
{
// CPU of the process and of each thread name (threads of the same name summed), in percent
// of one core since the previous sample.
class ThreadCpu
{
public:
    struct Result
    {
        double process = 0;
        std::map<std::string, double> threads;
    };
    Result sample();

private:
    std::map<int, std::pair<std::string, std::uint64_t>> last_;
    std::uint64_t lastProcess_ = 0;
    std::chrono::steady_clock::time_point lastAt_{};
};

struct GpuSample
{
    bool valid = false;
    int utilization = 0;
    int encoder = 0;
    unsigned encoderSessions = 0;
    std::uint64_t memoryUsed = 0;
    std::uint64_t memoryTotal = 0;
};

class Nvml
{
public:
    // The device with this PCI bus id (as cudaDeviceGetPCIBusId gives it).
    explicit Nvml(std::string const& pciBusId);
    ~Nvml();
    Nvml(Nvml const&) = delete;
    Nvml& operator=(Nvml const&) = delete;
    GpuSample sample();

private:
    struct Impl;
    Impl* impl_;
};
} // namespace fxeng
