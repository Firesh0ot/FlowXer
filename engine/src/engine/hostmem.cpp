#include "engine/hostmem.hpp"

#include <cuda_runtime_api.h>

namespace fxeng
{
namespace
{
bool readOnlyLockSupported()
{
    static int const supported = [] {
        int device = 0;
        int value = 0;
        if (cudaGetDevice(&device) != cudaSuccess || cudaDeviceGetAttribute(&value, cudaDevAttrHostRegisterReadOnlySupported, device) != cudaSuccess)
        {
            cudaGetLastError();
            return 0;
        }
        return value;
    }();
    return supported != 0;
}
} // namespace

HostRegistry::~HostRegistry()
{
    release();
}

bool HostRegistry::ensure(void const* ptr, std::size_t bytes, bool readOnly)
{
    auto const it = locked_.find(ptr);
    if (it != locked_.end() && it->second >= bytes)
    {
        return true;
    }
    if (refused_.count(ptr) != 0)
    {
        return false;
    }
    if (it != locked_.end())
    {
        cudaHostUnregister(const_cast<void*>(ptr));
        locked_.erase(it);
    }
    unsigned flags = cudaHostRegisterPortable;
    if (readOnly && readOnlyLockSupported())
    {
        flags |= cudaHostRegisterReadOnly;
    }
    auto const err = cudaHostRegister(const_cast<void*>(ptr), bytes, flags);
    if (err == cudaSuccess)
    {
        locked_.emplace(ptr, bytes);
        return true;
    }
    cudaGetLastError();
    // Locked by another registry: usable, and that registry unlocks it.
    if (err == cudaErrorHostMemoryAlreadyRegistered)
    {
        return true;
    }
    refused_.insert(ptr);
    return false;
}

void HostRegistry::release()
{
    for (auto const& entry : locked_)
    {
        cudaHostUnregister(const_cast<void*>(entry.first));
    }
    locked_.clear();
    refused_.clear();
}
} // namespace fxeng
