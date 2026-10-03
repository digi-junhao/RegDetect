"""RegDetect's regex front end: regex source to AST.

Deliberately empty. Re-exporting `tokenizer`'s names here would make
`python -m RegDetect.tokenizer` load that module twice and emit a runpy
warning, so callers import from the submodule directly:

    from RegDetect.tokenizer import tokenize, ParseError
"""
