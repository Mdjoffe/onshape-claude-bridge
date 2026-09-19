FeatureScript 2600;
import(path : "onshape/std/geometry.fs", version : "2600.0");

/**
 * A parametric L-bracket. This exists to exercise the git -> Onshape sync:
 * change a default below, merge, and confirm the Feature Studio updates.
 *
 * Deliberately self-contained -- it imports nothing from lib/ so you can test
 * the bridge before the shared library document exists.
 */

annotation { "Feature Type Name" : "Test bracket" }
export const testBracket = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Sketch plane", "Filter" : GeometryType.PLANE, "MaxNumberOfPicks" : 1 }
        definition.plane is Query;

        annotation { "Name" : "Leg length" }
        isLength(definition.legLength, { (millimeter) : [10, 40, 200] } as LengthBoundSpec);

        annotation { "Name" : "Width" }
        isLength(definition.width, { (millimeter) : [5, 25, 200] } as LengthBoundSpec);

        annotation { "Name" : "Thickness" }
        isLength(definition.thickness, { (millimeter) : [1, 4, 25] } as LengthBoundSpec);
    }
    {
        const legLength = definition.legLength;
        const thickness = definition.thickness;

        if (thickness >= legLength)
        {
            throw regenError("Thickness must be smaller than the leg length", ["thickness"]);
        }

        // The L profile, drawn from the inside corner outwards.
        const sketchId = id + "profile";
        var profile = newSketchOnPlane(context, sketchId, { "sketchPlane" : definition.plane });

        const zero = 0 * millimeter;
        skPolyline(profile, "outline", {
                    "points" : [
                        vector(zero, zero),
                        vector(legLength, zero),
                        vector(legLength, thickness),
                        vector(thickness, thickness),
                        vector(thickness, legLength),
                        vector(zero, legLength),
                        vector(zero, zero)
                    ]
                });

        skSolve(profile);

        const region = qSketchRegion(sketchId);
        opExtrude(context, id + "extrude", {
                    "entities" : region,
                    "direction" : evOwnerSketchPlane(context, { "entity" : region }).normal,
                    "endBound" : BoundingType.BLIND,
                    "endDepth" : definition.width
                });

        // Tidy up: the sketch has served its purpose.
        opDeleteBodies(context, id + "deleteSketch", {
                    "entities" : qCreatedBy(sketchId, EntityType.BODY)
                });
    });
