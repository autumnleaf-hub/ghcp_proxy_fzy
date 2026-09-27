# Tests

All root-level Python regression tests live in this package. Production modules
and resources such as `dashboard.html` remain in the repository root.

Run commands from the repository root with the repo-local virtual environment.
The suite uses the standard-library `unittest` runner; pytest is not required.

## Targeted checks

Windows:

```powershell
./.venv/Scripts/python.exe -X utf8 -B -m unittest tests.test_excel_upstream -v
```

macOS / Linux:

```sh
./.venv/bin/python -B -m unittest tests.test_excel_upstream -v
```

## Full discovery

Windows:

```powershell
./.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -t . -v
```

macOS / Linux:

```sh
./.venv/bin/python -B -m unittest discover -s tests -t . -v
```

Keep `-t .` so discovery imports modules as `tests.test_*`, consistently with
package-qualified imports between test modules. Use module invocations rather
than executing test files directly.

Tests requiring attachment libraries use `requirements-attachments.txt` and
`requirements-e2e.txt`; some optional checks may skip when dependencies or
platform tools are unavailable. Dashboard JavaScript checks require Node.js.

For new tests, import shared test helpers through `tests.test_*` and locate
production resources with `Path(__file__).resolve().parents[1]`. Keep generated
artifacts under temporary directories; do not use the `mutants/` workspace.
