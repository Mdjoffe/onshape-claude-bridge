# test-bracket

A parametric L-bracket that exists to prove the bridge works end to end. Nothing
downstream depends on it, so it is safe to break.

## Wiring it up

1. Create a new Onshape document with a Feature Studio and a Part Studio.
2. Copy the document and workspace ids out of the URL into `onshape.yml`.
3. `onshape-bridge elements projects/test-bracket` — copy the Feature Studio's
   element id into `onshape.yml`.
4. `onshape-bridge push projects/test-bracket --dry-run` — expect `would-update`.
5. Drop `--dry-run`. The Feature Studio in Onshape now holds `bracket.fs`.

## Proving the round trip

Change a default in `bracket.fs` (leg length 40 -> 50, say) and push again. You
should see `updated`, then `unchanged` on a second run — that second result is
the important one, because it means CI re-running will not churn Onshape
microversions.

To test the other direction, insert the feature in the Part Studio to get real
geometry, then uncomment the `exports:` block in `onshape.yml` and run
`onshape-bridge pull projects/test-bracket`.

## Caveat

The FeatureScript here has not been compiled against a live Onshape document —
it is written to the std library conventions but unverified. If Onshape rejects
it, the sync mechanism is still fine; fix the FeatureScript and push again.
