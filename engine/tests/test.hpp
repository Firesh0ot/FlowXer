// A minimal test runner: TEST(name) { CHECK(cond); CHECK_EQ(a, b); }
#pragma once

#include <functional>
#include <iostream>
#include <string>
#include <vector>

namespace fxtest
{
struct Case
{
    std::string name;
    std::function<void()> body;
};

inline std::vector<Case>& cases()
{
    static std::vector<Case> all;
    return all;
}

inline int& failures()
{
    static int count = 0;
    return count;
}

struct Register
{
    Register(char const* name, std::function<void()> body)
    {
        cases().push_back(Case{name, std::move(body)});
    }
};

inline void fail(char const* file, int line, std::string const& what)
{
    ++failures();
    std::cerr << file << ":" << line << ": FAILED " << what << "\n";
}
} // namespace fxtest

#define FXTEST_CAT2(a, b) a##b
#define FXTEST_CAT(a, b) FXTEST_CAT2(a, b)
#define TEST(name)                                                                                                                                             \
    static void name();                                                                                                                                        \
    static fxtest::Register FXTEST_CAT(reg_, name)(#name, name);                                                                                               \
    static void name()
#define CHECK(cond)                                                                                                                                            \
    do                                                                                                                                                         \
    {                                                                                                                                                          \
        if (!(cond))                                                                                                                                           \
        {                                                                                                                                                      \
            fxtest::fail(__FILE__, __LINE__, #cond);                                                                                                           \
        }                                                                                                                                                      \
    } while (0)
#define CHECK_EQ(a, b)                                                                                                                                         \
    do                                                                                                                                                         \
    {                                                                                                                                                          \
        auto const fxtest_a = (a);                                                                                                                             \
        auto const fxtest_b = (b);                                                                                                                             \
        if (!(fxtest_a == fxtest_b))                                                                                                                           \
        {                                                                                                                                                      \
            fxtest::fail(__FILE__, __LINE__, std::string(#a " == " #b " (") + std::to_string(fxtest_a) + " vs " + std::to_string(fxtest_b) + ")");             \
        }                                                                                                                                                      \
    } while (0)
