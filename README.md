# slate-harbor

Select and schedule regression tests for changes to a pinned Python source tree. Each input patch is applied independently against the original source.

```sh
docker build -t slate-harbor .
docker run --rm --network none -v "$PWD/data:/data:ro" slate-harbor
```

The data directory contains `commits/*.patch` and a `tests.json` manifest. Each numbered test has its pytest node ID, source file and runtime in seconds. The program emits one JSON line per patch with its ID and the tests assigned to each worker.

The build uses SQLGlot at revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` under its MIT license. The dependency versions in `requirements.lock` are provided with the reference environment. The selection and scheduling code is original. [SQLGlot source and license](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa)

To regenerate the observation tables, use CPython 3.13 with the locked dependencies, a pristine copy of the pinned source, and the numbered test manifest. The ordered-comparison recorder also needs Python development headers and a C compiler. The generated tables are placed in the scratch directory's `solution/` subfolder.

```sh
python profiles/rebuild.py shared isolated owners direct positions code-readers test-methods paths class-fixtures relations ordered-relations \
  --source /path/to/sqlglot --manifest /path/to/tests.json --work /path/to/scratch
```

The code-reader stage records the current operating system. Supply `--peer-observations /path/to/peer/lab` with independently collected Linux or macOS reports to combine observations across both environments. Missing evidence retains the broader selection.
