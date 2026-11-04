#pragma once

#include <nlohmann/json.hpp>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace textsvg {
using Json = nlohmann::ordered_json;
constexpr std::size_t max_input = 1024 * 1024;
constexpr std::size_t max_text = 16384;
constexpr std::size_t max_font = 64 * 1024 * 1024;
constexpr std::size_t max_glyphs = 32768;
constexpr std::size_t max_output = 16 * 1024 * 1024;

struct Error : std::runtime_error {
    std::string code;
    Error(std::string code, const std::string& message)
        : std::runtime_error(message), code(std::move(code)) {}
};

struct Request {
    std::string font_file;
    unsigned int face_index = 0;
    std::string text;
    double font_size = 0;
    std::string units;
};

std::vector<std::uint32_t> decode_utf8(std::string_view text);
Request parse_request(std::string_view input);
Json convert(const Request& request);

struct Point {
    double x, y;
};

// Shared by the FreeType callbacks and the curve-extrema unit tests.
class Path {
public:
    void move(Point point);
    void line(Point point);
    void quadratic(Point control, Point end);
    void cubic(Point control1, Point control2, Point end);
    void close();
    Json bounds() const;
    const std::string& data() const { return data_; }
private:
    void include(Point point);
    void append(std::string_view value);
    void point(Point value);
    std::string data_;
    Point current_{0, 0}, start_{0, 0}, min_{0, 0}, max_{0, 0};
    bool open_ = false, ink_ = false;
};
std::string number(double value);
} // namespace textsvg
