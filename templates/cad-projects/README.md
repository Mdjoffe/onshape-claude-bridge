# cad-projects

CAD projects that sync against Onshape through
[onshape-claude-bridge](https://github.com/Mdjoffe/onshape-claude-bridge).

## Layout

```
lib/                      shared FeatureScript, its own Onshape document
projects/test-bracket/    throwaway project for verifying the sync works
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

1. Create an API key pair at <https://cad.onshape.com/appstore/dev-portal> →
   **API keys**. Tick read *and* write on documents; the secret is shown once.
2. Add both as repository secrets `ONSHAPE_ACCESS_KEY` and `ONSHAPE_SECRET_KEY`,
   under Settings → Secrets and variables → Actions.
3. Confirm they work: Actions → **Onshape doctor** → Run workflow. It prints the
   authenticated account, or says why Onshape rejected the keys. Nothing else in
   this repo works until that run is green.

Prefer a shell? `pip install git+https://github.com/Mdjoffe/onshape-claude-bridge.git`
then `onshape-bridge doctor` runs the same check locally.

## Starting a project

`projects/test-bracket/` is a throwaway parametric bracket wired up for exactly
this — use it to confirm the sync works before trusting the bridge with a design
you care about. Its README walks the round trip.

```sh
cp -r projects/test-bracket projects/my-thing
onshape-bridge elements projects/my-thing   # after filling in the document ids
onshape-bridge push projects/my-thing --dry-run
```

## CI

The Onshape Free plan meters roughly **2500 API calls per user per year** —
about seven a day — so this repo spends them deliberately. See `API_BUDGET.md`
for the running ledger.

`.github/workflows/sync.yml` has two jobs:

- **pull request** → `validate` only. Parses every `onshape.yml` without
  touching the network, so it costs nothing and still catches a broken config.
- **manual run** → `sync`. Choose direction (`push`/`pull`), optionally one
  project, and whether to skip the comparison read. `pull` commits exports back.

There is deliberately **no push trigger**. Syncing on every commit would spend
around four calls a run asking whether anything changed; at a few pushes a week
that is a large slice of the annual allowance consumed by polling.

`.github/workflows/doctor.yml` is manual-only and checks the credentials. It is
stdlib-only and installs nothing, so a failure there means Onshape rejected the
keys rather than something upstream having broken. Costs 1 call.

### What a run costs

| Operation | Calls |
| --- | --- |
| Unconfigured project (placeholder ids) | 0 |
| `validate` | 0 |
| Push, unchanged | 1 |
| Push, changed | 2 |
| Push with **assume changed** | 1 |
| `doctor` | 1 |
| Export (`pull`) | 2 + one per poll |

Every run prints `Onshape API calls this run: N` as its last line — copy it into
`API_BUDGET.md`. Failed calls are free and are not counted: Onshape meters 2xx
and 3xx only. The same line reports which API version answered, which is worth
reading if your base URL carries no version segment.

**Exports are the expensive path.** Polling backs off (2s, 4s, 8s … capped at
30s), so a five-minute export costs about a dozen calls rather than 150. Still
worth running deliberately rather than on a schedule.

## Exports and repo size

`.gitignore` excludes exports by default, so they stay CI artifacts. If you want
them tracked, delete the matching pattern from `.gitignore` and enable Git LFS —
`.gitattributes` already has the filters staged for that. Binary CAD in plain git
history is what makes a repo like this unpleasant two years in.
