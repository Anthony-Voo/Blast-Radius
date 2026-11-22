# Blast Radius entry

Picks the sqlglot tests a patch can break, and splits them across workers.

How it works:

1. **Maps from the pinned commit** (`map/`, built by the scripts in `mapbuild/`):
   per-test line coverage of `sqlglot/` and `tests/` (including work done in
   process-pool workers), the fixture files each test opens, the dialect /
   parser / generator / expression classes each test instantiates, and the
   modules each test file imports. Nothing is keyed on a patch or a kind of edit.
2. **Static analysis** (`blast/analyze.py`): the patch is applied in memory and
   compared with the original syntax tree. Changed statements inside functions
   map to the tests that executed them; changes that run at import time map to
   the tests that use the enclosing class or execute code that mentions the
   changed name; fixture changes map to the tests that open the file.
3. **Verification** (`blast/runner.py`): when the candidate set is cheap enough
   to be worth the CPU, the patch is applied to a scratch copy of sqlglot in
   `/tmp` and the candidates are run with `unittest`; only failures are kept.
4. **Splitting** (`selector.py`): longest-first packing over 1..8 workers,
   keeping the cheapest of busiest worker + 2 s per worker.

`make sample` builds the image and scores it on the 30 sample commits.
