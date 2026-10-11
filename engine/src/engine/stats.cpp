#include "engine/stats.hpp"

#include <nvml.h>

#include <dirent.h>
#include <dlfcn.h>
#include <unistd.h>

#include <cstdlib>
#include <fstream>
#include <sstream>

namespace fxeng
{
namespace
{
// utime + stime (clock ticks) of a /proc stat line; the fields after the ")" of the name.
std::uint64_t cpuTicks(std::string const& path)
{
    std::ifstream in(path);
    std::string line;
    if (!std::getline(in, line))
    {
        return 0;
    }
    auto const close = line.rfind(')');
    if (close == std::string::npos)
    {
        return 0;
    }
    std::istringstream fields(line.substr(close + 2));
    std::string field;
    std::uint64_t utime = 0;
    std::uint64_t stime = 0;
    // Field 3 (state) is first here; utime is field 14, stime field 15.
    for (int index = 3; index <= 15 && (fields >> field); ++index)
    {
        if (index == 14)
        {
            utime = std::stoull(field);
        }
        else if (index == 15)
        {
            stime = std::stoull(field);
        }
    }
    return utime + stime;
}

std::string readName(std::string const& path)
{
    std::ifstream in(path);
    std::string name;
    std::getline(in, name);
    return name;
}
} // namespace

ThreadCpu::Result ThreadCpu::sample()
{
    Result result;
    auto const now = std::chrono::steady_clock::now();
    double const seconds = lastAt_.time_since_epoch().count() == 0 ? 0.0 : std::chrono::duration<double>(now - lastAt_).count();
    double const ticks = static_cast<double>(sysconf(_SC_CLK_TCK));
    std::map<int, std::pair<std::string, std::uint64_t>> current;
    if (DIR* dir = opendir("/proc/self/task"))
    {
        while (dirent* entry = readdir(dir))
        {
            if (entry->d_name[0] == '.')
            {
                continue;
            }
            int const tid = std::atoi(entry->d_name);
            std::string const base = std::string("/proc/self/task/") + entry->d_name;
            current[tid] = {readName(base + "/comm"), cpuTicks(base + "/stat")};
        }
        closedir(dir);
    }
    std::uint64_t const process = cpuTicks("/proc/self/stat");
    if (seconds > 0)
    {
        for (auto const& [tid, entry] : current)
        {
            auto const before = last_.find(tid);
            std::uint64_t const start = before == last_.end() ? 0 : before->second.second;
            double const used = entry.second >= start ? static_cast<double>(entry.second - start) : 0.0;
            result.threads[entry.first] += 100.0 * used / ticks / seconds;
        }
        result.process = 100.0 * static_cast<double>(process - lastProcess_) / ticks / seconds;
    }
    last_ = std::move(current);
    lastProcess_ = process;
    lastAt_ = now;
    return result;
}

struct Nvml::Impl
{
    void* lib = nullptr;
    nvmlDevice_t device = nullptr;
    nvmlReturn_t (*shutdown)() = nullptr;
    nvmlReturn_t (*utilization)(nvmlDevice_t, nvmlUtilization_t*) = nullptr;
    nvmlReturn_t (*encoder)(nvmlDevice_t, unsigned*, unsigned*) = nullptr;
    nvmlReturn_t (*sessions)(nvmlDevice_t, unsigned*, nvmlEncoderSessionInfo_t*) = nullptr;
    nvmlReturn_t (*memory)(nvmlDevice_t, nvmlMemory_t*) = nullptr;
};

Nvml::Nvml(std::string const& pciBusId)
    : impl_(new Impl)
{
    impl_->lib = dlopen("libnvidia-ml.so.1", RTLD_NOW | RTLD_LOCAL);
    if (impl_->lib == nullptr)
    {
        return;
    }
    auto* init = reinterpret_cast<nvmlReturn_t (*)()>(dlsym(impl_->lib, "nvmlInit_v2"));
    auto* byBus = reinterpret_cast<nvmlReturn_t (*)(char const*, nvmlDevice_t*)>(dlsym(impl_->lib, "nvmlDeviceGetHandleByPciBusId_v2"));
    impl_->shutdown = reinterpret_cast<nvmlReturn_t (*)()>(dlsym(impl_->lib, "nvmlShutdown"));
    impl_->utilization = reinterpret_cast<nvmlReturn_t (*)(nvmlDevice_t, nvmlUtilization_t*)>(dlsym(impl_->lib, "nvmlDeviceGetUtilizationRates"));
    impl_->encoder = reinterpret_cast<nvmlReturn_t (*)(nvmlDevice_t, unsigned*, unsigned*)>(dlsym(impl_->lib, "nvmlDeviceGetEncoderUtilization"));
    impl_->sessions =
        reinterpret_cast<nvmlReturn_t (*)(nvmlDevice_t, unsigned*, nvmlEncoderSessionInfo_t*)>(dlsym(impl_->lib, "nvmlDeviceGetEncoderSessions"));
    impl_->memory = reinterpret_cast<nvmlReturn_t (*)(nvmlDevice_t, nvmlMemory_t*)>(dlsym(impl_->lib, "nvmlDeviceGetMemoryInfo"));
    if (init == nullptr || byBus == nullptr || init() != NVML_SUCCESS || byBus(pciBusId.c_str(), &impl_->device) != NVML_SUCCESS)
    {
        impl_->device = nullptr;
    }
}

Nvml::~Nvml()
{
    if (impl_->device != nullptr && impl_->shutdown != nullptr)
    {
        impl_->shutdown();
    }
    if (impl_->lib != nullptr)
    {
        dlclose(impl_->lib);
    }
    delete impl_;
}

GpuSample Nvml::sample()
{
    GpuSample out;
    if (impl_->device == nullptr)
    {
        return out;
    }
    out.valid = true;
    nvmlUtilization_t util{};
    if (impl_->utilization != nullptr && impl_->utilization(impl_->device, &util) == NVML_SUCCESS)
    {
        out.utilization = static_cast<int>(util.gpu);
    }
    unsigned encoder = 0;
    unsigned period = 0;
    if (impl_->encoder != nullptr && impl_->encoder(impl_->device, &encoder, &period) == NVML_SUCCESS)
    {
        out.encoder = static_cast<int>(encoder);
    }
    unsigned count = 0;
    if (impl_->sessions != nullptr && impl_->sessions(impl_->device, &count, nullptr) == NVML_SUCCESS)
    {
        out.encoderSessions = count;
    }
    nvmlMemory_t memory{};
    if (impl_->memory != nullptr && impl_->memory(impl_->device, &memory) == NVML_SUCCESS)
    {
        out.memoryUsed = memory.used;
        out.memoryTotal = memory.total;
    }
    return out;
}
} // namespace fxeng
