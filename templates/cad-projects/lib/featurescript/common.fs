FeatureScript 2600;
import(path : "onshape/std/geometry.fs", version : "2600.0");

/**
 * Constants and helpers shared across projects in this repo.
 *
 * Cut an Onshape version after changing anything here, then bump the version
 * in each consuming project's import -- consumers pin a version, so they will
 * not pick up edits until you do.
 */

// Slop allowance for parts that have to fit together off the printer.
export const FIT_CLEARANCE = 0.2 * millimeter;

export function withClearance(dimension is ValueWithUnits) returns ValueWithUnits
{
    return dimension + FIT_CLEARANCE;
}
