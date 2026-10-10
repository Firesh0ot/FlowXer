#include "test.hpp"

int main()
{
    for (auto const& test : fxtest::cases())
    {
        int const before = fxtest::failures();
        test.body();
        std::cout << (fxtest::failures() == before ? "ok   " : "FAIL ") << test.name << "\n";
    }
    std::cout << fxtest::cases().size() << " tests, " << fxtest::failures() << " failed checks\n";
    return fxtest::failures() == 0 ? 0 : 1;
}
