# TextOutline

`cs.TextToSVG.TextOutline.new(target : Text)` wraps the bundled `text-to-svg`
C++ executable. Omit `target` on macOS, or pass `"macOS-universal"`.
Windows requires `"Windows-x64"` explicitly. Component-internal references use
`cs.TextOutline`; the namespace is independent of the repository name.

## convert(request : Object) : Object

Supply `fontFile` (a host 4D filesystem path, absolute POSIX path or platform
path as Text), `text` (Text),
`fontSize` (finite positive number, at most 10000), and `units`
(`"mm"`, `"pt"`, `"px"` or `"in"`). Optional `faceIndex` is a nonnegative integer,
default zero. The wrapper copies the request and sets `protocol:1`; it does not
mutate the caller's object. The CLI validates the request. Null requests return
`invalid_argument` without starting a process.
Copying and JSON serialization use a narrowly scoped local error handler;
unserializable requests return `invalid_argument` with captured `details` and
no process exit code. The previous handler is restored before execution.

The request is serialized to UTF-8 JSON and sent to `convert` over stdin using
the actual `SystemWorker.postMessage(Text)` API, then stdin is closed. Requests
are bounded to 1 MiB by the CLI and text to 16384 UTF-8 bytes. Font files are
explicit, never selected through platform font fallback.

The component resolves `/DATA` and `/RESOURCES` in **host context**, using
`File(path; pathType; *)` internally. Leading `/` selects POSIX/filesystem
syntax; otherwise platform syntax is used, including macOS HFS paths from
existing `.platformPath` callers and Windows native paths.
It then reconstructs the File through its `.platformPath` and `fk platform path`
before selecting the expanded `.path` on macOS or `.platformPath` on Windows.
This materialization is required; the original `.path` can still contain a
4D filesystem alias. The `*` context selector and platform-path expansion solve
different problems. Callers need not resolve paths themselves. C++ receives only
the physical native path, and the caller's `fontFile` is unchanged.

Non-Text, empty, NUL-containing or relative `fontFile` values return prelaunch
`invalid_argument`. Relative paths are rejected explicitly rather than relying
on 4D's constructor normalization. File construction/expansion errors are captured with a
narrow local error handler and returned as `invalid_argument` with details.
An unreadable resolved font still returns the CLI's `invalid_font` error.
File objects and relative filenames are not supported request values.

This is single-line horizontal LTR shaping, not paragraph layout: RTL and mixed
scripts are rejected except the Japanese Han/Hiragana/Katakana family. Missing
glyphs are errors, not silent substitutions. Whitespace has advance but no ink.
No font or source text is embedded in the SVG.

Success is `{ok:true,stdout:Text,stderr:Text,exitCode:0,value:Object}` where `value`
contains `{protocol:1,ok:true,svg:Text,pathData:Text,inkBounds:null|Object,
advance:{x:Number,y:Number},units:Text,fontSize:Number,glyphCount:Integer}`.
Ink bounds contain finite `x,y,width,height` in the requested units. SVG paths
preserve the baseline-relative geometry; advance and ink are distinct.
The wrapper validates the protocol and all required field types, numeric ranges
and finite coordinates, rather than treating arbitrary JSON as success.
Decoded glyph counts must be integers from zero through 32768.

## version() : Object

Executes `--version`, requiring exactly protocol 1 and version `"0.1.0"` in the
decoded `value`. The process envelope has the same shape as `convert`.

## Errors and execution

Calls are synchronous with a 30-second timeout and isolated per-call state.
The adapter preserves raw stdout/stderr/exitCode and system diagnostics.
Prelaunch failures have no invented exit code. Nonempty stderr alone is not
failure. On CLI exit 2, valid protocol-1 stderr `{error:{code:Text,message:Text}}`
is propagated as `result.error` while retaining the raw process envelope.
Malformed success/error responses produce `invalid_protocol`. Generic errors
include `missing_executable`, `unsupported_platform`, `unsupported_architecture`,
`invalid_argument`, `launch_failed`, `process_error`, `process_terminated`,
and `cli_error` when no feature-specific diagnostic can be decoded.

```4d
var $tool : cs.TextToSVG.TextOutline
var $request : Object
var $target : Text
$target:="macOS-universal"
If (Is Windows)
    $target:="Windows-x64"
End if
$tool:=cs.TextToSVG.TextOutline.new($target)
$request:=New object
$request.fontFile:="/DATA/MyFont.otf"
$request.text:="Hello"
$request.fontSize:=12
$request.units:="mm"
$result:=$tool.convert($request)
If ($result.ok)
    $svg:=$result.value.svg
Else
    $error:=$result.error.code+": "+$result.error.message
End if
```

A readable ZapfDingbats font can still return `missing_glyph` for Latin text.
Path readability (`invalid_font`) and character coverage are separate checks.

`_fontPath`, `_decode`, `_parse`, `_number`, `_invalid` and `_cli` are implementation details,
not access-control boundaries. `--echo <literal>` is a CLI diagnostic used to
verify native File/Folder argument quoting, not a feature method.
