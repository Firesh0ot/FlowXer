// Page-locks MXL grain memory so CUDA copies to and from it are direct DMA (no staging copy
// on the CPU). Each MXL grain is its own mapping that lives as long as its reader or writer,
// so it is locked once. One registry per thread that owns the reader or writer; release()
// before that reader or writer is released (the mapping goes away).
// Adapted from mxl-multiviewer src/media/cuda_compose.cu (HostMemory; Apache-2.0, same author).
#pragma once

#include <cstddef>
#include <unordered_map>
#include <unordered_set>

namespace fxeng
{
class HostRegistry
{
public:
    HostRegistry() = default;
    ~HostRegistry();
    HostRegistry(HostRegistry const&) = delete;
    HostRegistry& operator=(HostRegistry const&) = delete;

    // True when [ptr, ptr + bytes) is page-locked (now or before). readOnly for reader mappings.
    bool ensure(void const* ptr, std::size_t bytes, bool readOnly);
    // Unlocks everything. Every copy that uses the memory must be finished.
    void release();
    [[nodiscard]] std::size_t lockedCount() const
    {
        return locked_.size();
    }
    [[nodiscard]] std::size_t refusedCount() const
    {
        return refused_.size();
    }

private:
    std::unordered_map<void const*, std::size_t> locked_;
    std::unordered_set<void const*> refused_;
};
} // namespace fxeng
