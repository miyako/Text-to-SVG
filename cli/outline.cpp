#include "outline.hpp"

#include <ft2build.h>
#include FT_FREETYPE_H
#include FT_OUTLINE_H
#include <hb.h>
#include <hb-ot.h>

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <locale>
#include <memory>
#include <set>
#include <sstream>

namespace textsvg {
namespace {
void require(bool condition, const char* message) {
    if (!condition)
        throw Error("invalid_request", message);
}

void finite(double value) {
    if (!std::isfinite(value) || std::abs(value) > 1e12)
        throw Error("limit_exceeded", "nonfinite or excessive outline coordinates");
}

hb_script_t layout(const std::vector<std::uint32_t>& codepoints) {
    hb_script_t family = HB_SCRIPT_INVALID;
    for (auto cp : codepoints) {
        const auto category = hb_unicode_general_category(hb_unicode_funcs_get_default(), cp);
        if (category == HB_UNICODE_GENERAL_CATEGORY_CONTROL ||
            category == HB_UNICODE_GENERAL_CATEGORY_FORMAT ||
            category == HB_UNICODE_GENERAL_CATEGORY_LINE_SEPARATOR ||
            category == HB_UNICODE_GENERAL_CATEGORY_PARAGRAPH_SEPARATOR ||
            category == HB_UNICODE_GENERAL_CATEGORY_UNASSIGNED ||
            category == HB_UNICODE_GENERAL_CATEGORY_PRIVATE_USE ||
            (cp >= 0xfe00 && cp <= 0xfe0f) || (cp >= 0xe0100 && cp <= 0xe01ef) ||
            cp == 0x034f) {
            throw Error("unsupported_layout", "controls, joiners, variation selectors and multiline text are unsupported");
        }
        auto script = hb_unicode_script(hb_unicode_funcs_get_default(), cp);
        if (script == HB_SCRIPT_COMMON || script == HB_SCRIPT_INHERITED)
            continue;
        if (script == HB_SCRIPT_HIRAGANA || script == HB_SCRIPT_KATAKANA)
            script = HB_SCRIPT_HAN;
        if (script != HB_SCRIPT_LATIN && script != HB_SCRIPT_GREEK &&
            script != HB_SCRIPT_CYRILLIC && script != HB_SCRIPT_HAN)
            throw Error("unsupported_layout", "supported strong scripts are Latin, Greek, Cyrillic, or Japanese Han/Hiragana/Katakana");
        if (family != HB_SCRIPT_INVALID && family != script)
            throw Error("unsupported_layout", "mixed strong script families are unsupported");
        family = script;
    }
    return family == HB_SCRIPT_INVALID ? HB_SCRIPT_LATIN : family;
}

struct FreeType {
    FT_Library library = nullptr;
    FreeType() {
        if (FT_Init_FreeType(&library))
            throw Error("internal_error", "FreeType initialization failed");
    }
    ~FreeType() { FT_Done_FreeType(library); }
};

struct Face {
    FT_Face face = nullptr;
    ~Face() { if (face) FT_Done_Face(face); }
};

struct Decompose {
    Path& path;
    double scale, x, y;
    std::exception_ptr error;
    Point position(const FT_Vector* point) const {
        return {(x + static_cast<double>(point->x)) * scale,
                -(y + static_cast<double>(point->y)) * scale};
    }
    template<typename Function>
    int invoke(Function function) noexcept {
        try {
            function();
            return 0;
        } catch (...) {
            error = std::current_exception();
            return 1;
        }
    }
};

int move_to(const FT_Vector* point, void* user) {
    auto& d = *static_cast<Decompose*>(user);
    return d.invoke([&] { d.path.move(d.position(point)); });
}
int line_to(const FT_Vector* point, void* user) {
    auto& d = *static_cast<Decompose*>(user);
    return d.invoke([&] { d.path.line(d.position(point)); });
}
int conic_to(const FT_Vector* control, const FT_Vector* point, void* user) {
    auto& d = *static_cast<Decompose*>(user);
    return d.invoke([&] { d.path.quadratic(d.position(control), d.position(point)); });
}
int cubic_to(const FT_Vector* control1, const FT_Vector* control2, const FT_Vector* point, void* user) {
    auto& d = *static_cast<Decompose*>(user);
    return d.invoke([&] { d.path.cubic(d.position(control1), d.position(control2), d.position(point)); });
}

double quad_value(double a, double b, double c, double t) {
    const double s = 1 - t;
    return s * s * a + 2 * s * t * b + t * t * c;
}
double cubic_value(double a, double b, double c, double d, double t) {
    const double s = 1 - t;
    return s * s * s * a + 3 * s * s * t * b + 3 * s * t * t * c + t * t * t * d;
}

std::vector<double> extrema(double a, double b, double c, double d) {
    const double A = -a + 3 * b - 3 * c + d;
    const double B = 2 * (a - 2 * b + c);
    const double C = b - a;
    std::vector<double> roots;
    if (A == 0) {
        if (B != 0) roots.push_back(-C / B);
    } else {
        const double discriminant = B * B - 4 * A * C;
        if (discriminant >= 0) {
            const double q = -0.5 * (B + std::copysign(std::sqrt(discriminant), B));
            if (q == 0)
                roots.push_back(-B / (2 * A));
            else {
                roots.push_back(q / A);
                roots.push_back(C / q);
            }
        }
    }
    return roots;
}
} // namespace

std::vector<std::uint32_t> decode_utf8(std::string_view text) {
    std::vector<std::uint32_t> result;
    for (std::size_t i = 0; i < text.size();) {
        const auto first = static_cast<unsigned char>(text[i++]);
        std::uint32_t cp = first, minimum = 0;
        unsigned int remaining = 0;
        if (first < 0x80) {}
        else if (first >= 0xc2 && first <= 0xdf) {
            cp = first & 0x1f; remaining = 1; minimum = 0x80;
        } else if (first >= 0xe0 && first <= 0xef) {
            cp = first & 0x0f; remaining = 2; minimum = 0x800;
        } else if (first >= 0xf0 && first <= 0xf4) {
            cp = first & 7; remaining = 3; minimum = 0x10000;
        } else {
            throw Error("invalid_request", "input must be valid UTF-8");
        }
        if (remaining > text.size() - i)
            throw Error("invalid_request", "input must be valid UTF-8");
        while (remaining--) {
            const auto next = static_cast<unsigned char>(text[i++]);
            if ((next & 0xc0) != 0x80)
                throw Error("invalid_request", "input must be valid UTF-8");
            cp = (cp << 6) | (next & 0x3f);
        }
        if (cp < minimum || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
            throw Error("invalid_request", "input must be valid UTF-8");
        result.push_back(cp);
    }
    return result;
}

Request parse_request(std::string_view input) {
    if (input.size() > max_input)
        throw Error("limit_exceeded", "stdin exceeds 1 MiB");
    decode_utf8(input);
    std::vector<std::set<std::string>> keys;
    const std::set<std::string> allowed{"protocol", "fontFile", "faceIndex", "text", "fontSize", "units"};
    auto callback = [&](int depth, Json::parse_event_t event, Json& value) {
        if (depth > 16)
            throw Error("invalid_json", "JSON nesting exceeds 16");
        if (event == Json::parse_event_t::object_start && depth != 0)
            throw Error("invalid_request", "request fields must be scalar values");
        if (event == Json::parse_event_t::object_start)
            keys.emplace_back();
        else if (event == Json::parse_event_t::object_end)
            keys.pop_back();
        else if (event == Json::parse_event_t::key) {
            const auto key = value.get<std::string>();
            if (!keys.back().insert(key).second)
                throw Error("invalid_json", "duplicate JSON key");
            if (depth == 1 && allowed.count(key) == 0)
                throw Error("invalid_request", "unknown request key");
        }
        return true;
    };
    Json json;
    try {
        json = Json::parse(input.begin(), input.end(), callback);
    } catch (const Json::exception&) {
        throw Error("invalid_json", "malformed JSON or numeric range");
    }
    require(json.is_object(), "request must be an object");
    for (auto it = json.begin(); it != json.end(); ++it)
        require(allowed.count(it.key()) != 0, "unknown request key");
    require(json.contains("protocol") && json["protocol"].is_number_integer() && json["protocol"] == 1,
            "protocol must be integer 1");
    require(json.contains("fontFile") && json["fontFile"].is_string(), "fontFile must be a string");
    require(json.contains("text") && json["text"].is_string(), "text must be a string");
    require(json.contains("fontSize") && json["fontSize"].is_number(), "fontSize must be a number");
    require(json.contains("units") && json["units"].is_string(), "units must be a string");
    Request request;
    request.font_file = json["fontFile"].get<std::string>();
    require(!request.font_file.empty() && request.font_file.find('\0') == std::string::npos,
            "fontFile must be a nonempty path without NUL");
    require(request.font_file.size() <= 32768, "fontFile path is too long");
    if (json.contains("faceIndex")) {
        require(json["faceIndex"].is_number_integer() && json["faceIndex"] >= 0 &&
                json["faceIndex"] <= 65535, "faceIndex must be an integer from 0 to 65535");
        request.face_index = json["faceIndex"].get<unsigned int>();
    }
    request.text = json["text"].get<std::string>();
    if (request.text.size() > max_text)
        throw Error("limit_exceeded", "text exceeds 16384 UTF-8 bytes");
    request.font_size = json["fontSize"].get<double>();
    require(std::isfinite(request.font_size) && request.font_size > 0 && request.font_size <= 10000,
            "fontSize must be finite, positive and at most 10000");
    request.units = json["units"].get<std::string>();
    require(request.units == "mm" || request.units == "pt" || request.units == "px" || request.units == "in",
            "units must be mm, pt, px or in");
    layout(decode_utf8(request.text));
    return request;
}

std::string number(double value) {
    finite(value);
    if (value == 0) return "0";
    std::ostringstream stream;
    stream.imbue(std::locale::classic());
    stream << std::setprecision(std::numeric_limits<double>::max_digits10) << value;
    return stream.str();
}

void Path::append(std::string_view value) {
    // Both pathData and SVG contain this ASCII-only path; reserve envelope headroom.
    if (data_.size() + value.size() > (max_output - 4096) / 2)
        throw Error("output_limit", "outline path exceeds the 16 MiB output budget");
    data_.append(value);
}
void Path::point(Point p) {
    append(number(p.x)); append(" "); append(number(p.y));
}
void Path::include(Point p) {
    finite(p.x); finite(p.y);
    if (!ink_) {
        min_ = max_ = p; ink_ = true;
    } else {
        min_.x = std::min(min_.x, p.x); min_.y = std::min(min_.y, p.y);
        max_.x = std::max(max_.x, p.x); max_.y = std::max(max_.y, p.y);
    }
}
void Path::move(Point p) {
    close();
    if (!data_.empty()) append(" ");
    append("M"); point(p);
    current_ = start_ = p; open_ = true;
}
void Path::line(Point p) {
    include(current_); include(p);
    append(" L"); point(p); current_ = p;
}
void Path::quadratic(Point control, Point end) {
    include(current_); include(end);
    for (int axis = 0; axis < 2; ++axis) {
        const double a = axis ? current_.y : current_.x;
        const double b = axis ? control.y : control.x;
        const double c = axis ? end.y : end.x;
        const double denominator = a - 2 * b + c;
        if (denominator != 0) {
            const double t = (a - b) / denominator;
            if (t > 0 && t < 1)
                include({quad_value(current_.x, control.x, end.x, t),
                         quad_value(current_.y, control.y, end.y, t)});
        }
    }
    append(" Q"); point(control); append(" "); point(end); current_ = end;
}
void Path::cubic(Point c1, Point c2, Point end) {
    include(current_); include(end);
    for (int axis = 0; axis < 2; ++axis) {
        const auto roots = axis ? extrema(current_.y, c1.y, c2.y, end.y) :
                                  extrema(current_.x, c1.x, c2.x, end.x);
        for (const double t : roots) {
            if (t > 0 && t < 1)
                include({cubic_value(current_.x, c1.x, c2.x, end.x, t),
                         cubic_value(current_.y, c1.y, c2.y, end.y, t)});
        }
    }
    append(" C"); point(c1); append(" "); point(c2); append(" "); point(end); current_ = end;
}
void Path::close() {
    if (open_) {
        // A move-only contour has no ink, while a closing edge shares segment extrema.
        append(" Z"); current_ = start_; open_ = false;
    }
}
Json Path::bounds() const {
    if (!ink_) return nullptr;
    return Json{{"x", min_.x}, {"y", min_.y},
                {"width", max_.x - min_.x}, {"height", max_.y - min_.y}};
}

Json convert(const Request& request) {
    // Validate even for callers of the common engine that bypass the CLI parser.
    const auto checked = parse_request(Json{{"protocol", 1}, {"fontFile", request.font_file},
        {"faceIndex", request.face_index}, {"text", request.text}, {"fontSize", request.font_size},
        {"units", request.units}}.dump());
    const auto script = layout(decode_utf8(checked.text));
    std::vector<unsigned char> bytes;
    try {
        const auto path = std::filesystem::u8path(checked.font_file);
        if (!std::filesystem::is_regular_file(path))
            throw Error("invalid_font", "fontFile must be a readable regular file");
        std::ifstream input(path, std::ios::binary | std::ios::ate);
        if (!input) throw Error("invalid_font", "cannot open fontFile");
        const auto size = input.tellg();
        if (size <= 0) throw Error("invalid_font", "fontFile is empty or unreadable");
        if (size > static_cast<std::streamoff>(max_font))
            throw Error("limit_exceeded", "fontFile exceeds 64 MiB");
        bytes.resize(static_cast<std::size_t>(size));
        input.seekg(0);
        if (!input.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size())))
            throw Error("invalid_font", "cannot read fontFile");
        if (input.peek() != std::char_traits<char>::eof())
            throw Error("invalid_font", "fontFile changed while reading");
    } catch (const std::filesystem::filesystem_error&) {
        throw Error("invalid_font", "cannot access fontFile");
    }
    if (bytes.size() < 4 ||
        !(std::equal(bytes.begin(), bytes.begin() + 4, "\0\1\0\0") ||
          std::equal(bytes.begin(), bytes.begin() + 4, "OTTO") ||
          std::equal(bytes.begin(), bytes.begin() + 4, "ttcf") ||
          std::equal(bytes.begin(), bytes.begin() + 4, "true")))
        throw Error("invalid_font", "requires an uncompressed TrueType/OpenType font or collection");
    FreeType ft;
    Face face;
    if (FT_New_Memory_Face(ft.library, bytes.data(), static_cast<FT_Long>(bytes.size()), 0, &face.face))
        throw Error("invalid_font", "FreeType could not open the font");
    if (checked.face_index >= static_cast<unsigned long>(face.face->num_faces))
        throw Error("invalid_font", "faceIndex is outside the font collection");
    if (checked.face_index != 0) {
        FT_Done_Face(face.face); face.face = nullptr;
        if (FT_New_Memory_Face(ft.library, bytes.data(), static_cast<FT_Long>(bytes.size()),
                              checked.face_index, &face.face))
            throw Error("invalid_font", "FreeType could not open the selected face");
    }
    if (!FT_IS_SFNT(face.face) || !FT_IS_SCALABLE(face.face) || !face.face->units_per_EM ||
        FT_IS_TRICKY(face.face) || FT_HAS_COLOR(face.face) || FT_HAS_SVG(face.face) ||
        FT_HAS_MULTIPLE_MASTERS(face.face))
        throw Error("invalid_font", "requires static scalable TrueType/OpenType without color, SVG, tricky hinting or variations");
    if (FT_Select_Charmap(face.face, FT_ENCODING_UNICODE))
        throw Error("invalid_font", "font has no Unicode character map");
    // CID CFF can force scaling even under NO_SCALE when subfont UPEMs differ.
    // Identity size metrics keep that correction in font units, never 26.6 pixels.
    FT_Size_RequestRec size_request{FT_SIZE_REQUEST_TYPE_SCALES, 0x10000L, 0x10000L, 0, 0};
    if (FT_Request_Size(face.face, &size_request))
        throw Error("invalid_font", "cannot establish unhinted font-unit metrics");

    using Blob = std::unique_ptr<hb_blob_t, decltype(&hb_blob_destroy)>;
    using HbFace = std::unique_ptr<hb_face_t, decltype(&hb_face_destroy)>;
    using Font = std::unique_ptr<hb_font_t, decltype(&hb_font_destroy)>;
    using Buffer = std::unique_ptr<hb_buffer_t, decltype(&hb_buffer_destroy)>;
    Blob blob(hb_blob_create(reinterpret_cast<const char*>(bytes.data()), static_cast<unsigned int>(bytes.size()),
                            HB_MEMORY_MODE_READONLY, nullptr, nullptr), hb_blob_destroy);
    HbFace hbface(hb_face_create(blob.get(), checked.face_index), hb_face_destroy);
    Font font(hb_font_create(hbface.get()), hb_font_destroy);
    Buffer buffer(hb_buffer_create(), hb_buffer_destroy);
    if (!hb_blob_get_length(blob.get()) || !hb_face_get_glyph_count(hbface.get()) ||
        !hb_buffer_allocation_successful(buffer.get()) || hb_face_get_upem(hbface.get()) != face.face->units_per_EM)
        throw Error("invalid_font", "invalid or inconsistent OpenType face");
    const int upem = face.face->units_per_EM;
    hb_ot_font_set_funcs(font.get());
    hb_font_set_scale(font.get(), upem, upem);
    hb_buffer_set_direction(buffer.get(), HB_DIRECTION_LTR);
    hb_buffer_set_script(buffer.get(), script);
    hb_buffer_set_language(buffer.get(), hb_language_from_string(script == HB_SCRIPT_HAN ? "ja" : "und", -1));
    hb_buffer_set_flags(buffer.get(), HB_BUFFER_FLAG_DO_NOT_INSERT_DOTTED_CIRCLE);
    hb_buffer_add_utf8(buffer.get(), checked.text.data(), static_cast<int>(checked.text.size()), 0,
                       static_cast<int>(checked.text.size()));
    if (!hb_shape_full(font.get(), buffer.get(), nullptr, 0, nullptr) ||
        !hb_buffer_allocation_successful(buffer.get()))
        throw Error("invalid_font", "OpenType shaping failed");
    unsigned int count = 0;
    const auto* infos = hb_buffer_get_glyph_infos(buffer.get(), &count);
    const auto* positions = hb_buffer_get_glyph_positions(buffer.get(), nullptr);
    if (count > max_glyphs)
        throw Error("limit_exceeded", "shaped glyph count exceeds 32768");
    // HarfBuzz can hide missing default-ignorable characters; reject them in layout validation.
    for (auto cp : decode_utf8(checked.text)) {
        hb_codepoint_t glyph = 0;
        if (!hb_font_get_nominal_glyph(font.get(), cp, &glyph) || glyph == 0)
            throw Error("missing_glyph", "font does not cover every input character");
    }
    Path path;
    double pen_x = 0, pen_y = 0;
    const double scale = checked.font_size / upem;
    if (scale == 0)
        throw Error("limit_exceeded", "fontSize is too small for finite outline scaling");
    for (unsigned int i = 0; i < count; ++i) {
        if (infos[i].codepoint == 0)
            throw Error("missing_glyph", "shaping produced a missing glyph");
        const FT_Int32 flags = FT_LOAD_NO_SCALE | FT_LOAD_NO_HINTING | FT_LOAD_NO_BITMAP | FT_LOAD_NO_SVG;
        if (FT_Load_Glyph(face.face, infos[i].codepoint, flags))
            throw Error("invalid_font", "cannot load a shaped glyph outline");
        const auto slot = face.face->glyph;
        if (slot->format != FT_GLYPH_FORMAT_OUTLINE)
            throw Error("unsupported_glyph", "glyph is not a scalable outline");
        if (FT_Outline_Check(&slot->outline))
            throw Error("invalid_font", "invalid glyph outline");
        if (slot->outline.n_points != 0) {
            Decompose decomposition{path, scale, pen_x + positions[i].x_offset,
                                     pen_y + positions[i].y_offset, nullptr};
            const FT_Outline_Funcs funcs{move_to, line_to, conic_to, cubic_to, 0, 0};
            const auto error = FT_Outline_Decompose(&slot->outline, &funcs, &decomposition);
            if (decomposition.error) std::rethrow_exception(decomposition.error);
            if (error) throw Error("invalid_font", "cannot decompose glyph outline");
            path.close();
        }
        pen_x += positions[i].x_advance;
        pen_y += positions[i].y_advance;
        finite(pen_x * scale); finite(pen_y * scale);
    }
    const auto bounds = path.bounds();
    double x = 0, y = 0, width = std::max(pen_x * scale, checked.font_size), height = checked.font_size;
    if (!bounds.is_null()) {
        x = bounds["x"].get<double>(); y = bounds["y"].get<double>();
        width = bounds["width"].get<double>(); height = bounds["height"].get<double>();
        if (width <= 0 || height <= 0)
            throw Error("unsupported_glyph", "outline has degenerate ink bounds");
    }
    const std::string svg = "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"" + number(width) +
        checked.units + "\" height=\"" + number(height) + checked.units + "\" viewBox=\"" +
        number(x) + " " + number(y) + " " + number(width) + " " + number(height) +
        "\"><path fill-rule=\"nonzero\" d=\"" + path.data() + "\"/></svg>";
    return Json{{"protocol", 1}, {"ok", true}, {"svg", svg}, {"pathData", path.data()},
                {"inkBounds", bounds}, {"advance", {{"x", pen_x * scale}, {"y", -pen_y * scale}}},
                {"units", checked.units}, {"fontSize", checked.font_size}, {"glyphCount", count}};
}
} // namespace textsvg
