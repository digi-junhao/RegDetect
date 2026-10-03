"""StreamWeave's regex front end: regex source to AST.

Deliberately empty. Re-exporting `tokenizer`'s names here would make
`python -m streamweave.tokenizer` load that module twice and emit a runpy
warning, so callers import from the submodule directly:

    from streamweave.tokenizer import tokenize, ParseError
"""
