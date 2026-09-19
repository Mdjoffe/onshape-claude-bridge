FeatureScript 2600;
import(path : "onshape/std/geometry.fs", version : "2600.0");

// To consume the shared library, import the version you cut from lib/:
// import(path : "<library document id>/<version id>/<element id>", version : "<version id>");

annotation { "Feature Type Name" : "Example feature" }
export const exampleFeature = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Diameter" }
        isLength(definition.diameter, LENGTH_BOUNDS);
    }
    {
        // Feature body goes here.
    });
