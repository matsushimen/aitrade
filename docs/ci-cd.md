# CI/CD

## Continuous integration

GitHub Actions runs on pull requests to `main`, pushes to `main` and `ci/**`,
and manual dispatch. The reusable `checks.yml` workflow is also called by releases.

- Python 3.11, 3.12, 3.13 and 3.14: compilation, unit tests and CLI integration tests.
- Ruff: fatal syntax, undefined-name and control-flow checks; development-only dependency.
- actionlint: GitHub Actions workflow syntax and expressions (fixed version and SHA256).
- Validate `config/baseline.json` and check whitespace.
- `Required checks` fails if any underlying check fails or is cancelled.

Tests use synthetic data, temporary SQLite databases and mocked LLM responses.
They do not need bitbank, an LLM endpoint, secrets or collected `data/` files.
Live market validation remains a separate manual procedure in the
[aitrade-validation skill](../.agents/skills/aitrade-validation/SKILL.md).

The application runtime still uses only Python's standard library. Locally:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m ruff check scripts tests
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -v
```

Actions are pinned to commit SHAs. Dependabot proposes weekly updates to Actions
and development dependencies. PR jobs have read-only repository permissions,
do not inherit secrets, and do not persist checkout credentials.

For merge enforcement, configure a branch rule for `main` requiring the
`Required checks` status from the CI workflow. Adding a workflow alone does not
enable branch protection. No repository administration settings are changed by
these files.

## Continuous delivery

There is no configured application host or daemon deployment target.
Delivery currently means distributing verified source; it does not start a
trading process, call an LLM, or submit orders.

After successful checks on a `main` push, the `Source distribution` job uploads
`aitrade.tar.gz` and `SHA256SUMS` as a GitHub Actions artifact retained for 14 days.
Archives use `git archive`, so untracked market data, state and credentials are
not packaged.

To prepare a versioned release, tag a verified commit on `main`:

```sh
git tag v0.1.0
git push origin v0.1.0
```

`Release` reruns the same checks, accepts only `vMAJOR.MINOR.PATCH` tags, verifies
that the tagged commit is on `main`, builds a source archive and SHA256 checksum,
and creates a **draft** GitHub Release with generated notes and attached files.
Review and publish the draft in GitHub Releases. Only that delivery job receives
`contents: write`; no personal access token is required.

If delivery fails after a draft was created, inspect the existing draft before
retrying; the workflow deliberately does not overwrite an existing release.
No release tag is created automatically. Runtime deployment and persistent paper
trading infrastructure will require a specified host and state/monitoring setup.
