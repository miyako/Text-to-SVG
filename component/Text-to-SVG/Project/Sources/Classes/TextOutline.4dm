property _cli : cs._CLI

Class constructor($target : Text)
	This._cli:=cs._CLI.new("text-to-svg"; $target)

Function convert($request : Object)->$result : Object
	var $copy; $fontPath : Object
	var $input; $previous : Text
	var $errors : Collection
	var CLI_LastErrors : Collection
	If ($request=Null)
		return New object("ok"; False; "error"; New object("code"; "invalid_argument"; "message"; "Conversion request is required"))
	End if
	If (Value type($request.fontFile)#Is text)
		return New object("ok"; False; "error"; New object("code"; "invalid_argument"; "message"; "fontFile must be nonempty Text without NUL"))
	End if
	If (($request.fontFile="") | (Position(Char(0); $request.fontFile; *)>0))
		return New object("ok"; False; "error"; New object("code"; "invalid_argument"; "message"; "fontFile must be nonempty Text without NUL"))
	End if
	$fontPath:=This._fontPath($request.fontFile)
	If (Not($fontPath.ok))
		return $fontPath
	End if
	$previous:=Method called on error(ek local)
	CLI_LastErrors:=Null
	ON ERR CALL("_CaptureError"; ek local)
	$copy:=OB Copy($request)
	If ($copy#Null)
		$copy.protocol:=1
		$copy.fontFile:=$fontPath.value
		$input:=JSON Stringify($copy)
	End if
	ON ERR CALL($previous; ek local)
	$errors:=CLI_LastErrors
	CLI_LastErrors:=Null
	If (($errors#Null) | ($input=""))
		return New object("ok"; False; "error"; New object("code"; "invalid_argument"; "message"; "Conversion request must be JSON serializable"; "details"; $errors))
	End if
	$result:=This._decode(This._cli.execute(New collection("convert"); $input); "convert")

Function _fontPath($path : Text)->$result : Object
	var $file : 4D.File
	var $kind : Integer
	var $absolute : Boolean
	var $native; $previous : Text
	var $errors : Collection
	var CLI_LastErrors : Collection
	$kind:=fk posix path
	$absolute:=True
	If (Substring($path; 1; 1)#"/")
		$kind:=fk platform path
		If (Is Windows)
			$absolute:=Match regex("^[A-Za-z]:"; $path)
			If ($absolute)
				$absolute:=Length($path)>=3
				If ($absolute)
					$absolute:=(Substring($path; 3; 1)="/") | (Substring($path; 3; 1)=Char(92))
				End if
			Else
				$absolute:=Substring($path; 1; 2)=Char(92)+Char(92)
			End if
		Else
			$absolute:=Position(":"; $path; *)>1
		End if
	End if
	If (Not($absolute))
		return New object("ok"; False; "error"; New object("code"; "invalid_argument"; "message"; "fontFile must be an absolute or host filesystem path"))
	End if
	$previous:=Method called on error(ek local)
	CLI_LastErrors:=Null
	ON ERR CALL("_CaptureError"; ek local)
	$file:=File($path; $kind; *)
	If ($file#Null)
		$native:=cs._Process.new().nativePath($file)
	End if
	ON ERR CALL($previous; ek local)
	$errors:=CLI_LastErrors
	CLI_LastErrors:=Null
	If (($errors#Null) | ($native=""))
		return New object("ok"; False; "error"; New object("code"; "invalid_argument"; "message"; "fontFile path could not be resolved"; "details"; $errors))
	End if
	$result:=New object("ok"; True; "value"; $native)

Function version()->$result : Object
	$result:=This._decode(This._cli.execute(New collection("--version")); "version")

Function _number($value : Variant)->$valid : Boolean
	$valid:=False
	If ((Value type($value)#Is real) & (Value type($value)#Is longint))
		return False
	End if
	// Infinity is unchanged by halving; NaN does not equal itself.
	$valid:=($value=0) | (($value=$value) & (($value/2)#$value))

Function _invalid($result : Object; $message : Text)->$invalid : Object
	$result.ok:=False
	$result.error:=New object("code"; "invalid_protocol"; "message"; $message)
	$invalid:=$result

Function _parse($text : Text)->$parsed : Object
	var $payload : Variant
	var $previous : Text
	var $errors : Collection
	var CLI_LastErrors : Collection
	$previous:=Method called on error(ek local)
	CLI_LastErrors:=Null
	ON ERR CALL("_CaptureError"; ek local)
	$payload:=JSON Parse($text)
	ON ERR CALL($previous; ek local)
	$errors:=CLI_LastErrors
	CLI_LastErrors:=Null
	If (($errors#Null) | (Value type($payload)#Is object))
		return New object("ok"; False)
	End if
	$parsed:=New object("ok"; True; "value"; $payload)

Function _decode($result : Object; $field : Text)->$decoded : Object
	var $parsed; $payload; $bounds : Object
	var $key : Text
	If (Not($result.ok))
		If (Value type($result.exitCode)#Is longint)
			If (Value type($result.exitCode)#Is real)
				return $result
			End if
		End if
		If ($result.exitCode#2)
			return $result
		End if
		If ($result.error.code#"cli_error")
			return $result
		End if
		If ($result.stdout#"")
			return This._invalid($result; "CLI failure must have empty stdout")
		End if
		$parsed:=This._parse($result.stderr)
		If (Not($parsed.ok))
			return This._invalid($result; "CLI error must be a JSON object")
		End if
		$payload:=$parsed.value
		If (Not(This._number($payload.protocol)))
			return This._invalid($result; "Unsupported CLI protocol version")
		End if
		If ($payload.protocol#1)
			return This._invalid($result; "Unsupported CLI protocol version")
		End if
		If (Value type($payload.error)#Is object)
			return This._invalid($result; "CLI error object is required")
		End if
		If ((Value type($payload.error.code)#Is text) | (Value type($payload.error.message)#Is text))
			return This._invalid($result; "CLI error code and message must be Text")
		End if
		If (($payload.error.code="") | ($payload.error.message=""))
			return This._invalid($result; "CLI error code and message must be nonempty")
		End if
		$result.error:=$payload.error
		return $result
	End if
	$parsed:=This._parse($result.stdout)
	If (Not($parsed.ok))
		return This._invalid($result; "CLI response must be a JSON object")
	End if
	$payload:=$parsed.value
	If (Not(This._number($payload.protocol)))
		return This._invalid($result; "Unsupported CLI protocol version")
	End if
	If ($payload.protocol#1)
		return This._invalid($result; "Unsupported CLI protocol version")
	End if
	If ($field="version")
		If (Value type($payload.version)#Is text)
			return This._invalid($result; "CLI version must be Text")
		End if
		If ($payload.version#"0.1.1")
			return This._invalid($result; "Unsupported CLI version")
		End if
	Else
		If (Value type($payload.ok)#Is boolean)
			return This._invalid($result; "CLI conversion must have ok:true")
		End if
		If (Not($payload.ok))
			return This._invalid($result; "CLI conversion must have ok:true")
		End if
		If ((Value type($payload.svg)#Is text) | (Value type($payload.pathData)#Is text) | (Value type($payload.units)#Is text))
			return This._invalid($result; "CLI SVG, pathData and units must be Text")
		End if
		If ($payload.svg="")
			return This._invalid($result; "CLI SVG must not be empty")
		End if
		If (Not(New collection("mm"; "pt"; "px"; "in").includes($payload.units)))
			return This._invalid($result; "Unsupported output units")
		End if
		If (Not(This._number($payload.fontSize)))
			return This._invalid($result; "Invalid output fontSize")
		End if
		If (($payload.fontSize<=0) | ($payload.fontSize>10000))
			return This._invalid($result; "Invalid output fontSize")
		End if
		If (Not(This._number($payload.glyphCount)))
			return This._invalid($result; "Invalid output glyphCount")
		End if
		If (($payload.glyphCount<0) | ($payload.glyphCount>32768) | ($payload.glyphCount#Int($payload.glyphCount)))
			return This._invalid($result; "Invalid output glyphCount")
		End if
		If (Value type($payload.advance)#Is object)
			return This._invalid($result; "CLI advance must be an object")
		End if
		For each ($key; New collection("x"; "y"))
			If (Not(This._number($payload.advance[$key])))
				return This._invalid($result; "CLI advance must contain finite coordinates")
			End if
		End for each
		If (Value type($payload.inkBounds)#Is null)
			If (Value type($payload.inkBounds)#Is object)
				return This._invalid($result; "CLI inkBounds must be null or an object")
			End if
			$bounds:=$payload.inkBounds
			For each ($key; New collection("x"; "y"; "width"; "height"))
				If (Not(This._number($bounds[$key])))
					return This._invalid($result; "CLI inkBounds must contain finite coordinates")
				End if
			End for each
			If (($bounds.width<0) | ($bounds.height<0))
				return This._invalid($result; "CLI inkBounds dimensions must be nonnegative")
			End if
		End if
	End if
	$result.value:=$payload
	$decoded:=$result
