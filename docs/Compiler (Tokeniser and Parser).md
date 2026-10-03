# Stage 1: The front end

The front-end of the compiler has two stages:

- **Part I — the Tokeniser** (stage 1a). Regex text is inputted, and a flat list of tokens is outputted.
- **Part II — the Parser** (stage 1b). That token list inputted, and an abstract syntax tree outputted.


**This version supports the classic Thompson core and nothing else:** literals, concatenation, alternation
`|`, grouping `()`, and Kleene star `*`. 
This language is enough to express every regular language.

---

# Part I — The tokeniser

## 1. What a tokeniser is

A **tokeniser** is the first phase of nearly every compiler. Its
job is to convert a flat string of characters into a flat list of **tokens** — meaningful units,
each tagged with what kind of thing it is, and often it's value.


1. **What are the pieces?** Is `\n` two characters or one newline byte? Is the `*` in `a\*b` an
   operator or a literal asterisk?
2. **How do the pieces fit together?** Does `ab|c*d` mean `(ab) | ((c*)d)` or something else? Are the
   parentheses balanced? Does that `*` have anything to repeat?

Question 1 is **local** as you can answer it by looking at one character and the next adjacent character.
Question 2
is **structural**: it requires the whole shape of the input, and is therefore recursive. A tokeniser answers
question 1 and *only* question 1. The parser answers question 2, and gets to do so without ever
thinking about character types.

That split is the entire point:

```
Pattern: a\*b   (means: 'a', then a literal star character, then 'b')

without a tokeniser   parser sees:  'a' '\' '*' 'b'
                                    ...and must decide whether that '*' is an
                                    operator while also tracking grammar.

with a tokeniser      parser sees:  CHAR CHAR CHAR EOF
                                    ... '\*' has already become one ordinary character, with a byte value of 42. There is no star token so the parser cannot mistake it for a repeat.

**Note** EOF stands for end of input.                                    
                                    
```

The parser's grammar is written based on the second(tokensied) alphabet. It is short 
*because* there are only few token types to sort.

### Tokens are values, not text

Each token is a small `Token` object, defined as a frozen dataclass.

```python
@dataclass(frozen=True)
class Token:
    kind: TokenKind      # CHAR, PIPE, STAR, LPAREN, RPAREN, EOF
    value: int | None    # the byte for type CHAR, and none for all other perators
    position: int        # 0-based index into the original pattern
```

- `kind` tells the parser what to do with the token.
- `value` is the byte the token stands for, already decoded by the
  tokeniser: `a` arrives as 97, `\n` as 10.
- `position` is the column the token started at. Error messages use
  it to put a `^` under the exact problem.

---

## 2. The alphabet is bytes

The hardware reads **one byte per clock**, so every symbol is an `int`
from 0 to 255. The tokeniser converts characters with `ord()` and
rejects anything above 255, which matches Latin-1 encoding (`a` is 97).

Because symbols are already numbers, stage 4 can write them straight
into the hardware's comparators. And a character the hardware can't
represent is rejected up front, with a clear error:

```
'€' is U+20AC, outside the 0..255 byte alphabet at position 1
  a€b
   ^
```

---

## 3. The token kinds

There are six, and that is the whole language.

| Kind | Source | `value` |
|---|---|---|
| `CHAR` | `a`, `7`, `\n`, `\*`, `\x41` | `int`, 0–255 |
| `PIPE` | `\|` | `None` |
| `STAR` | `*` | `None` |
| `LPAREN` | `(` | `None` |
| `RPAREN` | `)` | `None` |
| `EOF` | end of input | `None` |

Concatenation ("a then b") has no syntax as it is implicit in adjacentcy. You just write `ab`. The parser can't look for an operator, instead, after each piece it
peeks at the next token. If it's a character or `(`, another piece
is starting, so it keeps going. Otherwise the sequence has ended.

The tokeniser always adds one `EOF` token at the end. This means the
parser always has a next token to look at: when it sees `EOF`, it
stops. Without it, every lookahead would need its own "I past the
end or not?" check, and missing one causes a crash.

---

## 4. Tokeniser walkthrough: `tokenize("(a|b)*abb")`

The source, indexed:

```
index:   0 1 2 3 4 5 6 7 8
char:    ( a | b ) * a b b
```

The main loop is a single left-to-right pass with a cursor `i`, which never backtracks.

| step | `i` | sees | action | emits |
|---|---|---|---|---|
| 1 | 0 | `(` | in `_SINGLES` | `LPAREN@0` |
| 2 | 1 | `a` | not an operator, not unsupported → literal byte | `CHAR(97)@1` |
| 3 | 2 | `\|` | in `_SINGLES` | `PIPE@2` |
| 4 | 3 | `b` | literal byte | `CHAR(98)@3` |
| 5 | 4 | `)` | in `_SINGLES` | `RPAREN@4` |
| 6 | 5 | `*` | in `_SINGLES` | `STAR@5` |
| 7–9 | 6–8 | `a` `b` `b` | literal bytes | `CHAR(97)@6` `CHAR(98)@7` `CHAR(98)@8` |
| 10 | 9 | — | loop ends, append sentinel | `EOF@9` |

Evidence of Results:

```
$ python -m streamweave.tokenizer '(a|b)*abb'
 pos  kind     value
----  -------  --------
   0  LPAREN
   1  CHAR     'a'
   2  PIPE
   3  CHAR     'b'
   4  RPAREN
   5  STAR
   6  CHAR     'a'
   7  CHAR     'b'
   8  CHAR     'b'
   9  EOF
```

---

## 5. The code, and all functions explained

### `tokenize` — the main loop

```python
while i < len(pattern):
    start = i
    char = pattern[i]

    if char in _SINGLES:                 # | * ( )
        tokens.append(Token(_SINGLES[char], None, start)); i += 1
    elif char == "\\":
        value, i = _scan_escape(pattern, start)
        tokens.append(Token(TokenKind.CHAR, value, start))
    elif char in _UNSUPPORTED:           # + ? . [ { ^ $
        raise ParseError(_UNSUPPORTED[char], pattern, start)
    else:                                # anything else is a literal byte
        tokens.append(Token(TokenKind.CHAR, _byte(char, pattern, start), start)); i += 1

tokens.append(Token(TokenKind.EOF, None, len(pattern)))
```

Four branches, and every one is decided by **a single character**. Nothing looks ahead more than one
character, and nothing ever backs up. That property is what makes this a tokeniser rather than a
parser, and it is why the whole thing is O(n) with no state machine to speak of.

Notice the assignment `value, i = _scan_escape(pattern, i)`. The helper returns the byte and the new position. There's no i += 1 here on purpose: the helper already set i, because only it knows whether the escape was 2 characters (\*) or 4 (\x41).

### `_scan_escape` — the only multi-character token

```python
char = pattern[i + 1]
if char in _CONTROL_ESCAPES:   return _CONTROL_ESCAPES[char], i + 2   # \n \t \r \f \v \0
if char in _META_ESCAPES:      return _byte(char, ...), i + 2         # \* \| \( \\ \+ ...
if char.lower() in _SHORTHAND_NAMES:                                  # \d \D \w \W \s \S
    raise ParseError(f"the shorthand class '\\{char}' is not supported in v1", ...)
if char == "x":                return _scan_hex(pattern, i)           # \x41
raise ParseError(f"unknown escape sequence '\\{char}'", pattern, i)
```

`_META_ESCAPES` includes characters like `+ ? . [ ] { } ^ $ -`,
even though none of them are operators in v1. A operation `+` is
rejected, so without `\+` there would be no way to match a plus
sign. The rule: any character that is refused on its own can still
be written with a backslash.
to_pattern
`render_byte` does the reverse, turning bytes back into pattern
text, and escapes the same set of characters. This is why
`to_pattern` always produces text that parses back correctly. The
two tables live in the same file because they must always match.

---

## 6. What the tokeniser deliberately does *not* do

**It does not reject the empty pattern.** `tokenize("")` returns `[EOF]`, which is the correct token
stream for empty input. There were no tokens, and here is the sentinel. The empty regex is
forbidden by the *grammar*: `concatenation := repetition+` requires at least one operand. So the
parser raises that error. Putting the check here would mean the tokeniser knows something about
grammar, and the next person would not know where to look for it.

**It does not check that parentheses balance.** `tokenize("(a")` happily returns `LPAREN CHAR EOF`.
Balance is a *structural* property; it needs a recursion, and the parser already has one.

**It does not check that `*` has an operand.** `tokenize("*a")` returns `STAR CHAR EOF`. That `*` has
nothing to repeat, but discovering that requires knowing what an operand is.

The tokeniser only catches mistakes you can see in a few characters,
like a `\` at the very end, an escape that doesn't exist, or a
feature v1 doesn't support. Anything that depends on the overall
shape of the pattern, such as brackets that don't match up, is the
parser's job.

### Errors that name the construct

Some characters, like `+`, `?`, `.`, `[`, `{`, `^` and `$`, mean
something in ordinary regex but aren't supported in v1. Rather than
a vague "unexpected character" error, the tokeniser says which
feature you tried to use, and suggests a workaround where one exists:

    the '+' operator is not supported in v1 (write XX* instead of X+) at position 1
      a+
       ^

    character classes '[...]' are not supported in v1 at position 0
      [a-z]
      ^

All errors are raised as a `ParseError`, which stores the message,
the pattern and the position, and prints a `^` under the problem.
User input is never checked with `assert`, because Python's `-O`
flag removes asserts, and the checks would silently disappear.

---

## 7. Why this language, and what it costs

## 7. Why this language, and what it costs

The hardware is a one-hot NFA, where every NFA
state is a flip-flop, and all of them update in parallel each clock.
So each construct has a direct cost in hardware.

v1 supports exactly the four constructs Thompson's construction
defines a rule for:

| Construct | Thompson rule | States added |
|---|---|---|
| `a` (literal) | one transition between two states | 2 |
| `ab` (concatenation) | an ε edge joins the two parts | 0 |
| `a\|b` (alternation) | a split state and a join state | 2 |
| `a*` (star) | a loop-back edge and a bypass edge | 2 |

Four constructs, four rules, no special cases. That's enough to write
almost any regular expression, and it's the quickest way to a working
pipeline: parser, NFA, SystemVerilog, and a passing cocotb test on
real RTL.

### Planned for v2

- **`+` and `?`.** `a+` is `a*` without the bypass; `a?` is `a*`
  without the loop. Each gets its own rule and costs 2 states, the
  same as `*`. Rewriting `X+` as `XX*` instead would copy the whole
  of `X`: `(abc)+` would take 14 states instead of 8.
- **Character classes and `.`.** `[a-z]` as one node is a single
  transition that checks a range, `(in_byte >= 8'h61) && (in_byte <= 8'h7a)`,
  for 2 states. Written as `a|b|…|z` it would take 102. `.` works
  the same way: one transition that always matches, instead of 256
  alternatives.

`+` and `?` come first because they're easy. Classes and `.` need
more work in the tokeniser, but give far more expressive power for
almost no extra hardware.

---

## 8. Deliberate choices, written down

- **Unknown escapes are errors.** PCRE lets `\q` mean a literal `q`. Here it raises, because a regex
  containing `\q` is far more likely to be a typo than a deliberate literal.
- **`\xHH` is supported.** Without it there is no way to write bytes 128–255 in an ASCII source file,
  which a byte-alphabet machine plainly needs.
- **`]`, `}` and `-` are ordinary literals.** They were only ever special inside character classes,
  and there are no character classes.
- **Shorthands get their own error.** `\d` reports "the shorthand class `\d` is not supported"
  rather than "unknown escape", because the latter would wrongly suggest a typo.

---

## 9. Q&A

**What is the difference between a tokeniser and a parser?**
A tokeniser produces a flat *list* by scanning left to right with bounded lookahead; a parser
produces a *tree* by recursing. Formally, tokenising handles the regular part of the language and
parsing handles the context-free part. Balanced parentheses are the canonical thing a tokeniser
provably cannot check — that is the pumping lemma — which is exactly why `(` and `)` come out as
opaque `LPAREN`/`RPAREN` tokens for the parser to match up.

**Why not implement the tokeniser with Python's `re` module?**
It is circular: using a regex engine to build a regex compiler means the thing you are demonstrating
is already done for you. It also imports the very semantics this project exists to avoid — `re`
supports backreferences and lookahead, which are not regular. `re` is not imported anywhere in
`streamweave/`.

**Why does every token carry a position?**
So every error can point at the right column. A compiler that says "syntax error" without saying
where is a compiler people work around instead of use. The position is captured at the moment the
token is created because that is the only moment the information exists for free — reconstructing it
later means re-scanning.

**Why is the value an `int` and not a `str`?**
The machine reads bytes. Storing `97` hands straight to a comparator in stage 4 with no conversion,
whereas storing characters would mean converting at every boundary and inviting a Unicode bug the
hardware cannot represent.

**Why is `EOF` a token rather than just the end of the list?**
It is a sentinel that removes a bounds check from every single lookahead in the parser. The parser
does `self.tokens[self.pos]` unconditionally; the guarantee that `EOF` is present and last is what
makes that safe.

**Why is there no token for concatenation?**
Because there is no syntax for it — it is implicit in adjacency. The parser recovers it by looping
while the next token *can start an atom*, which is a first-set test rather than an operator match.
That is also why concatenation binds tighter than `|` but looser than `*` without any precedence
table: the shape of the grammar rules encodes it.

**Why can this front end never support backreferences?**
Because `(a*)\1` is not a regular language, and this compiler's entire backend — Thompson
construction to an NFA to one flip-flop per state — can only express regular languages. A
backreference needs unbounded memory of *what* was matched, not just which states are live. A one-hot
NFA has exactly one bit per state and nowhere to put matched text. This is a limitation of the
machine, not of the parser, and it is why stage 5 compares against StreamWeave's own NFA simulator
rather than against `re`.

---

# Part II — The parser

Stage 1b: a flat list of tokens goes in, a **tree** comes out. Part I turned `(a|b)*abb` into a row
of labelled pieces, and this section works out how those pieces fit together.

```
   "(a|b)*abb"
        |
        |   stage 1a   tokenizer.py       flat -- a row of pieces
        v
   [LPAREN][CHAR a][PIPE][CHAR b][RPAREN][STAR][CHAR a][CHAR b][CHAR b][EOF]
        |
        |   stage 1b   parser.py          <-- structure appears here
        v
     a tree
        |
        |   stage 2    thompson.py        epsilon-NFA
        v
   one-hot SystemVerilog -> DE10-Lite
```

## 10. What a tree is, and why we need one

A **tree** is just something made of parts that contain smaller parts, like branches on a tree. Branches will have smaller
branches attached...

In the **AST**, each part is an operation, and the parts inside it are
what it works on. In `(a|b)*`, the star contains an alternation,
which contains `a` and `b`. The single characters are at the
bottom, with nothing inside them.

We need one because a regex is *nested* and a token list is *flat*. Look at two patterns:

```
   ab|c        means      (ab) or c
   a(b|c)      means      a followed by (b or c)
```

Both are five-ish tokens in a row. The difference is not *which* pieces are present. It is **which
pieces are associated with which**. A flat list cannot record that. A tree can:

```
   ab|c                       a(b|c)

   Alt                        Concat
   ├── Concat                 ├── Char 'a'
   │   ├── Char 'a'           └── Alt
   │   └── Char 'b'               ├── Char 'b'
   └── Char 'c'                   └── Char 'c'
```

That is the whole point of this stage. Once the tree exists, every later stage does not need 
to worry about text: stage 2 never sees a `|` character, it sees an `Alt` box with two things inside it, knows exactly
how to build an NFA state machine.

The tree is called an **Abstract Syntax Tree**, or AST. *Syntax* because it records the structure of
what was written; *abstract* because it discards the bits of text that existed only to convey that
structure.

### The four kinds of box


| Box | Holds | Written in the regex as |
|---|---|---|
| `Char` | one byte, e.g. `97` | `a` |
| `Concat` | two boxes: `left`, `right` | adjacency — `ab` |
| `Alt` | two boxes: `left`, `right` | `a\|b` |
| `Star` | one box: `child` | `a*` |

In Python, each node is a dataclass: a short way to define a class
with named fields, without writing the setup code yourself.

```python
@dataclass(frozen=True, repr=False)
class Star(Node):
    """`a*` -- match `child` zero or more times."""
    child: Node
```

`frozen=True` means a node can't be changed after it's created. This
matters in stage 2, which holds on to parts of the tree while it
builds the NFA, and nothing should be able to change them halfway
through. `repr=False` is there because the nodes have their own
`__repr__`, which prints them more readably.

Dataclasses also come with `==`, which compares two trees to see 
if have exactly the same
shape, and that's what every test in `tests/test_parser.py` checks.

`Concat` and `Alt` always have exactly two children. Thompson's rules
for both take two inputs, so each node becomes one construction step
in stage 2. If `Alt` held a list of three branches, stage 2 would
just have to split it back into pairs, which is the same result with
more code.

## 11. What a grammar is

Before writing code you write down the rules of the language. Ours is five lines:

```
regex          :=  alternation EOF
alternation    :=  concatenation ( '|' concatenation )*
concatenation  :=  repetition+
repetition     :=  atom '*'*
atom           :=  CHAR | '(' alternation ')'
```

Read `:=` as "is made of", `*` as "zero or more of the thing before it", `+` as "one or more". So
line 3 says: *a concatenation is one or more repetitions in a row.* Line 5 says: *an atom is either
a single character, or an open bracket, then a whole alternation, then a close bracket.*

Two things to notice.

**It is recursive.** `atom` refers back to `alternation`, the rule at the top. That circularity is
what lets the language nest to any depth with only five rules — `((((a))))` needs no special
handling, it just goes round the loop four times.

**It is a ladder, not a list.** Each rule is defined in terms of the rule *below* it. This ordering
is the entire reason the parser works, and §13 is about why.

## 12. What recursive descent is

**Recursive descent** is the technique of writing one function per grammar rule, named after the
rule, with each function calling the functions for the rules below it.

That is genuinely the whole idea. No clever algorithm, no table of states, no generated code. The
grammar has five rules; `parser.py` has five functions with the same names in the same order. You
can put the grammar and the code side by side and read across.

```python
def parse_alternation(self) -> Node:
    """`alternation := concatenation ( '|' concatenation )*` -- loosest."""
    node = self.parse_concatenation()
    while self.at(TokenKind.PIPE):
        self.advance()
        node = Alt(node, self.parse_concatenation())   # left-associative
    return node
```

Compare that to the rule: *a concatenation, then zero or more of (`|` then another concatenation)*.
The first line handles "a concatenation". The `while` handles "zero or more of". The `Alt(...)` is
where a box gets built. Nothing else is happening.

**Recursive** because these functions form a circle: `parse_alternation` calls `parse_concatenation`
calls `parse_repetition` calls `parse_atom` — and if `parse_atom` sees a `(`, it calls
`parse_alternation` again, right back at the top. That single back-edge is how nesting works.
Python's own call stack does the bookkeeping of remembering where each level was up to, a job we
would otherwise do by hand with an explicit stack.

**Descent** because the calls only ever go one direction, down the ladder. A rule never calls the
rule above it, except through that one bracket case.

### The cursor

The functions share one piece of state: an integer saying which token we have reached.

```python
class Parser:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern            # kept ONLY for error messages
        self.tokens = tokenize(pattern)   # the one seam with stage 1a
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]      # safe forever: the list ends in EOF

    def at(self, kind: TokenKind) -> bool:
        return self.peek().kind is kind

    def advance(self) -> Token:
        token = self.peek()
        self.pos += 1
        return token
```

`peek` looks at the current token without consuming it; `advance` consumes it. That is the entire
interface between the rule functions and the token list.

This deliberately mirrors Part I: the tokeniser walks *characters* with an integer cursor, the
parser walks *tokens* with an integer cursor. Same shape, one level up.

## 13. The precedence ladder — the central idea

**Precedence** means: when two operators compete for the same operand, which wins? In `ab|c`, does
the `b` belong to the `a` (concatenation) or to the `|`? Everyone agrees `|` is loosest, `*`
tightest, concatenation in between — which is why `ab|c` means `(ab)|c`.

Most explanations of precedence involve a table of numbers. **There is no such table in
`parser.py`.** Precedence is encoded in the order the functions call each other:

```
   parse_alternation      LOOSEST      handles  |
          | calls
          v
   parse_concatenation                 handles  adjacency (ab)
          | calls
          v
   parse_repetition       TIGHTEST     handles  *
          | calls
          v
   parse_atom                          a CHAR, or '(' ------+
          |                                                 |
          +-------------------------------------------------+
                 '(' calls back to the top -- the recursion
```

### Why lower on the ladder binds tighter

This is the part worth slowing down for.

Whoever is called **last** gets to look at the tokens **first**, and so grabs its operand before any
caller above it has a chance. `parse_repetition` sits at the bottom, so when it runs it sees only
the single atom immediately to the left of a `*` — everything else is still in the future, unread.
It therefore cannot possibly attach the `*` to anything larger. Meanwhile `parse_alternation` runs
outermost: by the time it decides anything, every `*` and every adjacency below it has already been
assembled into finished boxes, so all it can do is split those finished pieces apart at the `|`.

Tight binding is simply *"you were asked first, and there was less on the table when you were
asked"*.

Check it against `ab|c*`:

```
   ab|c*   ->   Alt( Concat(a, b), Star(c) )        correct
           NOT  Concat(a, Alt(b, Star(c)))
           NOT  Star(Alt(Concat(a,b), c))
```

To add an operator with a precedence between two existing ones, you insert a new function between
two existing ones in the chain. That is the only change required. No table to update, no numbers to
renumber.

## 14. The two loops that are easy to get wrong

### Concatenation has no operator

Every other rule can loop on "did I just see my symbol?" — `parse_alternation` looks for `|`,
`parse_repetition` looks for `*`. Concatenation has no symbol. There is no character meaning "then".
`ab` is a concatenation purely because `b` is written next to `a`.

So the loop condition is a different kind of question: **can the next token *begin* an atom?**

```python
ATOM_START = frozenset({TokenKind.CHAR, TokenKind.LPAREN})

node = self.parse_repetition()
while self.peek().kind in ATOM_START:
    node = Concat(node, self.parse_repetition())
return node
```

`ATOM_START` is the **first set** of `atom` — the set of tokens that could legally appear first in
one. Only two of the six token kinds are in it. The elegance is that all three ways a concatenation
can end fall out of that single condition:

| Next token | Why the loop stops | Who handles it instead |
|---|---|---|
| `\|` | not in `ATOM_START` | `parse_alternation`, one level up |
| `)` | not in `ATOM_START` | the enclosing `parse_atom` |
| `EOF` | not in `ATOM_START` | `parse()`'s final check |

The function returns and leaves that token untouched, for whichever caller further up the ladder
knows what to do with it. This is the standard answer to "how do you parse an invisible operator",
and it is why the token stream needs no `CONCAT` kind: adjacency is recovered by asking what comes
next, not by finding a symbol that was never written.

There is also no emptiness check here, and none is needed — `parse_repetition` calls `parse_atom`,
which already raises when the current token cannot start an atom.

### Postfix `*` is a loop, not recursion

`a**` is legal. It means the same as `a*`, but it is legal, so the parser must handle it. Parse the
atom once, then spin:

```python
node = self.parse_atom()
while self.at(TokenKind.STAR):
    self.advance()
    node = Star(node)
```

Each turn wraps whatever we have so far in another `Star`. Recursing into `parse_repetition` instead
would either loop forever on the zero-star case or need an extra lookahead hack, so the loop is not
a stylistic choice.

Note what this does **not** do: it does not notice that `Star(Star(a))` is the same language as
`Star(a)` and collapse it. That is deliberate. This stage's only job is to represent exactly what
was written; simplification is a later pass's job, and keeping them apart means a bug in one can
never be mistaken for a bug in the other. *A parser that quietly improves its input is a parser you
cannot trust to tell you what its input was.*

## 15. Full trace: `parse("(a|b)*abb")`

Stage 1a hands us this list. The number after `@` is the position in the original text, which every
error message will reuse:

```
   0         1          2        3          4          5        6          7          8          9
[LPAREN@0][CHAR a@1][PIPE@2][CHAR b@3][RPAREN@4][STAR@5][CHAR a@6][CHAR b@7][CHAR b@8][EOF@9]
```

`parse` first checks the list is not just `[EOF]` (it is not), then calls `parse_alternation`. Read
top to bottom; indentation is call depth, and `=>` marks what a function returns.

```
parse_alternation                       cursor at 0
| parse_concatenation                   cursor at 0
| | parse_repetition                    cursor at 0
| | | parse_atom                        cursor at 0, sees LPAREN
| | | | consume '(' , remember position 0
| | | | next token is CHAR, not RPAREN, so the group is not empty
| | | | parse_alternation               cursor at 1     <-- THE RECURSION
| | | | | parse_concatenation           cursor at 1
| | | | | | parse_repetition            cursor at 1
| | | | | | | parse_atom  consume CHAR a  => Char 'a'
| | | | | | | next is PIPE, not STAR, loop does not run
| | | | | | => Char 'a'                 cursor at 2
| | | | | | next is PIPE -- not in ATOM_START -- concatenation ends
| | | | | => Char 'a'
| | | | | sees PIPE, consumes it        cursor at 3
| | | | | parse_concatenation           cursor at 3
| | | | | | => Char 'b'                 cursor at 4
| | | | | next is RPAREN -- not PIPE -- alternation ends
| | | | => Alt(Char 'a', Char 'b')
| | | | expect RPAREN -- it is there, consume it        cursor at 5
| | | => Alt(Char 'a', Char 'b')        <-- the '(' and ')' produced no box
| | | sees STAR, consumes it            cursor at 6
| | | next is CHAR, not STAR, loop ends
| | => Star(Alt(Char 'a', Char 'b'))
| | next is CHAR a -- IS in ATOM_START -- so concatenation continues
| | parse_repetition  => Char 'a'       cursor at 7
| | so far: Concat( Star(Alt(a,b)), Char 'a' )
| | next is CHAR b -- continue
| | parse_repetition  => Char 'b'       cursor at 8
| | so far: Concat( Concat( Star(Alt(a,b)), 'a' ), 'b' )
| | next is CHAR b -- continue
| | parse_repetition  => Char 'b'       cursor at 9
| | next is EOF -- not in ATOM_START -- concatenation ends
| => that Concat
| next is EOF, not PIPE -- alternation ends
=> that Concat
```

Back in `parse`, the cursor is on `EOF`, so nothing is left over and the tree is returned:

```
$ python -m streamweave.parser '(a|b)*abb'
Concat
├── Concat
│   ├── Concat
│   │   ├── Star
│   │   │   └── Alt
│   │   │       ├── Char 'a'
│   │   │       └── Char 'b'
│   │   └── Char 'a'
│   └── Char 'b'
└── Char 'b'
nodes: 10
estimated NFA states: 14
```

Three moments in that trace are worth naming:

1. **The recursion at cursor 1.** `parse_atom` called `parse_alternation`, the rule at the very top
   of the ladder. Inside the brackets, precedence starts again from scratch — which is precisely
   what brackets are *for*.
2. **The `*` at cursor 5 attached to the finished `Alt`,** not to the `b`, because by then the
   bracketed group had already been reduced to one box. The bracket did its work by changing what
   was sitting to the left of the `*` when `parse_repetition` looked.
3. **The concatenation loop ran three times without ever seeing an operator,** purely on the
   first-set test.

## 16. Why `(a)` produces no node

`parse("(a)")` returns `Char('a')` — the same tree as `parse("a")`. There is no `Group` node in the
AST at all, and the brackets vanish.

They vanish because they have already done their entire job. A bracket's only purpose is to
*redirect the parser*: to say "start the precedence ladder again from the top here". Once
`parse_atom` has made that recursive call, the effect is permanently baked into the shape of the
tree. Keeping a `Group` box afterwards would record the *notation* rather than the *meaning*, and
the whole idea of an **abstract** syntax tree is to keep only the meaning.

The proof that nothing was lost is that you can always regenerate the brackets — which is what the
next section is about, and where a real bug was hiding.

## 17. `to_pattern`, and the bug an exhaustive sweep found

`to_pattern()` walks a tree and writes regex source back out, inserting brackets exactly where they
are needed and nowhere else:

```
   Star(Concat(a, b))     ->   (ab)*      Concat is looser than Star, so brackets
   Concat(Star(a), b)     ->   a*b        Star is tighter than Concat, so none
   Concat(Alt(a, b), c)   ->   (a|b)c     Alt is looser than Concat, so brackets
   Alt(Concat(a, b), c)   ->   ab|c       Concat is tighter than Alt, so none
```

The contract is `parse(to_pattern(parse(p))) == parse(p)`: the tree holds everything the text held.
Over a hundred hand-written tests asserted it and passed. Then an **exhaustive sweep** over every
pattern up to length 8 in the alphabet `a b | * ( )` — 2,015,539 inputs — found 592 failures:

```
a(bc)   ->  Concat(a, Concat(b, c))     right-nested, because the brackets forced it
        ->  to_pattern printed "abc"
        ->  which re-parses as Concat(Concat(a, b), c)      DIFFERENT TREE
```

Both operators fold left-associatively (§18), so **bare source can only ever produce left-nested
trees**. A right-nested tree therefore means the source had brackets, and dropping them on the way
out silently reshapes it. The fix is an asymmetry that reads oddly until you see why:

| Node | Left operand needs brackets when | Right operand needs brackets when |
|---|---|---|
| `Alt` | never | it is an `Alt` |
| `Concat` | it is an `Alt` | it is an `Alt` **or a `Concat`** |
| `Star` | *(child)* it is an `Alt` or a `Concat` | — |

So `a(bc)` stays `a(bc)`, and `abc` stays `abc`.

The lasting lesson is about testing, not about brackets: **round-tripping is a property, and
properties want exhaustive or randomised checks, not examples.** The sweep now lives in the suite,
covering every pattern up to length 6 and asserting that `parse` either returns an AST that
round-trips exactly or raises `ParseError` — never anything else.

## 18. Why the tree leans left, and what it costs in hardware

`abc` parses to `Concat(Concat(a, b), c)`, not `Concat(a, Concat(b, c))`. Drawn out, a long
concatenation stair-steps down the left-hand side and looks lopsided. That is not a bug, for two
separate reasons.

**It means the same thing.** Concatenation is associative — `(ab)c` and `a(bc)` match exactly the
same strings — so the two shapes describe the same language. The same is true of `|`.

**It costs nothing in hardware.** This is the reason that actually matters for StreamWeave. In the
one-hot backend, stage 2 allocates two flip-flops for each `Char`, `Alt` and `Star` box, and **zero**
for a `Concat`:

```
   NFA states  =  2 x (number of boxes that are not Concat)
```

`Concat` is free because concatenation in Thompson's construction is just *wiring*: you connect the
exit of the left fragment to the entrance of the right one. No new state represents "and then". So
however deeply the `Concat` boxes stack up, the flip-flop count does not move. For `(a|b)*abb`: 10
boxes, 3 of them `Concat`, so 7 x 2 = **14 flip-flops**.

That is why `parser.py` ships both `size()` and `state_estimate()`. `size()` is the raw box count;
`state_estimate()` is the flip-flop count the generated RTL will have — available now, before a line
of SystemVerilog exists. When Thompson construction lands and reports its own number, the two must
agree, and `test_demo_pattern_state_estimate` pins the value at 14 so a disagreement is immediately
visible.

What *does* matter is that left-leaning is applied **consistently** — to `Alt` as well as `Concat`,
always. That consistency is what lets the tests assert exact tree equality rather than some fuzzy
structural comparison.

## 19. The seam with the tokeniser

The two stages meet at exactly one line, `self.tokens = tokenize(pattern)`. The contract across that
line has two halves.

### Three guarantees the parser relies on

| Guarantee from stage 1a | What the parser therefore does not do |
|---|---|
| The token list ends with **exactly one `EOF`** | Never bounds-checks. `peek()` is a bare list index and can never run off the end, because there is always one more token to sit on. |
| Every `CHAR` value is an `int` in 0..255, already decoded (`\n` arrived as `10`, `\x41` as `65`) | Never calls `ord()`, never re-validates the range, never looks at the pattern text. It writes `Char(token.value)` and moves on. |
| Every token carries a correct 0-based `position` | Never counts characters. Every error reuses `token.position` and gets a correct caret for free. |

The `EOF` guarantee is doing quiet work everywhere. A parser that must ask "is there another token?"
before every check ends up twice the size and has a bug in whichever branch you forgot.

There is a test, `test_parser_does_not_revalidate_bytes`, whose only job is to fail if someone later
adds a byte-range check here. Duplicated validation is worse than none: it drifts, and then two
parts of the compiler disagree about what is legal.

### Four checks the tokeniser deliberately left behind

The tokeniser reports *what characters are present*. It never judges whether the arrangement makes
sense. So all of these are the parser's to reject:

- **The empty pattern.** `tokenize("")` returns `[EOF]`.
- **Unbalanced brackets.** `tokenize("((((")` happily returns four `LPAREN`s.
- **Operators with no operand.** `*a`, `a|`, `|a` all tokenise cleanly.
- **Everything else about shape** — nesting, precedence, ordering.

### Why `tokenize("") == [EOF]` is correct, not a missing error

This looks like a hole. It is not.

`[EOF]` is the *correct and complete* token list for the empty string: there are no characters, so
there are no tokens, and then the sentinel is appended as always. Nothing about "an empty regex is
meaningless" is a fact about *characters* — it is a fact about the **grammar**, specifically that
`concatenation := repetition+` requires at least one operand. Grammar facts belong to the parser, so
`parse("")` is what raises `empty pattern`.

The general rule: **each check lives in the one stage that owns the concept it is about.** Put the
empty check in the tokeniser and you have a module that sometimes reasons about characters and
sometimes about grammar, and the boundary stops being trustworthy. A useful smell test — if you find
yourself writing a character-level check in `parser.py`, you are editing the wrong file.

## 20. Where errors come from

`parse_atom` is where malformed patterns are finally noticed, because it is the only rule that
*demands* a specific token. Every other rule tolerates absence: "zero or more `|` clauses" is
satisfied by zero of them. `parse_atom` cannot shrug — it needs either a `CHAR` or a `(`, and if it
has neither, the pattern is broken.

| Input | Message | Caret |
|---|---|---|
| `""` | `empty pattern` | 0 |
| `*a` | `'*' has no operand to repeat` | at the `*` |
| `\|a` | `'\|' has no left-hand alternative` | at the `\|` |
| `a\|` | `expected a character or '(' after '\|'` | at the end |
| `a)` | `unmatched ')'` | at the `)` |
| `(a` | `unclosed '('` | **at the opening bracket** |
| `()` | `empty group` | at the `(` |

Two of those rows are more interesting than they look.

**`(a` points at the bracket, not at the end of the input.** When the closing bracket is missing, the
*place where you notice* is the end of the string, but the *place where you went wrong* is the
bracket you never closed. So `parse_atom` keeps hold of the `LPAREN` token it consumed and uses its
position:

```python
opening = self.advance()
...
if not self.at(TokenKind.RPAREN):
    raise self._unclosed(opening)      # ParseError("unclosed '('", pattern, opening.position)
```

For `((((((a` the caret lands on the innermost unclosed bracket, which is the one you actually need
to look at. Pointing at the end of the string would be technically true and practically useless.

**`a)` is caught at the top level, not in `parse_atom`.** Nothing in the ladder is offended by a
stray `)`: `parse_concatenation` sees a token that cannot start an atom and stops; `parse_alternation`
sees a token that is not `|` and stops; both return perfectly happily having parsed `a`. It is
`parse()`'s final check — *is the cursor on `EOF`?* — that notices a token is left over, and it words
the message `unmatched ')'` rather than a vague "expected end of pattern".

Finally, `parse()` catches `RecursionError`. A pattern like 5000 nested `(` exhausts Python's call
stack, since the ladder recurses once per bracket. That is re-raised as an ordinary `ParseError`
saying the pattern nests too deeply, so callers only ever have one exception type to catch. Raising
Python's recursion limit instead would just move the crash somewhere less explicable.

## 21. Q&A

**Why recursive descent rather than a Pratt / precedence-climbing parser?**

Pratt parsing keeps precedence in a table of numbers that one loop consults, which pays off when
there are many operators or when users can define new ones. This language has three, fixed forever.
Against that, recursive descent has the property that the code and the grammar are visibly the same
five rules in the same order — you can read `parser.py` next to the grammar and check the
correspondence line by line. For a compiler whose point is partly to be *explained*, that
readability is worth more than a table lookup, and there is no precedence table that could be wrong
because there is no table.

**How does your grammar encode precedence, given there is no precedence table in the code?**

By the order the functions call each other. Each rule is defined in terms of the rule below it, so
the functions form a chain: alternation, concatenation, repetition, atom. Whoever is called last
reads the tokens first and so claims its operand while the least is on the table, which makes it
bind tightest. Precedence is therefore a property of the *call graph*, not of any data.

**Concatenation has no operator — how does the parser know where one ends?**

By a **first-set test**. `parse_concatenation` loops for as long as the next token could *begin* an
atom, which is only `CHAR` or `LPAREN`. When it meets `PIPE`, `RPAREN`, `STAR` or `EOF` — none of
which can start an atom — the run is over, and it returns leaving that token unconsumed for a caller
further up the ladder. The boundary is found by asking what comes *next*, rather than by recognising
a symbol that was never written.

**Why does `(a)` produce no node?**

Because the bracket's job finished the moment it caused `parse_atom` to recurse back to the top of
the ladder. That recursion is permanently recorded in the shape of the tree, so a `Group` box would
carry no information — it would record notation rather than meaning, which is exactly what an
*abstract* syntax tree is meant to discard. Nothing is lost: `to_pattern()` regenerates the brackets
from the shape alone, and the round-trip sweep proves it.

**What does `a**` do, and why don't you simplify it?**

It produces `Star(Star(Char('a')))` — two nested boxes, not one. It is not collapsed to `Star(a)`
even though the two match identical strings, because this stage's contract is to represent exactly
what was written. Simplification belongs to a later pass, and there is a real benefit to keeping
them apart: when a pattern produces more flip-flops than expected, you can tell at a glance whether
the parser mis-read it or the optimiser failed to shrink it.

**Why is your AST binary rather than n-ary?**

Because Thompson's construction defines exactly one *two-input* rule for concatenation and one for
alternation. A binary node maps to one construction step with no loop in stage 2, so the translation
stays a direct transcription of the textbook rules. Keeping it binary also makes
`2 x (non-Concat count)` an exact flip-flop prediction rather than an estimate.

**Why can this parser never support backreferences, no matter how much code you add?**

Because backreferences are not regular, and this is not a limitation of the parser — it is a
limitation of the machine at the other end of the pipeline. A pattern like `(a*)b\1` requires
matching the same arbitrary-length string twice, which means *remembering* what the first group
captured. A finite automaton has, by definition, finitely many states, so it can only remember a
bounded amount; there is no bound on what `(a*)` might capture. Formally, `{ww | w in Sigma*}` is
not regular and no NFA or DFA recognises it.

On the hardware this matters twice over. The backend is one flip-flop per NFA state, fixed at
synthesis time — there is nowhere to *put* a captured string, and the circuit has no memory beyond
those flip-flops and no way to loop back and re-read the input. Supporting backreferences would mean
abandoning the automaton model for a backtracking engine, which is a different program with
different and much worse worst-case timing. So `\1` is not a feature this project has yet to
implement; it is outside the class of languages the whole design can express.

## 22. Files

```
streamweave/
├── __init__.py        empty (a docstring only)
├── tokenizer.py       stage 1a. ParseError, TokenKind, Token, tokenize, render_byte, dump, CLI
└── parser.py          stage 1b. Char, Concat, Alt, Star, Parser, parse,
                       to_pattern, pretty, size, state_estimate, CLI
tests/
├── test_tokenizer.py  64 tests
└── test_parser.py     139 tests, including the exhaustive sweep
```

One module per stage. `ParseError` lives in `tokenizer.py` and `parser.py` imports it rather than
sitting in a third file — the dependency runs one way, from the higher layer to the lower, so a
shared module would buy nothing but another file.

Inside `parser.py` the AST classes and the parser share a file but not a section: everything above
the `THE PARSER` banner is pure data with no parsing logic, so stage 2 can import `Char`, `Concat`,
`Alt` and `Star` and ignore the `Parser` class entirely.

`__init__.py` is deliberately empty. Re-exporting these names from it would make
`python -m streamweave.tokenizer` load that module twice and emit a runpy warning, so callers import
from the submodules directly:

```python
from streamweave.tokenizer import tokenize, ParseError
from streamweave.parser import parse, pretty, size, state_estimate, to_pattern
```

`parse(pattern) -> Node` is the entire public API of stage 1. Stage 2 imports only that function and
the four node classes.

Try it:

```bash
python -m streamweave.parser '(a|b)*abb'
python -m streamweave.parser '(a'
```

The first prints the tree, the box count and the flip-flop estimate. The second prints a caret under
the bracket you forgot to close, and exits 1.

Next: **Thompson construction** — each of the four node types gets one rule that builds a small NFA
fragment, and the fragments compose. Remember the frozen-dataclass trap from §10 before you start:
allocate a fresh state on every visit, and never memoise on node value.
