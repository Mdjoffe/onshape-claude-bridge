# onshape-claude-bridge

Sync a git repository against Onshape documents over the REST API. This repo is
the *tooling*; CAD content lives in a separate projects repo that calls this.

## What can and cannot cross the bridge

Onshape keeps geometry in its own cloud, so git can only be the source of truth
for the text parts of a design. Sync therefore has a direction per artifact:

| Direction | What moves |
| --- | --- |
| git → Onshape | FeatureScript (`.fs`), custom feature source |
| Onshape → git | STEP/STL/3MF exports, drawings, BOMs — written by CI |
| neither | Part Studio geometry drawn by hand in the GUI |

Hand-modeled geometry stays Onshape-authored; Onshape's own versions, branches
and merges are the history for it. Don't expect git to round-trip it.

## Setup

```sh
pip install -e .
```

Create an API key pair at <https://cad.onshape.com/appstore/dev-portal> →
**API keys**. The secret is shown exactly once, so capture it then. Grant the
key read *and* write on documents if you intend to push.

```sh
export ONSHAPE_ACCESS_KEY=...
export ONSHAPE_SECRET_KEY=...
onshape-bridge doctor
```

`doctor` is the definitive check that your account and plan allow API access —
it makes one authenticated call and reports who you are. A `401`/`403` means
the keys are wrong, revoked, or missing scopes, and it says so.

## Commands

| Command | Does |
| --- | --- |
| `doctor` | Verify the key pair against the live API |
| `validate <path>` | Parse `onshape.yml` files, no network |
| `elements <path>` | List a document's tabs and their element ids |
| `push <path> [--dry-run]` | Upload FeatureScript from git into Onshape |
| `pull <path> [--dry-run]` | Download configured exports into the repo |

`<path>` accepts a single `onshape.yml`, a project directory, or a tree to walk.
`elements` exists because element ids are the tedious part of writing a config —
run it once and copy the ids out.

Both sync directions compare before writing: identical content reports
`unchanged` and makes no API call, so re-running in CI creates no Onshape
microversions and no empty git diffs.

## Project config

One `onshape.yml` per project directory, in the content repo:

```yaml
project: test-bracket
document:
  id: 1a2b3c...          # from the /documents/<id>/w/<id> part of the URL
  workspace: 4d5e6f...

feature_studios:          # git is the source of truth for these
  - source: featurescript/bracket.fs
    element: 7g8h9i...

exports:                  # Onshape is the source of truth for these
  - element: 0j1k2l...
    kind: partstudios     # partstudios | assemblies | drawings | blobelements
    format: STEP
    output: exports/bracket.step
  - element: 0j1k2l...
    format: STL
    output: exports/bracket.stl
    options:
      mode: binary
      units: millimeter
```

`options` is passed through to the translation request untouched, so any option
the Onshape translation endpoint accepts for that format works without a change
here.

## Shared FeatureScript

FeatureScript imports resolve against Onshape document versions, not file paths,
so a shared library is its own project: a `lib/` directory with its own
`onshape.yml` pointing at a library document. Push it, cut a version in Onshape,
then `import` that version from the projects that consume it.

## Development

```sh
python -m pytest
```

## Content repo template

`templates/cad-projects/` is a ready-to-use scaffold for the companion content
repo: layout, `onshape.yml` examples, LFS-staged `.gitattributes`, and a
path-filtered GitHub Actions workflow that dry-runs on pull requests and pushes
on merge. Copy it into a new empty repo to start.
