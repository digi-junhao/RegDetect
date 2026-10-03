# Stage 3 — epsilon elimination

Stage 3: an **epsilon-NFA** goes in, an **epsilon-free NFA** comes out. Same states, same numbering,
same language — every edge now consumes exactly one byte.

```
   epsilon-NFA          14 states,  5 symbol edges,  11 epsilon edges
        |
        |   stage 3   epsilon.py        <-- THIS DOCUMENT
        v
   epsilon-free NFA     14 states, 22 symbol edges,   0 epsilon edges
        |
        |   stage 4   one-hot codegen
        v
   SystemVerilog -> DE10-Lite
```

---

## 1. Why this stage is mandatory

Stage 2 built epsilon edges deliberately, because they let each construction rule stay local. This
stage exists because **they cannot survive into hardware.**

An epsilon edge means "move without consuming a byte". In a design that reads one byte per clock,
that means "move **without a clock edge**". A state is a flip-flop; a transition is logic feeding the
next flip-flop's input. An epsilon transition has no clock edge to hang on, so it would have to be
wired as pure combinational logic *between* two flip-flops.

Now look at what `Star` builds:

```
   body_accept --eps--> body_start        (go round again)
```

For `(a*)*` the inner and outer stars chain their epsilon edges into a **cycle**. Wired
combinationally that is a *zero-delay feedback loop* — a wire whose value depends on itself with no
register in between. Verilator refuses to simulate it (`UNOPTFLAT`); Quartus refuses to synthesise
it. There is no clock, no settling, no defined value.

So the epsilon closures are computed **here, at build time**, on a laptop, once. What reaches stage 4
has every edge consuming exactly one byte and therefore taking exactly one clock cycle.

This is the general shape of a compiler doing its job: *the awkward construct exists to make the
front end simple, and is discharged before it reaches the target.*

## 2. What an epsilon closure is

The **epsilon closure** of a state is every state you can drift to for free:

```
   closure(p) = { p } union { everything reachable from p by epsilon edges alone }
```

For `a*` (stage 2 gives states 0..3, start 0, accept 1, with `0->1`, `0->2`, `2 --a--> 3`, `3->1`,
`3->2`, epsilons unlabelled):

```
   closure(0) = {0, 1, 2}      from 0 you can drift to the exit or into the body
   closure(1) = {1}            the exit goes nowhere
   closure(2) = {2}            the body's entry has no epsilon edges out
   closure(3) = {1, 2, 3}      from the body's exit: leave, or loop back
```

It is computed with a worklist and a `seen` set, not recursion — `(a*)*` has genuine epsilon
*cycles*, and a naive recursive walk would never terminate.

## 3. The rewrite

For every state `p` and every byte `c`:

```
   new_delta(p, c)  =  { e.target : e.source in closure(p),  e.symbol == c }
```

Read it as: **from `p`, drift for free wherever you can, then consume one `c`.**

And the accept condition becomes:

```
   accepting  =  { p : the original accept state is in closure(p) }
```

That is the whole algorithm. Fifteen lines:

```python
closures = [nfa.epsilon_closure({state}) for state in range(nfa.state_count)]

edges = {
    SymbolEdge(state, edge.symbol, edge.target)
    for state in range(nfa.state_count)
    for edge in nfa.symbol_edges
    if edge.source in closures[state]
}

accepting = frozenset(
    state for state in range(nfa.state_count) if nfa.accept in closures[state]
)
```

Note the edges are collected into a **set**. Several states' closures overlap, so the same
`(source, symbol, target)` triple is generated repeatedly; the set deduplicates it. Two states that
can reach the same symbol edge each get their own copy of it with themselves as the source.

## 4. Why the closure goes *before* the byte, not after

There are two standard formulations, and the difference decides what stage 4's reset looks like.

**What this module does — closure before:**

```
   new_delta(p, c) = move(closure(p), c)          accept becomes a SET
   start stays a single state
```

**The alternative — closure before and after:**

```
   new_delta(p, c) = closure(move(closure(p), c))  accept stays a single state
   start must become a SET, namely closure(start)
```

Both are correct. The second keeps a single accept state, which looks tidier — but it forces the
machine to *begin* in a whole set of states. In hardware that means **reset has to load an arbitrary
bit pattern** across the flip-flops rather than setting exactly one.

The first pushes the cost onto the output instead: `accepting` is a set, so the match signal is an OR
across several flip-flops.

```systemverilog
   match = state[3] | state[7] | state[11];     // combinational, free
```

A wider OR is combinational logic and costs essentially nothing. A multi-bit reset value is real
state that has to be held and driven. **Trading a multi-bit reset for a wider OR is the better deal**,
so this module takes the closure before the byte.

Why it still works, briefly. Writing `S_n` for the epsilon-NFA's live set after `n` bytes and `A_n`
for the epsilon-free machine's, the invariant is `closure(A_n) = S_n`. The drift that the second
formulation performs *after* byte `n` is instead performed by the `closure(p)` at the start of step
`n+1`. Nothing is lost; it is just done a moment later. At the end, "did we accept?" asks whether
any live state's closure contains the original accept — which is exactly the `accepting` set.

## 5. Worked by hand: `a*`

Using the closures from §2. The only symbol edge in the machine is `2 --a--> 3`, so a state gets an
outgoing `a` exactly when its closure contains state 2:

| `p` | `closure(p)` | contains 2? | new edge | contains accept (1)? |
|---|---|---|---|---|
| 0 | `{0,1,2}` | yes | `0 --a--> 3` | **yes** |
| 1 | `{1}` | no | — | **yes** |
| 2 | `{2}` | yes | `2 --a--> 3` | no |
| 3 | `{1,2,3}` | yes | `3 --a--> 3` | **yes** |

```
$ python -m RegDetect.epsilon 'a*'
states: 4   start: 0   accepting: [0, 1, 3]
symbol edges: 3   epsilon edges: 0   unreachable: 2

  from      on    to
  ----  ------  ----
     0     'a'     3   <- start  -> accepting
     2     'a'     3   -> accepting
     3     'a'     3   -> accepting
```

Three things to read off that.

**`0` is accepting.** The start state accepts, which is exactly how "matches the empty string" is
expressed once epsilon edges are gone. `a*` matches `""`, and there is no longer an epsilon edge to
drift along to say so — the property has been baked into the accepting set.

**`3 --a--> 3` is a self-loop, and that is fine.** In stage 2 the loop was `3 --eps--> 2` and
`2 --a--> 3`: a cycle containing an epsilon. Now it is a single edge that consumes a byte, so in
hardware it is a flip-flop feeding its own input *through a register*. Registered feedback is
ordinary sequential logic. Unregistered feedback is the thing that could not be built.

**One symbol edge became three.** This is the usual direction of travel: elimination trades epsilon
edges for more symbol edges. The state count — the flip-flop count — does not move.

## 6. Worked example: `(a|b)*abb`

```
$ python -m RegDetect.epsilon '(a|b)*abb'
states: 14   start: 0   accepting: [13]
symbol edges: 22   epsilon edges: 0   unreachable: 8

  from      on    to
  ----  ------  ----
     0     'a'     5   <- start
     0     'a'     9   <- start
     0     'b'     7   <- start
     1     'a'     9
     2     'a'     5
     ...
    11     'b'    13   -> accepting
    12     'b'    13   -> accepting
```

The three edges out of state 0 are the whole story of nondeterminism in one place. On an `a`, the
machine goes to **both** state 5 (still looping inside `(a|b)*`) and state 9 (one byte into matching
`abb`). It does not choose. Both flip-flops set, both threads carried forward, and whichever turns
out to be right survives.

Totals across the rewrite:

| | states | symbol edges | epsilon edges |
|---|---|---|---|
| after stage 2 | 14 | 5 | 11 |
| after stage 3 | 14 | 22 | **0** |

## 7. What gets left behind

Eight of those fourteen states are now **unreachable**:

```
   reachable: [0, 5, 7, 9, 11, 13]
   dead     : [1, 2, 3, 4, 6, 8, 10, 12]
```

They were pure epsilon scaffolding — the split and join states `Alt` allocated, the enter and exit
states `Star` allocated. Their *edges* have been rewritten away; their *state numbers* remain.

Keeping them is deliberate, for two reasons:

- **The project does no state minimisation.** The NFA is emitted as-is.
- **Stable numbering across stages.** State 9 in the generated SystemVerilog is state 9 in stage 2's
  listing and state 9 here. When something is wrong on the FPGA, that traceability is worth a great
  deal.

But be clear-eyed about the price: **8 of 14 flip-flops here carry no information.** Removing
unreachable states is *dead-code elimination*, not minimisation — a different thing from the
Hopcroft/Moore algorithms the project rules out — and it would cut this machine from 14 flip-flops to
6. `reachable_states()` is provided so the number is visible; nothing calls it automatically. Whether
stage 4 prunes before emitting is a decision that has not been made yet, and it is worth making
deliberately rather than by default.

## 8. This is not a subset construction

Worth stating plainly, because the machinery looks identical.

Subset construction *also* computes epsilon closures. It uses them to build a **DFA**, where each DFA
state is a **set** of NFA states — which is where the exponential blow-up comes from: *n* NFA states
can produce up to 2^n DFA states.

Epsilon elimination uses the same closures to rewrite an NFA into **another NFA with the same
states**. 14 in, 14 out. No set-of-states anywhere; `accepting` is a set of ordinary states, not a
state that *is* a set.

```
   subset construction:   NFA --> DFA,  up to 2^n states,  one state live at a time
   epsilon elimination:   NFA --> NFA,  exactly n states,  many states live at once
```

There is no DFA anywhere in this project, and there is no hook for one.

## 9. How this is tested

The rewrite must be **language-preserving**. That is a property, so it is checked as one.

`test_three_implementations_agree` runs every string in `{a,b}*` up to length 6 through three
implementations that share no code:

1. the **AST oracle** from `test_thompson.py` — slices strings, knows nothing about automata,
2. the **epsilon-NFA** from stage 2,
3. the **epsilon-free NFA** from this stage.

```python
expected = ast_matches(tree, text)
assert nfa.matches(text) == expected,        "stage 2 wrong"
assert flattened.matches(text) == expected,  "elimination changed the language"
```

Twenty-eight patterns × 127 strings. Comparing stages 2 and 3 alone would catch a broken rewrite;
keeping the oracle in catches the case where stages 2 and 3 are wrong *together*, which is the
failure a same-family comparison always misses.

The oracle is **imported** from `test_thompson`, not copied. Two copies of an oracle can drift apart,
and at that point neither is trustworthy.

## 10. Q&A

**Why build epsilon edges in stage 2 if stage 3 just removes them?**

Because they make each construction rule *local*. Without epsilons, `Alt` would have to reach inside
both children, find their entry states and merge them — which means knowing how those children were
built, destroying the "children are opaque boxes" invariant that keeps stage 2 to four short rules.
Build correctly first, optimise second. The rewrite here is mechanical and operates on a machine
already known to be correct.

**Does elimination change the language?**

No, and that is the whole contract. It changes *which edges exist*, never *which strings are
accepted*. `test_three_implementations_agree` is what holds it to that.

**Why does the edge count go up?**

Because one epsilon edge can be the shared prefix of many paths. When a state's closure covers three
states that each have an outgoing `a`, that state acquires three `a` edges of its own. Edges are
combinational logic — cheap, parallel, and not on the critical path in the same way state is. Trading
edges for the removal of epsilons is exactly the trade the hardware wants.

**Why is `accepting` a set, when stage 2 had a single accept state?**

Because the closure is taken before the byte rather than after (§4). The last drift the epsilon
machine would have done never happens, so instead of asking "are we *on* accept?" the machine asks
"could we *drift to* accept from here?" — and that is true of several states. It costs one OR gate.

**A self-loop like `3 --a--> 3` still looks like feedback. Why is that allowed?**

Because it is *registered* feedback. The value of flip-flop 3 on the next cycle depends on its value
on this cycle, with a clock edge in between — that is what a flip-flop is for, and every counter ever
built does it. The forbidden thing is a *combinational* path from a signal back to itself with no
register, which is what an epsilon cycle would have been.

**Could a pattern produce an epsilon closure that never terminates?**

The cycles are real — `(a*)*` has them by construction — but the closure is a worklist with a `seen`
set, so each state is enqueued at most once and the walk terminates after at most one pass per state.
`test_closure_terminates_on_an_epsilon_cycle` in the stage 2 suite pins it.

## 11. Files

```
RegDetect/
├── __init__.py        empty (a docstring only)
├── tokenizer.py       stage 1a -- text -> tokens
├── parser.py          stage 1b -- tokens -> AST
├── thompson.py        stage 2  -- AST -> epsilon-NFA
└── epsilon.py         stage 3  -- epsilon-NFA -> epsilon-free NFA
                       EpsilonFreeNFA, eliminate, compile_pattern, CLI
tests/
├── test_tokenizer.py   64 tests
├── test_parser.py     139 tests
├── test_thompson.py    73 tests
└── test_epsilon.py     62 tests
```

`eliminate(nfa) -> EpsilonFreeNFA` is the public API of stage 3, and
`compile_pattern(pattern)` runs the whole pipeline from source: tokenise, parse, construct,
eliminate.

`EpsilonFreeNFA.incoming()` is the shape stage 4 wants — for each state, the `(source, symbol)` pairs
that can enter it, which is literally the next-state equation:

```
   next_state[i] = OR over (j, c) of ( state[j] AND in_byte == c )
```

Try it:

```bash
python -m RegDetect.epsilon 'a*'
python -m RegDetect.epsilon '(a|b)*abb'
python -m RegDetect.epsilon '(a|b)*abb' aabb
```

Run the stage 2 CLI on the same pattern first to see what changed.

Next: **one-hot codegen** — one flip-flop per state, `incoming()` turned into a next-state equation
per state, and a `match` output that ORs the accepting flip-flops.
