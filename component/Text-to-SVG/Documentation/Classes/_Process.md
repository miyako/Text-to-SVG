# _Process (internal convention)

`cs._Process.new().execute(file : 4D.File; arguments : Collection; {stdin : Text}) : Object`
is the generic synchronous process adapter. Arguments must be Text, File or
Folder. File/Folder values are expanded to physical POSIX paths on macOS or
platform paths on Windows; NUL is rejected using exact string comparison.
Numeric, Boolean, Null, date/time and Collection values are rejected before
object-instance checks; plain Objects are also rejected. They return
`invalid_argument`, rather than raising a 4D object-type error or launching a process.

`quote(argument : Text) : Text` always quotes one argument. macOS double quotes
escape backslashes and quotes; Windows follows the CRT backslash-before-quote
and trailing-backslash rules. This is 4D command-line parsing, not a shell.
The executable uses a physical POSIX path on macOS and native platform path on Windows.
Native 4D round-trip tests, not shell tests, are necessary on each platform.

`expand(entry)` reconstructs a validated File or Folder with
`OB Class(entry).new(entry.platformPath; fk platform path)`.
`nativePath(entry)` uses that expanded object's `.path` on macOS or
`.platformPath` on Windows. Execution reuses these internal helpers for the
executable and File/Folder arguments. Expansion preserves the already
constructed object's class and context; as in llama-cpp's `expand`, it is not
itself a context selector. Construct a host-context object inside a component
with `File(path; pathType; *)` or `Folder(path; pathType; *)` before expanding it.
`TextOutline` does this for font paths. The bundled executable's `/RESOURCES`
object is deliberately constructed without `*` in component scope.

Options: `dataType:"text"`, `encoding:"UTF-8"`, `hideWindow:true`,
`timeout:30`, `currentDirectory:file.parent`. Optional nonempty UTF-8 Text input
is sent with the documented `.postMessage(stdin)` method (there is no
`writeInput` SystemWorker API). Input is then closed,
then `.wait()` completes. The adapter captures `.response` (stdout),
`.responseError` (stderr), `.exitCode` and `.errors` separately.
Nonempty stderr does not turn a zero exit code into failure. Missing/undefined
exitCode is abnormal termination, not zero.

Creation errors use `_CaptureError` and restore `Method called on error(ek local)`
immediately afterward. Diagnostic `Last errors` are returned, not swallowed.
Input/close/wait errors are captured in a second narrow local-handler scope and
returned as `process_error` with available raw streams/status.
There is no shared async queue/PID map or global handler. For binary feature
protocols, add a deliberately separate Blob mode rather than corrupting UTF-8 text.
