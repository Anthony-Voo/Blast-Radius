# Blast Radius entry

Picks the sqlglot tests a patch can break, and splits them across workers.

## How it works

1. **Maps from the pinned commit** (`map/`, built by the scripts in `mapbuild/`).
   One traced run of the test suite at `eb11aec` records, per test:
   - the lines of `sqlglot/` and `tests/` it executes, including work done in
     process-pool workers and subprocesses;
   - the fixture and source files it opens;
   - the dialect / tokenizer / parser / generator / expression classes it
     instantiates;
   - and, separately, which lines run and which classes are instantiated while
     each module is being imported, plus which modules each test file imports.

   Nothing in the maps is keyed on a patch or on a kind of edit.

2. **Static analysis** (`blast/analyze.py`). The patch is applied in memory and
   the old and new syntax trees are compared. A change with an identical tree
   selects nothing. Otherwise each changed line is classified:
   - inside a function body: the tests that executed that statement, plus the
     users of any module whose import executes it;
   - a class-level statement: the tests that instantiate that class (narrowed to
     those that also execute code reading the changed attribute, when every read
     happens at run time), plus users of modules that instantiate it on import;
   - other import-time statements: tests executing code that mentions the bound
     names;
   - fixtures and data files: the tests that open them.

3. **Evidence** (`blast/policy.py`, `blast/runner.py`, `blast/hot.py`). When the
   expected saving beats the CPU cost, some candidates are run against the
   patched code with `unittest`:
   - small candidate sets are run completely and only failures are kept;
   - for large sets, a cheap subset is chosen that covers every line any
     candidate executes in the changed function. If any of it fails, every
     unrun candidate is kept. If a large set fails broadly, every test is
     selected (shared state is likely corrupted). Only when all evidence passes
     are the slowest unrun tests dropped.
   - Patches that only rewrite function bodies are checked in a pre-loaded
     sqlglot process by swapping the new code objects into the live functions;
     everything else gets a fresh import of a patched copy in `/tmp`.

4. **Splitting** (`blast/policy.py`). Longest-first packing over 1..8 workers,
   keeping the cheapest of busiest worker + 2 s per worker.

## Layout

| Path | What |
| --- | --- |
| `selector.py` | Entry point: analyse every commit, gather evidence, print answers |
| `blast/patchlib.py` | Unified-diff parser and applier |
| `blast/analyze.py` | Patch to candidate tests |
| `blast/policy.py` | Cost model: run or emit, which tests to run, what to keep |
| `blast/runner.py` | Runs tests against a patched tree in a forked process |
| `blast/hot.py` | Pre-loaded worker that swaps changed function bodies in place |
| `map/` | Maps built from the pinned commit |
| `mapbuild/` | The tracer and scripts that built `map/` |
| `tools/fetch_sqlglot.py` | Build step: download sqlglot at the pinned commit |

`make sample` builds the image and scores it on the 30 sample commits.
