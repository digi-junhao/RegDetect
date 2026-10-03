# RegDetect log

## Week 0 (setup)
- Built environment: WSL, Verilator, cocotb, Git/GitHub all working.
- Smoke test: [PASS / still fixing]


---

## Week 1 (Handwritten Matcher)
-- Created working and tested hardware for the Detection of the phrase "GET".
-- Created a mealy model instead of moore model as the delay is one byte less

Match if only HIGH if STATE = GE, in_byte = T, and in_valid is high
This is done through an AND GATE

#### Why require in_valid?
Data may not go in every clock cycle
There will always be some voltage in the wires, so in_byte allows us to know if the 
8 bit value is a "real byte" or a "left over byte".


**File can be found in /rtl/handwritten_pattern.sv**

To improve accuracy of detector, the state machine can go to 3 possible states after a clock cycle
1. State Machine totally resets (idle_state)
2. Advances to the next accepting state
3. PARTIALLY RESET TO A LOWER STATE NOT THE IDLE STATE. eg.(GEG), after G it would not totally reset





## Week 2
- Done in depth research on the structure of the project
- Looked into Thompsons construction.
- Learnt about regular expressions
- Learnt about Deterministic Finite Autonoma (DFA), and Non-Deterministic Finite Autonoma (NFA)
- Hand construction from ReGex to NFA
- Learnt about epsilon closure

In a DFA, for every input there is exactly one next state
- uses binary encoding

However for NFA, for every input there can be more than 1 valid state. Later inputs decide 
which path was correct
- uses one-hot encoded so there can be multiple active states. Eg. state 000101 means the machine is in both state 0 and state 1 at the same time.
-  Every NFA state gets its own flip-flop and all states are evaluated in parallel every cycle. 


Explanation of Epsilon Closure














