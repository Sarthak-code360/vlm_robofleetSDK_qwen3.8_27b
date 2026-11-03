# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Set up

```sh
git clone https://github.com/Sarthak-code360/vlm_robofleetSDK_qwen3.8_27b.git && cd vlm_robofleetSDK_qwen3.8_27b
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[printer]"
python -m unittest discover -s tests -t .
```

The tests need no model, printer or network.

## Guidelines

- **Keep the core dependency-free.** `robofleet` (outside `robofleet.printers`) uses only the standard library.
  Optional hardware support goes behind an extra in `pyproject.toml`.
- **The model proposes, code verifies.** Anything the model outputs that reaches hardware must pass a checker
  in code (see `validator.py`, `printers/checks.py`, `printers/chooser.py`). Add a test for each new rule.
- **Keep humans in the loop for physical actions.** Do not remove confirmations such as the `PRINT` prompt.
- **No secrets in code, tests or examples.** Use placeholders like `ACCESS_CODE` and environment variables.
- Add or update tests in `tests/` for every change, and update `README.md` / `docs/` when behaviour changes.
- Add a line to `CHANGELOG.md` under "Unreleased".

## Pull requests

1. Fork and create a branch: `git checkout -b my-change`.
2. Make the change, run the tests.
3. Open a pull request describing what changed and why. CI runs the tests on Python 3.10–3.13.
