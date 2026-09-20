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
| `push <path> [--dry-run] [--capture DIR]` | Upload FeatureScript from git into Onshape |
| `pull <path> [--dry-run] [--capture DIR]` | Download configured exports into the repo |

`<path>` accepts a single `onshape.yml`, a project directory, or a tree to walk.
`elements` exists because element ids are the tedious part of writing a config —
run it once and copy the ids out.

Both sync directions compare before writing: identical content reports
`unchanged` and makes no API call, so re-running in CI creates no Onshape
microversions and no empty git diffs.

Calls go to **`/api/v10`** by default rather than to an unversioned path.
Onshape resolves an unversioned URL to whatever it considers oldest -- measured
as `v1`, though the docs claim `v0` -- which is a moving target nobody chose.
From v10 onward, configuration endpoints reject bad visibility conditions with
a 400 instead of silently repairing them, and failing is the cheaper outcome:
Onshape does not meter 4xx, so a rejection costs nothing while a silent repair
costs a wrong result you may not notice. Override with `ONSHAPE_BASE_URL` or
`--base-url`.

Every run reports `Onshape API calls this run: N` as its last line, alongside
the API version the server answered as and how many calls that endpoint has left
in its rate-limit window. The Onshape Free plan meters 2500 calls per user per
year, so the commands are built to spend as few as possible and to say how many
they spent.

Only metered responses are counted. Onshape charges for 2xx and 3xx and
explicitly does not charge for 4xx or 5xx, so a failed call is free and the
counter does not move. A `429` is a per-endpoint rate limit rather than the
annual allowance: the client waits the `Retry-After` the server names and
retries once, or raises if that wait is longer than `max_retry_after`.

`push --assume-changed` skips the comparison read and uploads unconditionally:
1 call per file instead of 2. Use it when git already established the file
changed -- a manual sync you triggered because you edited something. The cost of
being wrong is one needless Onshape microversion.

`--capture DIR` writes each write-response to a file. A metered call spends
part of a yearly allowance and its response cannot be had again for free, so
printing it to a CI log is not keeping it -- logs expire. The result line also
names the keys the response arrived with, minus `contents`, which is only the
source handed back. That matters most for a Feature Studio write: it is the one
place a compile complaint could surface, and nothing in Onshape's guides says
what such a field is called, so the run reports what actually came rather than
hunting for a name we guessed.

Export polling backs off (2s, 4s, 8s ... capped at 30s) rather than hammering a
flat interval, which takes a five-minute export from about 150 calls to a dozen.

Cheaper still, where it fits: `export_part_studio_stl` uses Onshape's
*synchronous* STL export, which answers with a 307 to the finished file. Two
calls, against the dozen a translation job costs, in exchange for no control
over tessellation. The client follows that redirect itself rather than letting
`requests` do it, so both hops are counted -- and it withholds the API keys when
the redirect leaves Onshape's host, since storage URLs carry their own
authorization.

`list_documents` searches an account and follows `next` until the results run
out, bounded by `max_pages` so a search that matches more than expected stops
rather than emptying the year's allowance a page at a time. Each page is one
call and holds at most 20 documents.

`create_version` freezes a workspace under a name. Naming it after the commit
that produced it is what ties Onshape's history to git's, for the geometry git
cannot hold.

Configurations are read with `get_configuration`, written with
`update_configuration`, and turned into the strings other calls want by
`encode_configuration`. `configuration_options()` exists for one trap: each
option carries both an `optionName` for people ("500 mm") and an `option` for
the API (`_500_mm`), and only the second is accepted as a value.

`export_part_studio_stl` takes a `configuration`, which must be the `encodedId`
rather than the `queryParam`. The two differ by a leading `configuration=`, and
sending the wrong one produces a doubly-encoded parameter that Onshape reads as
a different configuration.

Metadata -- tab names, part numbers, descriptions, custom properties -- is the
second thing in Onshape that is text, and the only other thing that could
sensibly live in git. `element_metadata` / `part_metadata` read it and
`update_element_metadata` / `update_part_metadata` write it.

Writes are keyed by `propertyId`, because that is what the API takes and
resolving a name costs a read. `property_ids()` turns a metadata response into a
name-to-id map worth caching: read once, keep the map, and later writes are one
call each instead of two.

`feature_health` is the cheapest check that a custom feature actually works:
one call returns every feature in a Part Studio paired with the status Onshape
last regenerated it to, so a FeatureScript that failed to compile shows up with
a status other than `OK`.

`part_studio_mass_properties` covers every body in a Part Studio in one call --
mass, volume, centroid, inertia -- rather than one call per part. Each figure
arrives as `[value, lower, upper]`, where the bounds are Onshape's tolerance on
it. `hasMass` stays false until a material is assigned.

`evaluate_featurescript` runs a FeatureScript lambda against a Part Studio and
returns whatever it returns. That is one call for as many measurements as the
lambda cares to compute, rather than one REST call each -- `tight_bounding_box`
is the worked example, and exists because Onshape's own bounding-box endpoint is
documented as approximate. Note the constraint: **only lambdas evaluate here**,
so this cannot be used to check that a Feature Studio's source compiles.

`translator_formats()` lists every format Onshape will accept or produce. One
call, and it turns a guessed `formatName` into a checked one before a
translation is started.

A project whose ids are still the scaffold's `REPLACE_WITH_...` placeholders
reports `unconfigured` and is skipped without contacting Onshape. That is what
lets you point one project at a real document while the rest of the repo stays
unconfigured -- without it, the first unconfigured project 404s and takes the
whole run down with it.

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
