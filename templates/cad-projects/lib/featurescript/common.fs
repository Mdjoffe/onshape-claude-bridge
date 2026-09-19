FeatureScript 2600;
import(path : "onshape/std/geometry.fs", version : "2600.0");

/**
 * Shared constants and helpers for every project in this repo.
 * Cut an Onshape version after changing this, then bump consumers.
 */

// Printer-specific clearance used across the toolchanger projects.
export const PRINT_CLEARANCE = 0.2 * millimeter;

export function nozzleSafeRadius(radius is ValueWithUnits) returns ValueWithUnits
{
    return radius + PRINT_CLEARANCE;
}
