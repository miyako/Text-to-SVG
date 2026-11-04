# _CLI (internal convention)

`cs._CLI.new(executableName : Text; target : Text)` is the reusable bundled
executable resolver. The host can technically access `cs.TextToSVG._CLI`;
underscore naming is not true private access in 4D.

`execute(arguments : Collection; {stdin : Text}) : Object` selects `/RESOURCES/bin/macOS/<name>`
or `/RESOURCES/bin/Windows/<name>.exe`, checks existence, and delegates to
`_Process`, which expands the virtual File using its platform path and
`fk platform path`. There is deliberately no PATH search, permission repair,
shell, download, copied PDF algorithm or queue.

macOS default/`macOS-universal` uses the two-slice executable. Windows requires
explicit `Windows-x64`. Unknown selection or OS returns an explicit error;
Windows ARM64 native execution is not implemented. See [TextOutline](TextOutline.md)
for the common result/error shape.

The optional UTF-8 Text input is passed unchanged to `_Process`; an omitted or
empty input simply closes stdin. The adapter knows nothing about feature JSON.

The read-only `compiled` Boolean evaluates `Is compiled mode` inside the
component. The release host fixture uses it to reject an interpreted fallback.
