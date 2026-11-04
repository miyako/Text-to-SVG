# Hermetic outline test fonts

These are **test fixtures**, never system-font substitutions and not runtime
fallback fonts. All upstream bytes are pinned by commit URL and SHA-256 in
`manifest.json`; the adjacent LICENSE files are the actual upstream SIL Open
Font License 1.1 notices, including copyright and Reserved Font Name terms.
Redistribution is permitted under those notices; retain them with the fixtures.

* `NotoSerif-Regular.ttf`: unmodified static TrueType Noto Serif, from
  `notofonts/noto-fonts` commit `ffebf8c1ee449e544955a7e813c54f9b73848eac`.
  Covers Latin accents, combining marks, kerning and standard `ffi` ligatures.
* `NotoSansJP-Regular.otf`: unmodified upstream Japanese subset CFF OpenType,
  from `notofonts/noto-cjk` commit `f8d157532fbfaeda587e826d4cd5b21a49186f7c`.
  This is the upstream `Sans/SubsetOTF/JP` artifact, not a locally generated
  subset. Covers Han, Hiragana and Katakana.
* `NotoSerif-NotoSansJP.ttc`: reproducibly generated two-face TTC v1 containing
  the original Latin TrueType at face index 0 and Japanese CFF at face index 1.
  The standard-library-only recipe in `scripts/fetch_test_fonts.py` writes
  a 20-byte TTC header, appends each original sfnt aligned to four bytes and
  relocates its table offsets; outlines, names, metrics and table checksums are
  unchanged. Both adjacent OFL notices apply. Tests verify positive nonzero
  face selection, different Latin outlines per face, Japanese coverage only
  on face 1, and rejection of out-of-range face 2.

The original embedded name-table copyright notices are retained:
Noto Serif: “Copyright 2015-2021 Google LLC. All Rights Reserved.”;
Noto Sans JP: “© 2014-2021 Adobe (http://www.adobe.com/).”
Both fonts' embedded license descriptions explicitly identify SIL OFL 1.1.
The downloaded Noto Serif repository notice additionally credits the Noto
Project Authors (2018); it is preserved verbatim rather than rewritten.

Restore missing/corrupt fixtures using `python3 scripts/fetch_test_fonts.py`.
Tests verify fixture hashes without fetching anything; fixture reads require no
network access. Building the engine follows its separate dependency policy;
`TEXT_TO_SVG_EXECUTABLE` reuses a previously built executable.
No fontTools, subsetter or host-installed fonts are involved.
