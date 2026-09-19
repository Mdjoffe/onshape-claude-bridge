# cad-projects

CAD projects that sync against Onshape through
[onshape-claude-bridge](https://github.com/Mdjoffe/onshape-claude-bridge).

## Layout

```
lib/                      shared FeatureScript, its own Onshape document
projects/<name>/
  onshape.yml             repo paths -> Onshape document/workspace/element ids
  featurescript/*.fs      git is the source of truth; CI pushes these
  exports/                CI writes these out of Onshape
  docs/                   notes, photos, print settings
```

One repo rather than one per project, because these projects share a
FeatureScript library and a single Onshape API key pair. CI is path-filtered, so
touching one project syncs only that project. Split a project out when it earns
it: it goes public on its own, picks up outside contributors, or its exports get
heavy.

## What syncs which way

| Direction | What moves |
| --- | --- |
| git → Onshape | FeatureScript, custom features |
| Onshape → git | STEP/STL/3MF exports, drawings, BOMs |
| neither | Part Studio geometry drawn in the GUI |

Hand-modeled geometry is Onshape-authored and stays there — Onshape's own
versions and branches are its history. Only put things in `featurescript/` that
you are willing to have CI overwrite in Onshape.

## Setup

1. Create an API key pair at <https://cad.onshape.com/appstore/dev-portal> → **API keys**.
2. Add them here as repository secrets `ONSHAPE_ACCESS_KEY` and `ONSHAPE_SECRET_KEY`
   (Settings → Secrets and variables → Actions).
3. Locally: `pip install git+https://github.com/Mdjoffe/onshape-claude-bridge.git`
   then `onshape-bridge doctor` to confirm the keys work.

## Starting a project

```sh
cp -r projects/example-project projects/my-thing
onshape-bridge elements projects/my-thing   # after filling in the document ids
onshape-bridge push projects/my-thing --dry-run
```

## CI

`.github/workflows/sync.yml`:

- **pull request** → dry run only; a PR can never mutate an Onshape document
- **push to main** → pushes changed projects' FeatureScript into Onshape
- **manual run** → push or pull, one project or all; `pull` commits exports back

## Exports and repo size

`.gitignore` excludes exports by default, so they stay CI artifacts. If you want
them tracked, delete the matching pattern from `.gitignore` and enable Git LFS —
`.gitattributes` already has the filters staged for that. Binary CAD in plain git
history is what makes a repo like this unpleasant two years in.
