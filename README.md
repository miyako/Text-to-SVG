# Text-to-SVG

A real **4D 20.7 LTS component** wrapping a self-contained C++17 executable.
FreeType extracts scalable TrueType/OpenType outlines and HarfBuzz shapes text.
The output is standalone SVG containing filled paths, not embedded fonts, text,
bitmaps, raster tracing, centerlines or G-code. No service or Node.js is used.

| Identity | Value |
| --- | --- |
| Repository / component / project / package | `Text-to-SVG` |
| Independent host namespace | `TextToSVG` |
| Public class | `TextOutline` |
| Bundled executable | `text-to-svg` |
| Intended native platforms | macOS universal ARM64 + x86_64; Windows x64 |

This destination repository was adopted from
[miyako/4d-component-template](https://github.com/miyako/4d-component-template).
Neither upstream template is modified. Unsigned local acceptance and explicitly
authorized signed-candidate verification do **not** authorize publication, tags,
visibility changes or merging to the default branch.
See [AGENTS.md](AGENTS.md) for the adoption and acceptance gates.

## 4D quickstart

Install the unpacked compiled `Text-to-SVG.4dbase` in the host's `Components`
folder. A released dependency-manager asset, if publication is later approved,
must be named exactly `Text-to-SVG.zip`. No published asset is implied here.

```4d
var $outline : cs.TextToSVG.TextOutline
var $request; $result : Object
var $font; $output : 4D.File
var $target : Text
$target:="macOS-universal"
If (Is Windows)
    $target:="Windows-x64"
End if
$font:=File("/DATA/MyFont.otf")
$output:=File("/DATA/lettering.svg")
$outline:=cs.TextToSVG.TextOutline.new($target)
$request:=New object("fontFile"; $font.platformPath; "faceIndex"; 0)
$request.text:="Office café"
$request.fontSize:=10
$request.units:="mm"
$result:=$outline.convert($request)
If ($result.ok)
    $output.setText($result.value.svg)
Else
    ALERT($result.error.code+": "+$result.error.message)
End if
```

The font path is explicit and absolute. Use your own licensed scalable font;
there is no font-name lookup, fallback or substitution. Do not write an empty
stdout/error response as SVG. Calls are synchronous, have a finite 30-second
process timeout, close stdin and retain stdout, stderr, exit and system errors
separately. A nonempty stderr alone is not a process failure.

Detailed contracts:
[TextOutline](component/Text-to-SVG/Documentation/Classes/TextOutline.md),
[_CLI](component/Text-to-SVG/Documentation/Classes/_CLI.md),
[_Process](component/Text-to-SVG/Documentation/Classes/_Process.md).
Underscore classes are internal by convention, not a 4D access-control boundary.

## Layout and geometry

Initial scope is **one horizontal left-to-right line**. Kerning, standard
ligatures and combining marks are shaped by HarfBuzz. Japanese
Han/Hiragana/Katakana form one supported script family when the selected font
has the requested glyphs. HarfBuzz is not paragraph or Unicode bidi layout:
right-to-left scripts, directional controls and unsupported mixed-script input
are rejected, not silently guessed. There is no vertical layout or wrapping.
Newlines, tabs and unsupported controls are errors.

`fontSize` is the **em size in the selected unit**, not cap height or visible
ink height. Supported units are `mm`, `pt` (1/72 inch), `px` (1/96 inch) and
`in`. For example 10 mm, 28.3464566929134 pt and 37.7952755905512 px describe
the same physical em size. Numeric path coordinates are in the selected unit.

The initial pen/baseline is `(0,0)`, with SVG X right and Y down. Path data
already includes glyph offsets/advances and Y inversion; the SVG uses it
without a second transform. Contours use `M/L/Q/C/Z` and nonzero filling to
preserve counters/holes. Hinting and bitmap loading are disabled. Visible bounds
include curve extrema, not just endpoints or the control box.

`inkBounds` is `{x,y,width,height}` in these baseline-relative coordinates, or
`null` when there is no visible ink. `advance:{x,y}` is the layout pen movement,
not the ink width. A space can advance without ink. Empty text is valid, with
empty path data, null ink and zero advance. Whitespace-only text is valid.
For ink, SVG `viewBox` is the tight ink rectangle; without ink it is
`0 0 max(advance.x,fontSize) fontSize`, so neither dimension is zero.
SVG physical `width` and `height` carry the selected unit.

Static scalable TrueType and CFF outlines in uncompressed SFNT containers are
supported. `faceIndex` selects a zero-based face in a collection. Variable,
color/SVG and tricky-hinting fonts, WOFF/compressed containers, bitmap-only or
non-outline glyphs, missing glyphs and invalid faces are explicit failures.
Fonts are untrusted parser input; keep dependencies updated deliberately.

## CLI protocol 1

```sh
printf '%s' '{"protocol":1,"fontFile":"/absolute/font.ttf","faceIndex":0,"text":"AV Office","fontSize":10,"units":"mm"}' \
  | ./build/macos-arm64/text-to-svg convert > lettering.json
./build/macos-arm64/text-to-svg --version
```

`convert` takes one UTF-8 JSON object on stdin, terminated by EOF. The 4D wrapper
adds protocol version 1. Required fields: `fontFile`, `text`, `fontSize`, `units`;
optional `faceIndex` defaults to 0. Unknown fields and duplicate JSON keys are
rejected. No shell is invoked.

Success exits 0 with one JSON object plus LF on stdout:

```json
{"protocol":1,"ok":true,"svg":"<svg ...>...</svg>","pathData":"M...Z","inkBounds":{"x":0,"y":-7,"width":12,"height":7},"advance":{"x":13,"y":0},"units":"mm","fontSize":10,"glyphCount":2}
```

The numbers above illustrate the schema, not a font-independent vector.
Failure exits 2, leaves stdout empty and emits a protocol-1
`{"protocol":1,"error":{"code":"...","message":"..."}}` diagnostic plus LF on
stderr. `--version` returns `{"protocol":1,"version":"0.1.0"}` plus LF.
`--echo <literal>` is a diagnostic argument-roundtrip command used by native
4D host tests; it returns `{"protocol":1,"argument":"..."}`, not SVG.

Limits: request 1 MiB, text 16,384 UTF-8 bytes, font file 64 MiB, font size
positive finite and at most 10,000 selected units, 32,768 shaped glyphs, SVG
output 16 MiB. Malformed UTF-8/JSON, oversize input, unsafe layout and invalid
geometry fail explicitly. The subprocess isolates font/shaping work from 4D.
No asynchronous or concurrent shared engine state is installed in the host.

| CLI error code | Meaning |
| --- | --- |
| `invalid_arguments` | Unsupported command or argument count |
| `invalid_json` | Malformed/duplicate/deep JSON or numeric parse overflow |
| `invalid_request` | Invalid field, type, value or UTF-8 |
| `unsupported_layout` | Unsupported script family, control or layout |
| `invalid_font` | Unreadable/invalid font, unsupported format or face |
| `missing_glyph` | Input or shaped glyph is absent; no substitution |
| `unsupported_glyph` | Non-outline or degenerate ink geometry |
| `limit_exceeded` | Input, font, text, glyph or numeric resource limit |
| `output_limit` | Path/output budget reached before unbounded growth |
| `internal_error` | Unexpected engine/system exception |

## Build and local acceptance

Only C++ is retained. CMake dependency definitions pin FreeType, HarfBuzz and
the JSON parser to immutable downloads/checksums. They build static libraries;
macOS uses OS system runtimes and Windows selects the static MSVC CRT.
Native C++ builds inspect actual Mach-O/PE imports and reject unbundled
third-party or dynamic MSVC runtime dependencies; import inspection is not
Windows execution or Windows 4D evidence.
Configuration needs network access to acquire pinned source dependencies, but
conversion never downloads anything. Fixture fonts are licensed test inputs,
not runtime font fallback or bundled component fonts.

```sh
python3 scripts/doctor.py --root .
python3 scripts/build_cli.py --root . --target macos-arm64
python3 -m unittest discover -s tests -v
python3 scripts/generate_icons.py --check
```

Choose the **actual native target** (`macos-arm64`, `macos-x64`,
`windows-x64`). C++ cross mode is intentionally unsupported by the template.
Obtain all three binaries and their manifests from native builds of the same
revision/source inputs before resource assembly. Never mix stale artifacts or
label Rosetta/cross-compilation as native Intel/Windows execution.

```sh
python3 scripts/ci_config.py assemble --root .
python3 scripts/ci_config.py download-tool4d --root .
python3 scripts/test_4d.py --root . --tool4d .tools/tool4d.app/Contents/MacOS/tool4d
python3 scripts/compile_component.py --root . --tool4d .tools/tool4d.app/Contents/MacOS/tool4d
python3 scripts/package_component.py --root . --repository miyako/Text-to-SVG \
  --tag v0.1.0 --unpack-test
python3 scripts/test_4d.py --root . --tool4d .tools/tool4d.app/Contents/MacOS/tool4d \
  --component build/release-test/Text-to-SVG.4dbase
```

The `--tag` above validates identity only; it creates/pushes no tag.
Free unsigned local tool4d compilation needs no activation or signing secrets.
The approved compiler is **20.7 build 101905, macOS ARM64**, downloaded from
the stable URL in `package.json.tool`:
`https://resources-download.4d.com/release/20.x/20.7/101905/mac/tool4d_arm64.tar.xz`.
The vendor currently redirects to its exact HTTPS `builds2.4d.com` CDN hostname.
Every redirect hop is origin-checked, the immutable archive SHA256 remains
mandatory, and extraction rejects unsafe paths. Expiring signed CDN URLs are
not configuration inputs.

When all required artifacts are available,
`scripts/accept_local.py --root . --repository miyako/Text-to-SVG --tool4d PATH`
records the full pipeline. A compiled package must be loaded from the **actual
unpacked source-stripped ZIP** with `Is compiled mode=True`. Fresh compilation
and generated completion metadata alone do not prove package loading.

Artifacts/evidence: `build/acceptance.json`, `build/source-host-status.json`,
`build/compile-status.json`, `build/compiled-host-status.json`, and, only after
the package gates succeed, `dist/Text-to-SVG.zip` with adjacent
`dist/Text-to-SVG.acceptance.json`. A failed attempt does not approve an old ZIP.
`passed`, `failed` and `not_run` are distinct. Partial local acceptance is
**not release ready**. Native Intel/Windows CLI and 4D execution,
dependency-manager download/install and optional signing require separate
evidence. No publication workflow should be dispatched before user review.

## Signed candidate and release gates

The signed lifecycle is now implemented, not merely configured through secrets.
`release-policy.json` requires Apple notarization for version `0.1.0`; the release
workflow also explicitly requires signing, so there is no unsigned fallback.
Unsigned `accept_local.py` remains available for development and preserves its
honest partial-platform result.

The non-publishing candidate workflow runs only on an authorized push to
`miyako-text-to-svg-implementation` whose commit message contains
`[signed-candidate]`. It does not need a workflow merged to `main`, creates no
tag/release, and has read-only repository permissions. Pull requests, forks and
other branches cannot reach its secret-bearing job. Candidate and tag-release
callers share `.github/workflows/signed-build.yml`, including the same native
matrix and same-run manifest/artifact handoff.

Exactly six Actions secrets are required: `APPLE_DEVELOPER_ID_CERTIFICATE`
(base64 Developer ID Application P12 with private key),
`APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD`, `KEYCHAIN_PASSWORD`,
`NOTARYTOOL_APPLE_ID`, `NOTARYTOOL_PASSWORD` (app-specific), and
`NOTARYTOOL_TEAM_ID`. The signing identity is derived from the imported
certificate and checked against the configured team. There is no
`CODESIGN_APPLE_ID` secret or normal Apple account password.

The pipeline validates all three native build manifests, assembles Resources,
runs the source host, requires clean structured compilation, and host-tests the
unsigned compiled ZIP. It then signs an isolated extracted copy: the universal
CLI and actual Mach-O component Libraries, not `.4DZ` data or arbitrary
Resources. Developer ID, team, hardened runtime and secure timestamps are
verified. Certificates/keychains live only in isolated runner-temporary storage,
with `finally` cleanup and an independent `always()` workflow cleanup.

A signed DMG is submitted to Apple; only explicit notarization `Accepted`
permits stapling and staple validation. Rejection diagnostics are retained with
credentials redacted. Signature/Gatekeeper checks apply to both payload and
container. The final dependency-manager ZIP is produced from the actual mounted,
accepted/stapled DMG; **ZIP files cannot be stapled**. Pre-sign compiler and
native provenance is preserved, while final-byte hashes are regenerated after
signing/stapling.

Finally the exact signed ZIP is unpacked and loaded by real tool4d with compiled
mode required. `release_assets.py` checks the resulting adjacent evidence and
every delivered byte before artifact upload or future publication. Outputs are
`Text-to-SVG.zip`, `Text-to-SVG.dmg`, `Text-to-SVG-signing.json` and
`Text-to-SVG.acceptance.json`. A signed candidate is not a published release:
native Intel/Windows 4D, published dependency-manager installation and Windows
Authenticode remain separate observations. Publication still needs explicit user
approval, an existing matching immutable tag and the release opt-in gate.

## Licensing

Scaffold and original wrapper code: [MIT](LICENSE). Keep the original copyright
notice. FreeType, HarfBuzz and JSON parser redistribution notices are included
under `component/Text-to-SVG/Resources/licenses/`; fixture fonts have their own
licenses/provenance under `tests/fonts/`. Review all relevant licenses when
changing dependencies; notice file presence is not a legal audit.
4D/tool4d is separately licensed vendor software and is never bundled.
The signing lifecycle reuses/adapts MIT implementation patterns from
`miyako/4d-component-template` signed-lifecycle revision
`96da0b4a0f628536b906855a03f2a54ec6cdb002` and `miyako/hash`; it does not copy
their component identities, feature fixtures or signing credentials.
