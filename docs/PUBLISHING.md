# Publishing a release to PyPI

Releases are published by GitHub Actions ([`.github/workflows/publish.yml`](../.github/workflows/publish.yml)) using
PyPI **trusted publishing**. No PyPI password or token is stored anywhere.

## One-time setup (maintainer)

1. Create accounts on [pypi.org](https://pypi.org/account/register/) and [test.pypi.org](https://test.pypi.org/account/register/),
   and turn on two-factor authentication. PyPI requires it.
2. On PyPI, go to **Your projects > Publishing > Add a new pending publisher** and enter:
   - PyPI project name: `robofleet`
   - Owner: `Sarthak-code360`
   - Repository name: `robofleet`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. Do the same on TestPyPI with environment name `testpypi`.
4. On GitHub, go to **Settings > Environments** and create two environments, `pypi` and `testpypi`. For safety,
   add yourself as a required reviewer on `pypi`.

## Each release

1. Update `version` in `pyproject.toml` and `__version__` in `src/robofleet/__init__.py` (they must match).
2. Move the "Unreleased" notes in `CHANGELOG.md` under the new version.
3. Commit, push, and check that CI is green.
4. **Optional dry run:** go to **Actions > Publish > Run workflow**. This publishes to TestPyPI only. Then check it:
   ```sh
   pip install -i https://test.pypi.org/simple/ robofleet
   ```
5. Create a GitHub release with a tag that matches the version:
   ```sh
   git tag v0.1.0 && git push origin v0.1.0
   ```
   Then open **Releases > Draft a new release**, pick the tag and click **Publish release**. The workflow builds
   the package, checks that the tag matches the version, and uploads it to PyPI.
6. Check it: `pip install robofleet==0.1.0`.

A version number can only be uploaded once. To fix a bad release, publish a new version.

## Build locally (optional)

```sh
pip install build twine
python -m build          # creates dist/*.whl and dist/*.tar.gz
twine check dist/*
```
