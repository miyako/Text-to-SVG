#include "outline.hpp"
#include <array>
#include <cstdio>
#include <iostream>
#include <string_view>
#include <vector>

#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#endif

namespace {
int failure(const std::string& code, const std::string& message) {
    std::cerr << textsvg::Json{{"protocol", 1}, {"error", {{"code", code}, {"message", message}}}}.dump() << '\n';
    return 2;
}

int run(int argc, char* argv[]) {
    try {
        for (int i = 1; i < argc; ++i)
            textsvg::decode_utf8(argv[i]);
        if (argc == 2 && std::string_view(argv[1]) == "--version") {
            std::cout << "{\"protocol\":1,\"version\":\"" TEXTSVG_VERSION "\"}\n";
            return 0;
        }
        if (argc == 3 && std::string_view(argv[1]) == "--echo") {
            std::string output = textsvg::Json{{"protocol", 1}, {"argument", argv[2]}}.dump();
            if (output.size() + 1 > textsvg::max_output)
                throw textsvg::Error("output_limit", "output exceeds 16 MiB");
            std::cout << output << '\n';
            return 0;
        }
        if (argc != 2 || std::string_view(argv[1]) != "convert")
            return failure("invalid_arguments", "expected convert, --echo <literal>, or --version");
        std::string input;
        std::array<char, 8192> chunk{};
        while (std::cin) {
            std::cin.read(chunk.data(), static_cast<std::streamsize>(chunk.size()));
            const auto count = static_cast<std::size_t>(std::cin.gcount());
            if (count > textsvg::max_input - input.size())
                throw textsvg::Error("limit_exceeded", "stdin exceeds 1 MiB");
            input.append(chunk.data(), count);
        }
        if (!std::cin.eof())
            throw textsvg::Error("invalid_request", "failed to read stdin");
        const auto result = textsvg::convert(textsvg::parse_request(input));
        const std::string output = result.dump();
        if (output.size() + 1 > textsvg::max_output)
            throw textsvg::Error("output_limit", "output exceeds 16 MiB");
        std::cout << output << '\n';
        return 0;
    } catch (const textsvg::Error& error) {
        return failure(error.code, error.what());
    } catch (const nlohmann::json::exception&) {
        return failure("invalid_json", "invalid JSON value or numeric range");
    } catch (const std::bad_alloc&) {
        return failure("limit_exceeded", "memory allocation failed");
    } catch (const std::exception&) {
        return failure("internal_error", "conversion failed");
    }
}
}

#ifdef _WIN32
int wmain(int argc, wchar_t* argv[]) {
    if (_setmode(_fileno(stdin), _O_BINARY) == -1 ||
        _setmode(_fileno(stdout), _O_BINARY) == -1 ||
        _setmode(_fileno(stderr), _O_BINARY) == -1)
        return failure("internal_error", "failed to configure binary streams");
    std::vector<std::string> arguments;
    arguments.reserve(argc);
    for (int i = 0; i < argc; ++i) {
        const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
            argv[i], -1, nullptr, 0, nullptr, nullptr);
        if (size == 0)
            return failure("invalid_arguments", "arguments must be valid Unicode");
        std::string argument(size, '\0');
        if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
            argv[i], -1, argument.data(), size, nullptr, nullptr) != size)
            return failure("invalid_arguments", "arguments must be valid Unicode");
        argument.pop_back();
        arguments.push_back(std::move(argument));
    }
    std::vector<char*> pointers;
    pointers.reserve(argc);
    for (auto& argument : arguments)
        pointers.push_back(argument.data());
    return run(argc, pointers.data());
}
#else
int main(int argc, char* argv[]) {
    return run(argc, argv);
}
#endif
