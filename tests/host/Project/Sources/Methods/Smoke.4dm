//%attributes = {"invisible":true}
var $ignored : Integer
var $parameter; $name; $target : Text
var $folderPath; $fontPath : Text
var $request; $result; $report; $conversion; $small; $large; $empty; $space; $probe; $aliasRequest : Object
var $tool : cs.TextToSVG.TextOutline
var $requestFile; $statusFile : 4D.File
var $renamedExecutable : 4D.File
var $font; $expandedFont : 4D.File
var $adapter : cs.TextToSVG._Process
var $cases; $malformed : Collection
var $invalid : Variant
var HOST_StatusFile : 4D.File
var HOST_Phase : Text
var HOST_PreviousHandler : Text
$ignored:=Get database parameter(User param value; $parameter)
If (Is Windows)
	$requestFile:=File($parameter; fk platform path)
Else
	$requestFile:=File($parameter; fk posix path)
End if
$request:=JSON Parse($requestFile.getText())
If (Is Windows)
	$statusFile:=File($request.status; fk platform path)
Else
	$statusFile:=File($request.status; fk posix path)
End if
$statusFile.setText("{\"success\":false,\"phase\":\"starting\"}")
HOST_StatusFile:=$statusFile
HOST_Phase:="constructor"
HOST_PreviousHandler:=Method called on error(ek local)
ON ERR CALL("Host_error"; ek local)
$target:="macOS-universal"
If (Is Windows)
	$target:="Windows-x64"
End if
$tool:=cs.TextToSVG.TextOutline.new($target)
$report:=New object("success"; True; "finished"; False; "cases"; New collection)
$report.compiled:=$tool._cli.compiled
HOST_Phase:="renamed executable"
If (Is Windows)
	$renamedExecutable:=File($request.executable; fk platform path)
Else
	$renamedExecutable:=File($request.executable; fk posix path)
End if
$report.renamedExecutable:=cs.TextToSVG._Process.new().execute($renamedExecutable; New collection("--version"))
If (Not($report.renamedExecutable.ok))
	$report.success:=False
End if
HOST_Phase:="feature cases"
If ($request.expectCompiled#$report.compiled)
	$report.success:=False
End if
$conversion:=New object("fontFile"; $request.fontFile; "text"; "AV"; "fontSize"; 12; "units"; "mm")
$cases:=New collection($conversion; New object("fontFile"; $request.fontFile; "text"; "AV"; "fontSize"; 24; "units"; "mm"); New object("fontFile"; $request.fontFile; "text"; " "; "fontSize"; 12; "units"; "mm"); New object("fontFile"; $request.fontFile; "text"; ""; "fontSize"; 12; "units"; "mm"); New object("fontFile"; $request.fontCFF; "text"; "A"; "fontSize"; 12; "units"; "pt"); New object("fontFile"; $request.fontFile; "text"; "é"; "fontSize"; 12; "units"; "px"))
$cases.push(New object("fontFile"; $request.fontCFF; "faceIndex"; 0; "text"; "日本語ひらがなカタカナ"; "fontSize"; 0.5; "units"; "in"))
$cases.push(New object("fontFile"; $request.fontCFF; "text"; "A"; "fontSize"; 24; "units"; "pt"))
For each ($probe; $cases)
	$result:=$tool.convert($probe)
	$report.cases.push($result)
	If (Not($result.ok))
		$report.success:=False
	End if
End for each
HOST_Phase:="host font aliases"
$adapter:=cs.TextToSVG._Process.new()
$report.fontPaths:=New collection
$report.aliasRequestsUnchanged:=True
$report.aliasOutputMatches:=True
For each ($name; New collection("Data"; "Resources"))
	$font:=File($request["fontAlias"+$name]; fk posix path; *)
	$expandedFont:=File($font.platformPath; fk platform path)
	$fontPath:=$expandedFont.path
	If (Is Windows)
		$fontPath:=$expandedFont.platformPath
	End if
	$probe:=New object("hostContext"; $fontPath=$request["fontFile"+$name])
	$probe.virtualBefore:=$font.path=$request["fontAlias"+$name]
	$probe.physicalAfter:=$expandedFont.path#$font.path
	$probe.fileHelper:=$adapter.nativePath($expandedFont)=$fontPath
	$folderPath:=$expandedFont.parent.path
	If (Is Windows)
		$folderPath:=$expandedFont.parent.platformPath
	End if
	$probe.folderHelper:=$adapter.nativePath($expandedFont.parent)=$folderPath
	$probe.nativePath:=$fontPath
	$report.fontPaths.push($probe)
	$aliasRequest:=New object("fontFile"; $request["fontAlias"+$name]; "text"; "AV"; "fontSize"; 12; "units"; "mm")
	$result:=$tool.convert($aliasRequest)
	If (($aliasRequest.fontFile#$request["fontAlias"+$name]) | (Value type($aliasRequest.protocol)#Is undefined))
		$report.aliasRequestsUnchanged:=False
	End if
	$report.cases.push($result)
	If (Not($result.ok))
		$report.success:=False
	Else
		If ($report.cases[0].ok)
			If ($result.value.svg#$report.cases[0].value.svg)
				$report.aliasOutputMatches:=False
			End if
		End if
	End if
End for each
$result:=$tool.convert(New object("fontFile"; $expandedFont.platformPath; "text"; "AV"; "fontSize"; 12; "units"; "mm"))
$report.cases.push($result)
If (Not($result.ok))
	$report.success:=False
End if
$probe:=New object("protocol"; 1; "fontFile"; $request.fontAliasData; "text"; "AV"; "fontSize"; 12; "units"; "mm")
$report.rawFontAliasCLI:=$tool._decode($tool._cli.execute(New collection("convert"); JSON Stringify($probe)); "convert")
$report.missingFontAlias:=$tool.convert(New object("fontFile"; $request.fontAliasData+".missing"; "text"; "AV"; "fontSize"; 12; "units"; "mm"))
$malformed:=New collection("relative-font.ttf"; ":relative-font.ttf"; "../relative-font.ttf")
If (Is Windows)
	$malformed:=New collection("C:"; "C:relative-font.ttf"; Char(92)+"relative-font.ttf")
End if
$report.relativeFontPaths:=New collection
For each ($invalid; $malformed)
	$report.relativeFontPaths.push($tool.convert(New object("fontFile"; $invalid; "text"; "AV"; "fontSize"; 12; "units"; "mm")))
End for each
$report.unresolvedFontPath:=$report.relativeFontPaths[0]
$report.pathHandlerRestored:=Method called on error(ek local)="Host_error"
$report.invalidFontPaths:=New collection
For each ($invalid; New collection(Null; 123; True; New object; New collection; ""; "bad"+Char(0)+"font"))
	$report.invalidFontPaths.push($tool.convert(New object("fontFile"; $invalid; "text"; "AV"; "fontSize"; 12; "units"; "mm")))
End for each
$report.requestUnchanged:=Value type($conversion.protocol)=Is undefined
If (Not($report.requestUnchanged))
	$report.success:=False
End if
HOST_Phase:="geometry semantics"
$report.geometry:=False
If ($report.success)
	$small:=$report.cases[0].value
	$large:=$report.cases[1].value
	$space:=$report.cases[2].value
	$empty:=$report.cases[3].value
	If (($small.inkBounds=Null) | ($large.inkBounds=Null))
		$report.success:=False
	Else
		If (($small.inkBounds.width<=0) | ($small.inkBounds.height<=0) | ($small.inkBounds.y>=0) | ($small.advance.x<=0) | ($small.advance.y#0) | ($small.pathData=""))
			$report.success:=False
		End if
		If ((Abs($large.inkBounds.width-(2*$small.inkBounds.width))>0.00001) | (Abs($large.inkBounds.height-(2*$small.inkBounds.height))>0.00001) | (Abs($large.advance.x-(2*$small.advance.x))>0.00001))
			$report.success:=False
		End if
	End if
	If (($space.pathData#"") | ($space.inkBounds#Null) | ($space.advance.x<=0) | ($empty.pathData#"") | ($empty.inkBounds#Null) | ($empty.advance.x#0) | ($empty.glyphCount#0))
		$report.success:=False
	End if
	$small:=$report.cases[4].value
	$large:=$report.cases[7].value
	If (($small.inkBounds=Null) | ($large.inkBounds=Null))
		$report.success:=False
	Else
		If (($small.inkBounds.width<=0) | ($small.inkBounds.height<=0) | ($small.advance.x<=0))
			$report.success:=False
		End if
		If ((Abs($large.inkBounds.width-(2*$small.inkBounds.width))>0.00001) | (Abs($large.inkBounds.height-(2*$small.inkBounds.height))>0.00001) | (Abs($large.advance.x-(2*$small.advance.x))>0.00001))
			$report.success:=False
		End if
	End if
	$report.geometry:=$report.success
End if
HOST_Phase:="feature failures"
$result:=$tool.convert(New object("fontFile"; $request.fontFile; "text"; "A"; "fontSize"; 0; "units"; "mm"))
$report.invalid:=$result
If ($result.ok | ($result.exitCode#2))
	$report.success:=False
End if
$report.nullRequest:=$tool.convert(Null)
HOST_Phase:="serialization failure"
$probe:=OB Copy($conversion)
$probe.circular:=$probe
$report.serialization:=$tool.convert($probe)
OB REMOVE($probe; "circular")
If ($report.serialization.ok | ($report.serialization.error.code#"invalid_argument"))
	$report.success:=False
End if
HOST_Phase:="feature failures"
$report.missingFont:=$tool.convert(New object("fontFile"; $request.fontFile+".missing"; "text"; "A"; "fontSize"; 12; "units"; "mm"))
$report.missingGlyph:=$tool.convert(New object("fontFile"; $request.fontFile; "text"; "日"; "fontSize"; 12; "units"; "mm"))
$report.rtl:=$tool.convert(New object("fontFile"; $request.fontFile; "text"; "שלום"; "fontSize"; 12; "units"; "mm"))
$report.failureSemantics:=True
For each ($name; New collection("missingFont"; "missingGlyph"; "rtl"))
	$result:=$report[$name]
	If ($result.ok | ($result.exitCode#2) | ($result.stdout#""))
		$report.success:=False
		$report.failureSemantics:=False
	End if
End for each
$result:=$tool.version()
$report.version:=$result
If (Not($result.ok))
	$report.success:=False
Else
	If ($result.value.version#"0.1.0")
		$report.success:=False
	End if
End if
HOST_Phase:="missing executable"
$result:=cs.TextToSVG._CLI.new("missing-cli"; $target).execute(New collection("--version"))
$report.missing:=$result
If ($result.error.code#"missing_executable")
	$report.success:=False
End if
$result:=cs.TextToSVG.TextOutline.new("unsupported-arm-target").version()
$report.unsupported:=$result
If ($result.error.code#"unsupported_architecture")
	$report.success:=False
End if
HOST_Phase:="launch failure"
$result:=cs.TextToSVG._Process.new().execute($requestFile; New collection)
$report.launch:=$result
If ($result.error.code#"launch_failed")
	$report.success:=False
End if
HOST_Phase:="malformed protocol"
$result:=$tool._decode(New object("ok"; True; "stdout"; "not json"; "stderr"; ""; "exitCode"; 0); "convert")
$report.protocol:=$result
If ($result.error.code#"invalid_protocol")
	$report.success:=False
End if
$report.protocolTypes:=New collection
For each ($name; New collection("{\"protocol\":true,\"svg\":\"invalid\"}"; "{\"protocol\":false,\"svg\":\"invalid\"}"))
	$result:=$tool._decode(New object("ok"; True; "stdout"; $name; "stderr"; ""; "exitCode"; 0); "convert")
	$report.protocolTypes.push($result)
	If ($result.ok | ($result.error.code#"invalid_protocol"))
		$report.success:=False
	End if
End for each
$result:=$tool._decode(New object("ok"; True; "stdout"; "{\"protocol\":null,\"svg\":\"invalid\"}"; "stderr"; ""; "exitCode"; 0); "convert")
$report.protocolNull:=$result
If ($result.ok | ($result.error.code#"invalid_protocol"))
	$report.success:=False
End if
$report.protocolScalars:=New collection
For each ($name; New collection("true"; "false"; "null"; "1"; "[]"))
	$result:=$tool._decode(New object("ok"; True; "stdout"; $name; "stderr"; ""; "exitCode"; 0); "convert")
	$report.protocolScalars.push($result)
	If ($result.ok | ($result.error.code#"invalid_protocol"))
		$report.success:=False
	End if
End for each
HOST_Phase:="File argument"
$report.malformedFields:=New collection
$malformed:=New collection
For each ($name; New collection("ok"; "svg"; "pathData"; "inkBounds"; "advance"; "units"; "fontSize"; "glyphCount"))
	$probe:=OB Copy($report.cases[0].value)
	OB REMOVE($probe; $name)
	$malformed.push(JSON Stringify($probe))
End for each
$malformed.push("{\"protocol\":1,\"ok\":true,\"svg\":\"x\",\"pathData\":\"\",\"inkBounds\":null,\"advance\":{\"x\":true,\"y\":0},\"units\":\"mm\",\"fontSize\":12,\"glyphCount\":1}")
$probe:=OB Copy($report.cases[0].value)
$probe.glyphCount:=32769
$malformed.push(JSON Stringify($probe))
For each ($name; $malformed)
	$result:=$tool._decode(New object("ok"; True; "stdout"; $name; "stderr"; ""; "exitCode"; 0); "convert")
	$report.malformedFields.push($result)
	If ($result.ok | ($result.error.code#"invalid_protocol"))
		$report.success:=False
	End if
End for each
HOST_Phase:="malformed error protocol"
$report.malformedErrors:=New collection
For each ($name; New collection("null"; "{\"protocol\":true,\"error\":{\"code\":\"bad\",\"message\":\"bad\"}}"; "{\"protocol\":1,\"error\":{\"code\":1,\"message\":\"bad\"}}"; "{\"protocol\":1,\"error\":{\"code\":\"bad\"}}"))
	$result:=$tool._decode(New object("ok"; False; "stdout"; ""; "stderr"; $name; "exitCode"; 2; "error"; New object("code"; "cli_error"; "message"; $name)); "convert")
	$report.malformedErrors.push($result)
	If ($result.ok | ($result.error.code#"invalid_protocol"))
		$report.success:=False
	End if
End for each
HOST_Phase:="File argument"
$result:=cs.TextToSVG._CLI.new("text-to-svg"; $target).execute(New collection("--echo"; $requestFile))
$report.fileArgument:=$result
If (Not($result.ok))
	$report.success:=False
Else
	If (JSON Parse($result.stdout).argument#$parameter)
		$report.success:=False
	End if
End if
$report.rejectedArguments:=New collection
HOST_Phase:="scalar arguments"
For each ($invalid; New collection(123; 1.25; True; False; Null; Current date; Current time; New collection; New object))
	$result:=cs.TextToSVG._Process.new().execute($requestFile; New collection($invalid))
	$report.rejectedArguments.push(New object("type"; Value type($invalid); "result"; $result))
	If ($result.ok | ($result.error.code#"invalid_argument"))
		$report.success:=False
	End if
End for each
$report.rejectedNul:=New collection
HOST_Phase:="NUL arguments"
For each ($name; New collection(Char(0)+"prefix"; "middle"+Char(0)+"suffix"; "trailing"+Char(0)))
	$result:=cs.TextToSVG._Process.new().execute($requestFile; New collection($name))
	$report.rejectedNul.push($result)
	If ($result.ok | ($result.error.code#"invalid_argument"))
		$report.success:=False
	End if
End for each
$report.missingFile:=cs.TextToSVG._Process.new().execute(Null; New collection)
$report.missingArguments:=cs.TextToSVG._Process.new().execute($requestFile; Null)
If ($report.missingFile.ok | ($report.missingFile.error.code#"invalid_argument"))
	$report.success:=False
End if
If ($report.missingArguments.ok | ($report.missingArguments.error.code#"invalid_argument"))
	$report.success:=False
End if
HOST_Phase:="Folder argument"
$result:=cs.TextToSVG._CLI.new("text-to-svg"; $target).execute(New collection("--echo"; $requestFile.parent))
$report.folderArgument:=$result
$folderPath:=$requestFile.parent.path
If (Is Windows)
	$folderPath:=$requestFile.parent.platformPath
End if
If (Not($result.ok))
	$report.success:=False
Else
	If (JSON Parse($result.stdout).argument#$folderPath)
		$report.success:=False
	End if
End if
$report.finished:=True
ON ERR CALL(HOST_PreviousHandler; ek local)
$statusFile.setText(JSON Stringify($report))
HOST_StatusFile:=Null
HOST_Phase:=""
HOST_PreviousHandler:=""
QUIT 4D
