# Stage 2 — Thompson construction

Stage 2 of the compiler: an **abstract syntax tree** goes in, an **epsilon-NFA** comes out.

Stage 1 (see `frontend_walkthrough.md`) turned `(a|b)*abb` into a tree of four kinds of box. This
stage turns that tree into a machine — a set of numbered states and the edges between them. Stage 3
will strip the epsilon edges out; stage 4 turns what remains into SystemVerilog, one flip-flop per
state.

```
   a tree
        |
        |   stage 2   thompson.py       <-- THIS DOCUMENT
        v
   epsilon-NFA          14 states, 5 symbol edges, 11 epsilon edges
        |
        |   stage 3   epsilon elimination
        v
   epsilon-free NFA
        |
        |   stage 4   one-hot codegen
        v
   SystemVerilog -> DE10-Lite
```

---

## 1. What an NFA is

A **finite automaton** is a machine with a fixed number of *states*, one of which it is "in" at any
moment. It reads input one byte at a time, and each byte moves it along an *edge* to another state.
If it ends up in the designated **accept** state when the input runs out, the input matched.

"Finite" is the important word, and it is not decoration. The number of states is fixed when the
machine is built and never grows, which is exactly what makes it implementable in hardware: a fixed
number of flip-flops, decided at synthesis time.

The **N** is for **nondeterministic**, which sounds mystical and is not. It means the machine can be
in **several states at once**:

```
   pattern (a|b)*abb, after reading "a"

   the machine is in states {1, 2, 3, 4, 5, 6, 8, 9, 10}
                             ^^^^^^^^^^^^^^^^^^  ^^^^^^
                             "still looping in   "one 'a' into
                              (a|b)*"             matching abb"
```

Both readings are live simultaneously. The machine does not guess which is right and backtrack if it
is wrong — it simply carries every possibility forward in parallel and sees which survive. If any
live thread is sitting on `accept` when the input ends, the string matched.

**This is why the hardware design works at all.** "In several states at once" is awkward in software
but *free* in hardware: give every state its own flip-flop, and "the set of live states" is just the
bit pattern across those flip-flops. All of them update in parallel on the same clock edge. A
deterministic machine (a DFA) would need fewer flip-flops but exponentially more of them to build,
and this project deliberately never constructs one.

## 2. What an epsilon transition is

An ordinary edge says *"if the next byte is `a`, move from state 4 to state 5."* An **epsilon edge**
says *"move from state 0 to state 2, consuming nothing."* It is a free move, taken without reading
any input.

```
   4 --'a'--> 5      consumes one byte
   0 --eps--> 2      consumes nothing; both ends are "the same moment"
```

Epsilon edges buy one thing, and it is the thing that makes this whole stage short: **each rule can
be written without knowing anything about the shape of its children.**

Consider building `a|b`. You need "either the `a` machine or the `b` machine". Without epsilons you
would have to reach into both sub-machines, find their entry states, and merge them — which means
knowing how they were built. With epsilons you make one new state and draw a free edge to each
child's entry. You never look inside.

That locality is why the four rules in §4 are three or four lines each.

## 3. The invariant that makes it compose

Every fragment this module builds has **exactly one entry state and exactly one exit state**.

```
        ┌─────────────┐
   ---> │  fragment   │ --->
  start └─────────────┘  accept
```

That is not a convenience, it is the load-bearing invariant. Because every fragment looks like this
from the outside — one wire in, one wire out — a rule can treat its children as opaque boxes. `Star`
does not care whether its body is one character or a hundred nested alternations; it only needs the
body's two ends.

Every rule below both **relies** on its children having that shape and **preserves** it for its own
parent. That is the entire compositional argument, and it is why the construction is provably
correct by induction over the tree with almost no case analysis.

## 4. The four rules

One rule per AST node type. No special cases, no lookahead, no optimisation.

### `Char(c)` — two states, one edge

```
   s --'c'--> a
```

The only rule that produces a *symbol* edge. Everything else in the machine is plumbing.

```python
start, accept = self.new_state(), self.new_state()
self.symbol_edges.append(SymbolEdge(start, node.code, accept))
return start, accept
```

**Cost: 2 states.**

### `Concat(L, R)` — no new states, one wire

```
   ---> [ L ] --eps--> [ R ] --->
```

Build both children, join the left's exit to the right's entry, and hand back the outer ends.

```python
left_start, left_accept = self.build(node.left)
right_start, right_accept = self.build(node.right)
self.epsilon_edges.append(EpsilonEdge(left_accept, right_start))
return left_start, right_accept
```

**Cost: 0 states.** This is the fact the parser's `state_estimate` was built around: concatenation is
pure wiring, so however deeply `Concat` nodes stack up, the flip-flop count does not move.

### `Alt(L, R)` — two states, four epsilons

```
              ┌--eps--> [ L ] --eps--┐
        s ----┤                      ├----> a
              └--eps--> [ R ] --eps--┘
```

A new entry that splits into both branches, and a new exit both branches rejoin at.

```python
start, accept = self.new_state(), self.new_state()
left_start, left_accept = self.build(node.left)
right_start, right_accept = self.build(node.right)
self.epsilon_edges += [
    EpsilonEdge(start, left_start),     # split into either branch
    EpsilonEdge(start, right_start),
    EpsilonEdge(left_accept, accept),   # rejoin from either branch
    EpsilonEdge(right_accept, accept),
]
```

Both branches become live at once. Neither is tried first; there is no backtracking.

**Cost: 2 states.**

### `Star(B)` — two states, four epsilons

```
                  ┌──────── eps ────────┐   (skip the body entirely)
                  │                     v
             s ---┴--eps--> [ B ] --eps--> a
                              ^      │
                              └─eps──┘   (go round again)
```

```python
start, accept = self.new_state(), self.new_state()
body_start, body_accept = self.build(node.child)
self.epsilon_edges += [
    EpsilonEdge(start, body_start),        # enter the body
    EpsilonEdge(start, accept),            # or skip it entirely  -> matches ""
    EpsilonEdge(body_accept, body_start),  # go round again
    EpsilonEdge(body_accept, accept),      # or leave
]
```

The skip edge is what makes `a*` match the empty string. The loop edge is what makes it match any
number of repetitions. **Cost: 2 states.**

### The arithmetic

```
   states = 2 x (number of nodes that are not Concat)
```

Which is exactly `parser.state_estimate`, written before this module existed. Two independent
formulas — the parser's prediction and what the builder actually allocated — must agree, and
`test_matches_the_parser_estimate` holds them to each other across fourteen patterns. If stage 2
ever drifts, that test fails rather than the discrepancy surfacing as a mysterious flip-flop count
in Quartus three stages later.

## 5. Full trace: `(a|b)*abb`

The tree, from stage 1:

```
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
```

The builder walks it depth-first, allocating states as it goes. `Star` and `Alt` take their own two
states *before* descending, which is why the outermost numbers are the lowest:

| Visit | Node | States allocated | Edges added |
|---|---|---|---|
| 1 | `Star` | 0, 1 | *(deferred until the body exists)* |
| 2 | `Alt` | 2, 3 | *(deferred)* |
| 3 | `Char 'a'` | 4, 5 | `4 --'a'--> 5` |
| 4 | `Char 'b'` | 6, 7 | `6 --'b'--> 7` |
| 5 | *finish `Alt`* | — | `2->4`, `2->6`, `5->3`, `7->3` |
| 6 | *finish `Star`* | — | `0->2`, `0->1`, `3->2`, `3->1` |
| 7 | `Char 'a'` | 8, 9 | `8 --'a'--> 9` |
| 8 | *`Concat`* | — | `1->8` |
| 9 | `Char 'b'` | 10, 11 | `10 --'b'--> 11` |
| 10 | *`Concat`* | — | `9->10` |
| 11 | `Char 'b'` | 12, 13 | `12 --'b'--> 13` |
| 12 | *`Concat`* | — | `11->12` |

14 states, 5 symbol edges, 11 epsilon edges. From the CLI:

```
$ python -m RegDetect.thompson '(a|b)*abb'
states: 14   start: 0   accept: 13
symbol edges: 5   epsilon edges: 11

  from      on    to
  ----  ------  ----
     0     eps     1   <- start
     0     eps     2   <- start
     1     eps     8
     2     eps     4
     2     eps     6
     3     eps     1
     3     eps     2
     4     'a'     5
     5     eps     3
     6     'b'     7
     7     eps     3
     8     'a'     9
     9     eps    10
    10     'b'    11
    11     eps    12
    12     'b'    13   -> accept
```

Read a few of those against the rules:

- **`0 -> 1` and `0 -> 2`** are `Star`'s skip and enter. State 0 is the star's entry, 1 its exit.
- **`3 -> 1` and `3 -> 2`** are `Star`'s leave and loop, from the `Alt`'s exit.
- **`2 -> 4` and `2 -> 6`** are `Alt`'s split; **`5 -> 3` and `7 -> 3`** its rejoin.
- **`1 -> 8`, `9 -> 10`, `11 -> 12`** are the three `Concat` wires, one per `Concat` node.

### Running it

Matching `"aabb"` means tracking the live set. Start with the epsilon closure of `{0}` — everything
reachable for free:

```
   start        eps-closure of {0}      = {0, 1, 2, 4, 6, 8}
   read 'a'     from 4 -> 5,  from 8 -> 9
                eps-closure of {5, 9}   = {1, 2, 3, 4, 5, 6, 8, 9, 10}
   read 'a'     from 4 -> 5,  from 8 -> 9
                eps-closure of {5, 9}   = {1, 2, 3, 4, 5, 6, 8, 9, 10}
   read 'b'     from 6 -> 7,  from 10 -> 11
                eps-closure of {7, 11}  = {1, 2, 3, 4, 6, 7, 8, 11, 12}
   read 'b'     from 6 -> 7,  from 12 -> 13
                eps-closure of {7, 13}  = {1, 2, 3, 4, 6, 7, 8, 13}
                                                                ^^
   13 is the accept state, and the input is finished  ->  MATCH
```

Notice the machine was tracking "still looping in `(a|b)*`" **and** "part-way through `abb`"
simultaneously the whole time. That is nondeterminism doing its job, and it is why no backtracking
is needed.

```bash
python -m RegDetect.thompson '(a|b)*abb' aabb
```

## 6. Why epsilons are fine here and fatal in hardware

This module deliberately produces something stage 3 must destroy.

An epsilon edge means "move without consuming a byte" — that is, **without a clock cycle**. In
hardware, a state is a flip-flop and a transition is logic feeding the next flip-flop. An epsilon
edge has no clock edge attached to it, so it would have to be wired as pure combinational logic
between two flip-flops.

Now look at `Star`'s loop edge: `body_accept --eps--> body_start`. Combined with the body's own
epsilon edges, `(a*)*` produces a **cycle of epsilon edges**. Wired combinationally, that is a
zero-delay feedback loop — a wire whose value depends on itself with no register in between.
Verilator refuses to simulate it (`UNOPTFLAT`) and Quartus refuses to synthesise it.

So stage 3 computes the epsilon closures **at build time** and rewrites every real transition to
skip the epsilon edges:

```
   before:   8 --'a'--> 9,   9 --eps--> 10,   10 --'b'--> 11
   after:    8 --'a'--> 9  and, wherever 9 was reachable, 10's outgoing edges
             are pulled back so no epsilon remains at runtime
```

The result is an epsilon-*free* NFA with the same language and the same state count, where every
edge consumes exactly one byte and therefore takes exactly one clock cycle.

Worth being precise about one thing: this is a **different** use of epsilon closures from the one
inside subset construction. Subset construction uses closures to build a **DFA**, with a potential
exponential blow-up in states. Stage 3 uses them to rewrite an NFA into another NFA of the *same
size*. There is no DFA anywhere in this project, by design.

## 7. The trap: AST nodes are values, not identities

This is the one place the construction can go silently, badly wrong.

AST nodes are frozen dataclasses, so they compare and hash **by value**:

```python
tree = parse("aa")
tree.left == tree.right          # True
hash(tree.left) == hash(tree.right)   # True -- indistinguishable as values
```

A builder that memoised — "I have seen `Char(97)` before, reuse its fragment" — would emit **one**
two-state fragment where two are needed. `aa` would collapse into `a`, and the bug would not show up
as a crash; it would show up as a machine that accepts the wrong language, three stages later, on an
FPGA.

So `_Builder.build` allocates fresh states on **every visit** and never caches on node value. There
is a test whose only job is to pin this:

```python
def test_identical_subtrees_get_separate_states():
    tree = parse("aa")
    assert tree.left == tree.right           # equal as values...
    nfa = build(tree)
    assert nfa.state_count == 4              # ...but four distinct states
    assert nfa.matches("aa")
    assert not nfa.matches("a")              # the give-away if they merged
```

The general lesson: **value semantics and graph construction are a dangerous pair.** Anywhere you
build a graph from a value-typed tree, "have I seen this before?" is almost never the question you
mean; "have I visited this *position* before?" is.

## 8. How this is tested

Structural tests — state counts, edge counts, endpoints in range — are necessary but weak. They
check that the machine is well-formed, not that it is *right*.

The tests that carry the weight cross-check against an **independent oracle**: a matcher in
`tests/test_thompson.py` that walks the AST by slicing strings and knows nothing about states,
edges or closures.

```python
def _remainders(node, text):
    """The set of suffixes left over after `node` matches a prefix of `text`."""
    if isinstance(node, Char):
        return {text[1:]} if text and ord(text[0]) == node.code else set()
    if isinstance(node, Concat):
        return {r for mid in _remainders(node.left, text)
                  for r in _remainders(node.right, mid)}
    ...
```

Then, for 28 patterns, every string in `{a,b}*` up to length 6 — 127 strings each — is fed to both:

```python
assert nfa.matches(text) == ast_matches(tree, text)
```

Two implementations sharing no code and no data structures, agreeing on several thousand strings, is
much stronger evidence than any number of hand-inspected edge listings. It is the same technique
that caught the `to_pattern` bracket bug in stage 1: **where a property can be checked exhaustively
over a small domain, check it exhaustively.**

`Python's re` is never used as the oracle, here or in stage 5. It supports backreferences and
lookahead, which are not regular and which this hardware correctly cannot implement — comparing
against it would produce fake mismatches.

## 9. Q&A

**Why an NFA and not a DFA? Isn't a DFA faster?**

In software, yes: a DFA is in exactly one state at a time, so matching is one table lookup per byte.
In hardware, the comparison inverts. An NFA's "several states at once" costs nothing, because every
state has its own flip-flop and they all update in parallel on the same clock edge — the machine
does all its work in one cycle regardless of how many states are live. Meanwhile subset construction
can produce exponentially many DFA states, and each one is real silicon. So the NFA is both smaller
and exactly as fast. This is the Sidhu–Prasanna result (FCCM 2001), and it is why there is no subset
construction anywhere in this project.

**What does "nondeterministic" actually mean here — does it guess?**

No, and nothing in this codebase ever backtracks. It means the machine occupies a *set* of states
rather than a single one. When an edge splits, both targets become live and are carried forward
together. "Nondeterministic" describes the *model* — a mathematical machine allowed to have several
next states — not the implementation, which is entirely deterministic and just tracks a set.

**Why does `Concat` cost no states when everything else costs two?**

Because concatenation is not a *choice*, it is a *sequence*. `Alt` needs a state to split at and one
to rejoin at; `Star` needs somewhere to loop back to and somewhere to escape to. Concatenation needs
neither — "do this, then do that" is expressed entirely by connecting the first fragment's exit to
the second's entry. No decision is being represented, so no state is needed to represent it.

**Why build epsilon edges at all, if stage 3 has to remove them?**

Because they make each rule *local*. Without them, `Alt` would have to reach inside both children,
find their entry states and merge them, which means knowing how those children were built —
destroying the "children are opaque boxes" invariant that makes the construction four short rules.
Epsilons let you build correctly first and optimise second, which is nearly always the right order.
Stage 3 removing them is a mechanical rewrite over a machine that is already known to be correct.

**Could `Star`'s loop edge cause an infinite loop in `epsilon_closure`?**

The cycle is real — `(a*)*` has one by construction — but the closure is a worklist with a `seen`
set, so each state is enqueued at most once and the loop terminates after at most one pass per
state. A naive recursion would hang, which is why it is written as a worklist.
`test_closure_terminates_on_an_epsilon_cycle` pins it.

**Is this the same "epsilon closure" as in subset construction?**

The operation is identical; the use is not. Subset construction uses closures to build a **DFA**,
where each DFA state is a *set* of NFA states — hence the exponential blow-up. Stage 3 uses closures
to rewrite an NFA into an epsilon-free NFA with the *same states*. Same tool, different job, and only
one of them changes the size of the machine.

## 10. Files

```
RegDetect/
├── __init__.py        empty (a docstring only)
├── tokenizer.py       stage 1a -- text -> tokens
├── parser.py          stage 1b -- tokens -> AST
└── thompson.py        stage 2  -- AST -> epsilon-NFA
                       SymbolEdge, EpsilonEdge, NFA, build, compile_pattern, CLI
tests/
├── test_tokenizer.py   64 tests
├── test_parser.py     139 tests
└── test_thompson.py    73 tests, including the oracle cross-check
```

`build(node) -> NFA` is the public API of stage 2. `compile_pattern(pattern)` is
`build(parse(pattern))`, for convenience and the CLI.

The `NFA` is a frozen dataclass of tuples, like everything else in the pipeline — hashable,
comparable, and impossible to mutate behind stage 3's back.

Try it:

```bash
python -m RegDetect.thompson 'a*'
python -m RegDetect.thompson '(a|b)*abb'
python -m RegDetect.thompson '(a|b)*abb' aabb
```

The third prints the machine and then tells you whether the string matched.

Next: **epsilon elimination** — compute the closures at build time, rewrite the real transitions to
skip epsilon edges, and hand stage 4 a machine where every edge is exactly one clock cycle.
