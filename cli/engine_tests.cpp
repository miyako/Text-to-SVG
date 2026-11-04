#include "outline.hpp"
#include <cmath>
#include <iostream>

namespace {
void check(bool value) {
    if (!value) throw std::runtime_error("engine assertion failed");
}
void rejected(const std::string& input, const std::string& code) {
    try {
        textsvg::parse_request(input);
    } catch (const textsvg::Error& error) {
        check(error.code == code);
        return;
    }
    throw std::runtime_error("expected rejection");
}
}

int main() {
    try {
        textsvg::Path q;
        q.move({0, 0}); q.quadratic({5, 10}, {10, 0}); q.close();
        check(std::abs(q.bounds()["height"].get<double>() - 5) < 1e-12);
        check(q.data().find(" Q") != std::string::npos && q.data().back() == 'Z');
        textsvg::Path c;
        c.move({0, 0}); c.cubic({0, 10}, {10, 10}, {10, 0}); c.close();
        check(std::abs(c.bounds()["height"].get<double>() - 7.5) < 1e-12);
        check(c.bounds()["width"] == 10);
        textsvg::Path negative;
        negative.move({0, 0}); negative.cubic({0, -10}, {10, -10}, {10, 0}); negative.close();
        check(std::abs(negative.bounds()["y"].get<double>() + 7.5) < 1e-12);
        textsvg::Path linear_cubic;
        linear_cubic.move({-10, 2}); linear_cubic.cubic({-5, 2}, {0, 2}, {5, 2}); linear_cubic.close();
        check(linear_cubic.bounds()["x"] == -10 && linear_cubic.bounds()["width"] == 15);
        textsvg::Path empty;
        check(empty.bounds().is_null() && empty.data().empty());
        const auto request = textsvg::parse_request(
            R"({"protocol":1,"fontFile":"a.ttf","text":"café","fontSize":12,"units":"mm"})");
        check(request.face_index == 0 && request.text == "café");
        rejected(R"({"protocol":1,"protocol":1})", "invalid_json");
        rejected(R"({"protocol":1,"unknown":0})", "invalid_request");
        rejected(R"({"protocol":true})", "invalid_request");
        rejected(R"({"protocol":1,"text":{"nested":true}})", "invalid_request");
        rejected(R"({"protocol":1e9999})", "invalid_json");
        rejected(R"({"protocol":1,"text":"\ud800"})", "invalid_json");
        rejected(std::string(20, '[') + std::string(20, ']'), "invalid_json");
        auto json = textsvg::Json{{"protocol", 1}, {"fontFile", "a.ttf"},
            {"text", "abc"}, {"fontSize", 12}, {"units", "pt"}};
        for (const auto* value : {"\n", "\t", "\u200d", "\ufe0f", "שלום", "العربية", "Latin日本"}) {
            json["text"] = value;
            rejected(json.dump(), "unsupported_layout");
        }
        for (const auto* value : {"", " ", "日本ひらがなカタカナ", "Ελληνικά", "Кириллица"}) {
            json["text"] = value;
            check(textsvg::parse_request(json.dump()).text == value);
        }
        json["text"] = "A"; json["fontSize"] = 0;
        rejected(json.dump(), "invalid_request");
        json["fontSize"] = true;
        rejected(json.dump(), "invalid_request");
        json["fontSize"] = 1; json["faceIndex"] = -1;
        rejected(json.dump(), "invalid_request");
        json["faceIndex"] = 1.5;
        rejected(json.dump(), "invalid_request");
        json["faceIndex"] = 0; json["text"] = std::string(textsvg::max_text + 1, 'a');
        rejected(json.dump(), "limit_exceeded");
        textsvg::Path bounded;
        bounded.move({0, 0});
        bool output_limit = false;
        try {
            for (std::size_t i = 0; i < textsvg::max_output; ++i)
                bounded.line({1234567890, -1234567890});
        } catch (const textsvg::Error& error) {
            output_limit = error.code == "output_limit";
        }
        check(output_limit && bounded.data().size() < textsvg::max_output / 2);
        bool bad_utf8 = false;
        try { textsvg::decode_utf8("\xc0\x80"); }
        catch (const textsvg::Error&) { bad_utf8 = true; }
        check(bad_utf8);
        std::cout << "outline engine checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
