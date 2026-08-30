# ================================================
# solid_engine.py
# Solid Feature Engine — StepByStepCAD_VoiceAuto
# ================================================

import math
import adsk.core
import adsk.fusion
import traceback
import sys
import os

_addin_dir = os.path.dirname(os.path.realpath(__file__))
if _addin_dir not in sys.path:
    sys.path.insert(0, _addin_dir)

import importlib
state_manager = importlib.import_module("state_manager")
sketch_engine  = importlib.import_module("sketch_engine")

# ================================================
# GLOBALS
# ================================================

app = adsk.core.Application.get()
ui  = app.userInterface

MM = 1.0 / 10.0   # mm -> cm


# ================================================
# TWO-WAY COMMUNICATION INJECTION
#
# _speak_fn and _error_fn are injected by
# Stepbystep_voice_to_CAD.py at startup via
# set_speak() and set_error().
#
# _speak(msg) — spoken aloud + blue log  (confirmations, guidance)
# _error(msg) — spoken aloud + red log   (user-facing errors)
#
# ui.messageBox() is ONLY used for tracebacks.
# ================================================

_speak_fn = None
_error_fn = None

def set_speak(fn):
    global _speak_fn
    _speak_fn = fn

def set_error(fn):
    global _error_fn
    _error_fn = fn

def _speak(msg):
    if _speak_fn:
        _speak_fn(msg)

def _error(msg):
    if _error_fn:
        _error_fn(msg)
    else:
        ui.messageBox(msg)


# ================================================
# COMMAND LIST
# ================================================

SOLID_COMMANDS = [
    "extrude",
    "extrude_face",
    "extrude_ring",
    "revolve",
    "hole",
    "thread_external",
    "thread_internal",
    "mirror",
    "mirror_feature",
    "rectangular_pattern",
    "circular_pattern",
    "repeat_feature",
    "show_features",
    "select_feature",
]


# ================================================
# HELPERS
# ================================================

def get_design():
    return adsk.fusion.Design.cast(app.activeProduct)

def get_root():
    return get_design().rootComponent

def _resolve_body():
    root = get_root()
    body, err = state_manager.resolve_body_or_none(root)
    if err:
        _error(err)
    return body

def _operation_enum(op_str, fallback_new_body=True):
    op_map = {
        "join"    : adsk.fusion.FeatureOperations.JoinFeatureOperation,
        "cut"     : adsk.fusion.FeatureOperations.CutFeatureOperation,
        "new_body": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    }
    if op_str in op_map:
        return op_map[op_str]
    if op_str is None:
        return (adsk.fusion.FeatureOperations.NewBodyFeatureOperation
                if fallback_new_body
                else adsk.fusion.FeatureOperations.JoinFeatureOperation)
    _error("Unknown operation: {}. Use join, cut, or new body.".format(op_str))
    return None

def _get_last_feature():
    state        = state_manager.read_state()
    feature_name = state.get("last_feature_name")
    if not feature_name:
        return None
    root = get_root()
    collections = [
        root.features.extrudeFeatures,
        root.features.holeFeatures,
        root.features.filletFeatures,
        root.features.chamferFeatures,
        root.features.revolveFeatures,
        root.features.mirrorFeatures,
        root.features.rectangularPatternFeatures,
        root.features.circularPatternFeatures,
        root.features.threadFeatures,
    ]
    for col in collections:
        try:
            for feat in col:
                if feat.name == feature_name:
                    return feat
        except Exception:
            continue
    return None


# ================================================
# SHOW FEATURES / SELECT FEATURE — MULTI-03
#
# Previously, mirror_feature/rectangular_pattern/circular_pattern could
# only ever target "whatever feature was last created" (_get_last_feature,
# via last_feature_name in state) — with no way to go back and pattern
# an EARLIER feature (e.g. a hole made several steps ago on a specific
# face) without redoing it. This exposes Fusion's own design.timeline,
# which lists every feature regardless of creation order OR whether it
# was made via voice or the mouse — directly enabling "pattern any
# feature at any point in the design," the same way show_bodies/
# select_body did for bodies and show_faces/select_face did for faces.
# ================================================

def show_features(command):
    try:
        timeline = get_design().timeline
        count = timeline.count
        if count == 0:
            _error("No features found in the timeline.")
            return
        parts = ["The timeline has {} {}. ".format(
            count, "item" if count == 1 else "items")]
        for i in range(count):
            entity = timeline.item(i).entity
            name = getattr(entity, "name", None) if entity else None
            if not name:
                parts.append("Item {}: not a selectable feature. ".format(i))
                continue
            try:
                type_name = entity.classType().split("::")[-1]
            except Exception:
                type_name = "feature"
            parts.append("Feature {}: {}, {}. ".format(i, name, type_name))
        parts.append(
            "Say select feature, followed by a number, to make that "
            "feature active for pattern or mirror commands."
        )
        _speak("".join(parts))
    except Exception:
        ui.messageBox(traceback.format_exc())


def select_feature(command):
    try:
        timeline = get_design().timeline
        idx = command.get("feature_index")
        if idx is None or idx < 0 or idx >= timeline.count:
            _error(
                "Feature index {} is out of range. Say show features to "
                "hear the current list.".format(idx)
            )
            return
        entity = timeline.item(idx).entity
        name = getattr(entity, "name", None) if entity else None
        if not name:
            _error(
                "Timeline item {} is not a selectable feature (it may be "
                "a sketch or a folder).".format(idx)
            )
            return
        state_manager.update_state_bulk({
            "last_feature_type": "selected",
            "last_feature_name": name,
        })
        _speak(
            "Feature {} selected: {}. You can now say rectangular "
            "pattern, circular pattern, or mirror feature.".format(idx, name)
        )
    except Exception:
        ui.messageBox(traceback.format_exc())


def _resolve_target_feature(command):
    """Resolve which feature a pattern/mirror command should act on.
    Prefers an explicit feature_index in the command itself (so
    "circular pattern 6 copies feature 2" works in one shot), falling
    back to _get_last_feature() — i.e. whatever select_feature or the
    last-created feature left active — for backward compatibility with
    the existing single-step workflow."""
    idx = command.get("feature_index")
    if idx is not None:
        try:
            timeline = get_design().timeline
            if idx < 0 or idx >= timeline.count:
                _error(
                    "Feature index {} is out of range. Say show features "
                    "to hear the current list.".format(idx)
                )
                return None
            entity = timeline.item(idx).entity
            if entity is None or not getattr(entity, "name", None):
                _error(
                    "Timeline item {} is not a selectable feature.".format(idx)
                )
                return None
            return entity
        except Exception:
            _error("Could not resolve feature index {}.".format(idx))
            return None

    feat = _get_last_feature()
    if not feat:
        _error(
            "No feature in state to use. Say select feature followed by "
            "a number, or specify a feature index directly in your command."
        )
    return feat


def _find_cylindrical_face(body):
    for face in body.faces:
        try:
            if face.geometry.objectType == adsk.core.Cylinder.classType():
                return face
        except Exception:
            continue
    return None


# ================================================
# MAIN EXECUTION
# ================================================

def execute(command):
    try:
        cmd = command["command"]
        dispatch = {
            "extrude"             : extrude,
            "extrude_face"        : extrude_face,
            "extrude_ring"        : extrude_ring,
            "revolve"             : revolve,
            "hole"                : hole,
            "thread_external"     : thread_external,
            "thread_internal"     : thread_internal,
            "mirror"              : mirror_body,
            "mirror_feature"      : mirror_feature,
            "rectangular_pattern" : rectangular_pattern,
            "circular_pattern"    : circular_pattern,
            "repeat_feature"      : repeat_feature,
            "show_features"       : show_features,
            "select_feature"      : select_feature,
        }
        fn = dispatch.get(cmd)
        if fn:
            fn(command)
        else:
            _error("Solid engine received an unknown command: {}.".format(cmd))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# EXTRUDE
# ================================================

def extrude(command):
    try:
        root   = get_root()
        sketch = state_manager.get_last_sketch()

        if sketch is None:
            _error("No active sketch. Please run create sketch first.")
            return
        if sketch.profiles.count == 0:
            _error("No closed profile in the sketch. Draw a closed shape first.")
            return

        profile_index = command.get("profile_index", sketch.profiles.count - 1)
        if profile_index >= sketch.profiles.count:
            _error(
                "Profile index {} is out of range. "
                "The sketch has {} profiles.".format(
                    profile_index, sketch.profiles.count)
            )
            return
        profile = sketch.profiles.item(profile_index)

        distance    = command.get("distance", 10)
        extent_type = command.get("extent", "one_side")
        op_str      = command.get("operation", None)

        if op_str is None:
            op_str = "cut" if distance < 0 else "new_body"
        operation = _operation_enum(op_str)
        if operation is None:
            return

        extrudes  = root.features.extrudeFeatures
        ext_input = extrudes.createInput(profile, operation)
        dist      = adsk.core.ValueInput.createByReal(abs(distance) * MM)

        # Cut always uses symmetric extent — attempts to use setDistanceExtent for
        # CutFeatureOperation cause RuntimeError 3 in Fusion 360's Python API.
        # SE-01 (one-sided cut) was investigated but is not stable in this workflow.
        # Join and new_body honour extent_type (one_side or symmetric) correctly.
        if operation == adsk.fusion.FeatureOperations.CutFeatureOperation:
            ext_input.setSymmetricExtent(dist, True)
        elif extent_type == "symmetric":
            ext_input.setSymmetricExtent(dist, True)
        else:
            ext_input.setDistanceExtent(False, dist)

        feat        = extrudes.add(ext_input)
        sketch_data = state_manager.read_state().get("last_sketch_data")

        updates = {
            "last_feature_type": "extrude",
            "last_feature_name": feat.name,
            "last_feature_data": {
                "type"       : "extrude",
                "sketch_data": sketch_data,
                "distance"   : distance,
                "operation"  : op_str,
                "extent"     : extent_type,
            },
        }
        if feat.bodies.count > 0:
            updates["last_body_name"] = feat.bodies.item(0).name
        else:
            root_bodies = get_root().bRepBodies
            if root_bodies.count > 0:
                updates["last_body_name"] = root_bodies.item(0).name

        state_manager.update_state_bulk(updates)
        _speak(
            "Extrude complete. {} millimetres, {} operation.".format(
                abs(int(distance)),
                op_str.replace("_", " "))
        )

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# EXTRUDE FACE
# ================================================

def extrude_face(command):
    try:
        root = get_root()
        body = _resolve_body()
        if not body:
            return

        face_index = command.get("face_index")
        if face_index is not None:
            if face_index >= body.faces.count:
                _error(
                    "Face index {} is out of range. "
                    "The body has {} faces.".format(face_index, body.faces.count)
                )
                return
            face = body.faces.item(face_index)

            # FP-01: same staleness risk as create_sketch's face_index path —
            # verify before trusting the index.
            fp_status = state_manager.verify_face_fingerprint(face_index, face)
            if fp_status is False:
                _error(
                    "Face index {} no longer matches the face you last saw "
                    "there. The model has changed since then. Say show faces "
                    "again to refresh, then pick a face index.".format(face_index)
                )
                return

            state_manager.update_state("last_face_index", face_index)
            state_manager.record_single_face_fingerprint(face_index, face)
        else:
            face = state_manager.get_last_face()
            if not face:
                _error(
                    "No face selected. "
                    "Add face index to your command, or run select face first."
                )
                return

        sketch = root.sketches.add(face)
        for edge in face.edges:
            sketch.project(edge)
        adsk.doEvents()

        if sketch.profiles.count == 0:
            _error("No profile generated from the face. The face may not be planar.")
            return

        profile   = sketch.profiles.item(0)
        distance  = command.get("distance", 5)
        op_str    = command.get("operation", "join")
        operation = _operation_enum(op_str, fallback_new_body=False)
        if operation is None:
            return

        extrudes  = root.features.extrudeFeatures
        ext_input = extrudes.createInput(profile, operation)
        dist      = adsk.core.ValueInput.createByReal(abs(distance) * MM)
        ext_input.setDistanceExtent(False, dist)
        feat = extrudes.add(ext_input)

        updates = {
            "last_feature_type": "extrude",
            "last_feature_name": feat.name,
        }
        if feat.bodies.count > 0:
            updates["last_body_name"] = feat.bodies.item(0).name
        else:
            root_bodies = get_root().bRepBodies
            if root_bodies.count > 0:
                updates["last_body_name"] = root_bodies.item(0).name
        state_manager.update_state_bulk(updates)
        _speak("Face extrude complete. {} millimetres.".format(abs(int(distance))))

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# EXTRUDE RING — RING-01
#
# For concentric circles in one sketch (e.g. 3 circles sharing a centre,
# increasing radius), Fusion's own sketch.profiles ALREADY computes the
# annular region between any two of them as a separate, distinct profile
# from the full inner disc — this is native Fusion behavior, not
# something this engine has to construct. A profile between circle A
# (outer) and circle B (inner) has exactly two loops: an OUTER loop
# bounded by circle A, and an INNER loop (a hole) bounded by circle B.
#
# The fully click-based way to get this today: click directly inside the
# ring area in the sketch (not the centre) and say "select profile",
# then "extrude join profile N" — Fusion's native profile-region
# detection already excludes the inner circle's area correctly, no
# special code needed.
#
# This command is the voice-only equivalent: given two circle indices
# from "show curves", find that exact ring profile automatically, no
# click required.
# ================================================

def _find_ring_profile(sketch, outer_circle, inner_circle):
    """Find the sketch profile whose outer boundary is outer_circle and
    whose single inner hole is inner_circle. Returns None if no such
    profile exists (e.g. the circles aren't concentric, or something
    else crosses between them, or the API doesn't cast a curve as
    expected — all treated as caller-facing 'not found')."""
    try:
        for profile in sketch.profiles:
            loops = profile.profileLoops
            if loops.count != 2:
                continue
            outer_match = False
            inner_match = False
            for i in range(loops.count):
                loop = loops.item(i)
                curves = loop.profileCurves
                if curves.count != 1:
                    continue
                try:
                    underlying = curves.item(0).sketchEntity
                except Exception:
                    continue
                if loop.isOuter and underlying == outer_circle:
                    outer_match = True
                elif (not loop.isOuter) and underlying == inner_circle:
                    inner_match = True
            if outer_match and inner_match:
                return profile
    except Exception:
        pass
    return None


def extrude_ring(command):
    try:
        root   = get_root()
        sketch = state_manager.get_last_sketch()
        if sketch is None:
            _error(
                "No active sketch. Draw your concentric circles and "
                "finish the sketch first."
            )
            return

        inner_idx = command.get("inner_curve_index")
        outer_idx = command.get("outer_curve_index")
        inner = sketch_engine._curve_at(sketch, inner_idx)
        outer = sketch_engine._curve_at(sketch, outer_idx)

        if inner is None or not isinstance(inner, adsk.fusion.SketchCircle):
            _error(
                "Inner curve index {} is not a circle. Say show curves "
                "to hear current indices.".format(inner_idx)
            )
            return
        if outer is None or not isinstance(outer, adsk.fusion.SketchCircle):
            _error(
                "Outer curve index {} is not a circle. Say show curves "
                "to hear current indices.".format(outer_idx)
            )
            return
        if outer.radius <= inner.radius:
            _error(
                "The outer circle must be larger than the inner circle "
                "to extrude a ring. Check your curve indices."
            )
            return

        profile = _find_ring_profile(sketch, outer, inner)
        if profile is None:
            _error(
                "Could not find a ring profile bounded by circle {} on "
                "the outside and circle {} on the inside. Make sure both "
                "circles share the same centre and nothing else crosses "
                "between them.".format(outer_idx, inner_idx)
            )
            return

        distance  = command.get("distance", 10)
        op_str    = command.get("operation", "new_body")
        operation = _operation_enum(op_str)
        if operation is None:
            return

        extrudes  = root.features.extrudeFeatures
        ext_input = extrudes.createInput(profile, operation)
        dist      = adsk.core.ValueInput.createByReal(abs(distance) * MM)
        if operation == adsk.fusion.FeatureOperations.CutFeatureOperation:
            ext_input.setSymmetricExtent(dist, True)
        else:
            ext_input.setDistanceExtent(False, dist)
        feat = extrudes.add(ext_input)

        updates = {
            "last_feature_type": "extrude",
            "last_feature_name": feat.name,
        }
        if feat.bodies.count > 0:
            updates["last_body_name"] = feat.bodies.item(0).name
        state_manager.update_state_bulk(updates)

        _speak(
            "Ring extruded between circle {} and circle {}. {} "
            "millimetres, {} operation.".format(
                inner_idx, outer_idx, abs(int(distance)), op_str.replace("_", " "))
        )

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# REVOLVE
# ================================================

def revolve(command):
    try:
        profile = state_manager.get_last_profile()
        sketch  = state_manager.get_last_sketch()

        if not profile:
            _error("No profile in state. Please run finish sketch first.")
            return
        if not sketch:
            _error("No active sketch in state.")
            return

        line_count = sketch.sketchCurves.sketchLines.count
        if line_count == 0:
            _error("Revolve needs at least one line in the sketch to use as the axis.")
            return

        axis_index = command.get("axis_line_index", 0)
        if axis_index >= line_count:
            _error(
                "Axis line index {} is out of range. "
                "The sketch has {} lines.".format(axis_index, line_count)
            )
            return

        axis  = sketch.sketchCurves.sketchLines.item(axis_index)
        angle = command.get("angle", 360)

        # SE-03 FIX: Read operation from command dict; default to new_body.
        # Enables 'revolve cut' and 'revolve join' via voice.
        op_str    = command.get("operation", "new_body")
        operation = _operation_enum(op_str)
        if operation is None:
            return

        revolves  = get_root().features.revolveFeatures
        rev_input = revolves.createInput(profile, axis, operation)
        rev_input.setAngleExtent(
            False,
            adsk.core.ValueInput.createByString("{} deg".format(angle)),
        )
        feat = revolves.add(rev_input)

        updates = {
            "last_feature_type": "revolve",
            "last_feature_name": feat.name,
            "last_face_index"  : 0,
        }
        if feat.bodies.count > 0:
            updates["last_body_name"] = feat.bodies.item(0).name
        state_manager.update_state_bulk(updates)
        _speak(
            "Revolve complete. {} degrees, {} operation.".format(
                int(angle), op_str.replace("_", " "))
        )

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# HOLE
# ================================================

def hole(command):
    try:
        root = get_root()
        body = _resolve_body()
        if not body:
            return

        face_index = command.get("face_index")
        if face_index is not None:
            if face_index >= body.faces.count:
                _error(
                    "Face index {} is out of range. "
                    "The body has {} faces.".format(face_index, body.faces.count)
                )
                return
            face = body.faces.item(face_index)

            # FP-01: verify before drilling a hole at a coordinate on a face
            # that might no longer be the one the designer meant.
            fp_status = state_manager.verify_face_fingerprint(face_index, face)
            if fp_status is False:
                _error(
                    "Face index {} no longer matches the face you last saw "
                    "there. The model has changed since then. Say show faces "
                    "again to refresh, then pick a face index.".format(face_index)
                )
                return

            used_face_index = face_index
            state_manager.record_single_face_fingerprint(face_index, face)
        else:
            face            = None
            used_face_index = None
            for idx in range(body.faces.count):
                f = body.faces.item(idx)
                if isinstance(f.geometry, adsk.core.Plane) and abs(f.geometry.normal.z) > 0.9:
                    face            = f
                    used_face_index = idx
                    break
            if not face:
                _error(
                    "No top or bottom planar face found. "
                    "Add face index to your command."
                )
                return

        x        = command["x"]        * MM
        y        = command["y"]        * MM
        diameter = command["diameter"] * MM
        depth    = command["depth"]    * MM

        sketch = root.sketches.add(face)
        point  = sketch.sketchPoints.add(adsk.core.Point3D.create(x, y, 0))

        holes      = root.features.holeFeatures
        hole_input = holes.createSimpleInput(adsk.core.ValueInput.createByReal(diameter))
        hole_input.setPositionBySketchPoint(point)
        hole_input.setDistanceExtent(adsk.core.ValueInput.createByReal(depth))
        feat = holes.add(hole_input)

        # SE-02 FIX: Clear last_sketch_name so the anonymous hole sketch does not
        # leak into state and corrupt subsequent sketch-dependent commands.
        state_manager.update_state_bulk({
            "last_feature_type": "hole",
            "last_feature_name": feat.name,
            "last_face_index"  : used_face_index,
            "last_sketch_name" : None,
            "last_feature_data": {
                "type"    : "hole",
                "x"       : command["x"],
                "y"       : command["y"],
                "diameter": command["diameter"],
                "depth"   : command["depth"],
            },
        })
        _speak(
            "Hole created. Diameter {} millimetres, depth {} millimetres, "
            "at position {}, {}.".format(
                int(command["diameter"]), int(command["depth"]),
                int(command["x"]), int(command["y"]))
        )

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# THREAD EXTERNAL
# ================================================

def thread_external(command):
    try:
        body = _resolve_body()
        if not body:
            return
        face = _find_cylindrical_face(body)
        if not face:
            _error("No cylindrical face found on the body for threading.")
            return

        designation     = command.get("designation", "M10x1.5")
        handedness      = command.get("handedness", "right")
        thread_features = get_root().features.threadFeatures
        thread_data     = thread_features.threadDataQuery
        default_type    = thread_data.defaultMetricThreadType
        recommend       = thread_data.recommendThreadData(face.geometry.radius * 2, default_type)

        thread_input           = thread_features.createInput(face, True)
        thread_input.isModeled = True
        thread_input.threadInfo = thread_data.createThreadInfo(
            handedness.lower() == "right", default_type, designation, recommend.threadClass)
        feat = thread_features.add(thread_input)

        state_manager.update_state_bulk({
            "last_feature_type": "thread",
            "last_feature_name": feat.name,
        })
        _speak(
            "External thread applied. Designation {}, {} hand.".format(
                designation, handedness)
        )
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# THREAD INTERNAL
# ================================================

def thread_internal(command):
    try:
        body = _resolve_body()
        if not body:
            return
        face = _find_cylindrical_face(body)
        if not face:
            _error("No cylindrical face found on the body for threading.")
            return

        designation     = command.get("designation", "M10x1.5")
        handedness      = command.get("handedness", "right")
        thread_features = get_root().features.threadFeatures
        thread_data     = thread_features.threadDataQuery
        default_type    = thread_data.defaultMetricThreadType
        recommend       = thread_data.recommendThreadData(face.geometry.radius * 2, default_type)

        thread_input           = thread_features.createInput(face, False)
        thread_input.isModeled = True
        thread_input.threadInfo = thread_data.createThreadInfo(
            handedness.lower() == "right", default_type, designation, recommend.threadClass)
        feat = thread_features.add(thread_input)

        state_manager.update_state_bulk({
            "last_feature_type": "thread",
            "last_feature_name": feat.name,
        })
        _speak(
            "Internal thread applied. Designation {}, {} hand.".format(
                designation, handedness)
        )
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# MIRROR BODY
# ================================================

def mirror_body(command):
    try:
        root = get_root()
        body = _resolve_body()
        if not body:
            return
        plane_name = command.get("plane", "xy")
        plane_map  = {
            "xy": root.xYConstructionPlane,
            "yz": root.yZConstructionPlane,
            "xz": root.xZConstructionPlane,
        }
        plane = plane_map.get(plane_name)
        if plane is None:
            _error("Unknown mirror plane: {}. Use X Y, Y Z, or X Z.".format(plane_name))
            return
        objs = adsk.core.ObjectCollection.create()
        objs.add(body)
        mirror_features = root.features.mirrorFeatures
        mirror_input    = mirror_features.createInput(objs, plane)
        mirror_features.add(mirror_input)
        _speak("Body mirrored about the {} plane.".format(plane_name.upper()))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# MIRROR FEATURE
# ================================================

def mirror_feature(command):
    try:
        root = get_root()
        last = _resolve_target_feature(command)
        if not last:
            return
        plane_name = command.get("plane", "xy")
        plane_map  = {
            "xy": root.xYConstructionPlane,
            "yz": root.yZConstructionPlane,
            "xz": root.xZConstructionPlane,
        }
        plane = plane_map.get(plane_name)
        if plane is None:
            _error("Unknown mirror plane: {}. Use X Y, Y Z, or X Z.".format(plane_name))
            return
        feats = adsk.core.ObjectCollection.create()
        feats.add(last)
        mirror_features = root.features.mirrorFeatures
        mirror_input    = mirror_features.createInput(feats, plane)
        mirror_input.patternComputeOption = (
            adsk.fusion.PatternComputeOptions.IdenticalPatternCompute
        )
        mirror_features.add(mirror_input)
        _speak("Feature mirrored about the {} plane.".format(plane_name.upper()))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# RECTANGULAR PATTERN
# ================================================

def rectangular_pattern(command):
    try:
        root = get_root()
        last = _resolve_target_feature(command)
        if not last:
            return

        count_x   = int(command.get("count_x", command.get("count", 2)))
        count_y   = int(command.get("count_y", 1))
        spacing_x = command.get("spacing_x", command.get("spacing", 10)) * MM
        spacing_y = command.get("spacing_y", 10) * MM

        feat_col  = adsk.core.ObjectCollection.create()
        feat_col.add(last)
        patterns  = root.features.rectangularPatternFeatures
        pat_input = patterns.createInput(
            feat_col,
            root.xConstructionAxis,
            adsk.core.ValueInput.createByString(str(count_x)),
            adsk.core.ValueInput.createByReal(spacing_x),
            adsk.fusion.PatternDistanceType.SpacingPatternDistanceType,
        )
        pat_input.setDirectionTwo(
            root.yConstructionAxis,
            adsk.core.ValueInput.createByString(str(count_y)),
            adsk.core.ValueInput.createByReal(spacing_y),
        )
        pat_input.patternComputeOption = (
            adsk.fusion.PatternComputeOptions.IdenticalPatternCompute
        )
        feat = patterns.add(pat_input)
        state_manager.update_state_bulk({
            "last_feature_type": "rectangular_pattern",
            "last_feature_name": feat.name,
        })
        _speak(
            "Rectangular pattern created. {} columns by {} rows.".format(count_x, count_y)
        )
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# CIRCULAR PATTERN
# ================================================

def circular_pattern(command):
    try:
        root = get_root()
        last = _resolve_target_feature(command)
        if not last:
            return

        count       = int(command.get("count", 4))
        total_angle = command.get("angle", 360)
        axis_name   = command.get("axis", "z")
        axis_map = {
            "x": root.xConstructionAxis,
            "y": root.yConstructionAxis,
            "z": root.zConstructionAxis,
        }
        axis = axis_map.get(axis_name)
        if axis is None:
            _error("Unknown axis: {}. Use X, Y, or Z.".format(axis_name))
            return

        feat_col  = adsk.core.ObjectCollection.create()
        feat_col.add(last)
        patterns  = root.features.circularPatternFeatures
        pat_input = patterns.createInput(feat_col, axis)
        pat_input.quantity    = adsk.core.ValueInput.createByString(str(count))
        pat_input.totalAngle  = adsk.core.ValueInput.createByString("{} deg".format(total_angle))
        pat_input.isSymmetric = (total_angle == 360)
        pat_input.patternComputeOption = (
            adsk.fusion.PatternComputeOptions.IdenticalPatternCompute
        )
        feat = patterns.add(pat_input)
        state_manager.update_state_bulk({
            "last_feature_type": "circular_pattern",
            "last_feature_name": feat.name,
        })
        _speak(
            "Circular pattern created. {} instances over {} degrees about the {} axis.".format(
                count, int(total_angle), axis_name.upper())
        )
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# REPEAT FEATURE
# ================================================

def repeat_feature(command):
    try:
        root  = get_root()
        state = state_manager.read_state()
        data  = state.get("last_feature_data")

        if not data:
            _error("No feature data in state. Run an extrude first.")
            return

        sketch_data = data.get("sketch_data")
        if not sketch_data:
            _error("No sketch data stored. Draw a shape, finish sketch, then extrude.")
            return

        count     = int(command.get("count", 2))
        spacing   = command.get("spacing", 10)
        direction = command.get("direction", "x")

        dx = spacing if direction in ("x", "both") else 0
        dy = spacing if direction in ("y", "both") else 0

        base_sketch = state_manager.get_last_sketch()
        if not base_sketch:
            _error("No base sketch found in state.")
            return

        op_map = {
            "join"    : adsk.fusion.FeatureOperations.JoinFeatureOperation,
            "cut"     : adsk.fusion.FeatureOperations.CutFeatureOperation,
            "new_body": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
            None      : adsk.fusion.FeatureOperations.JoinFeatureOperation,
        }
        raw_op    = data.get("operation")
        operation = op_map.get(raw_op, adsk.fusion.FeatureOperations.JoinFeatureOperation)
        dist_val  = data.get("distance", 10)

        for i in range(1, count):
            offset_x   = dx * i
            offset_y   = dy * i
            new_sketch = root.sketches.add(base_sketch.referencePlane)
            curves     = new_sketch.sketchCurves
            stype      = sketch_data["type"]

            if stype == "circle":
                cx = (sketch_data["cx"] + offset_x) * MM
                cy = (sketch_data["cy"] + offset_y) * MM
                r  = (sketch_data["diameter"] / 2) * MM
                curves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(cx, cy, 0), r)

            elif stype in ("rectangle", "rectangle_2point"):
                x1 = (sketch_data["x1"] + offset_x) * MM
                y1 = (sketch_data["y1"] + offset_y) * MM
                x2 = (sketch_data["x2"] + offset_x) * MM
                y2 = (sketch_data["y2"] + offset_y) * MM
                curves.sketchLines.addTwoPointRectangle(
                    adsk.core.Point3D.create(x1, y1, 0),
                    adsk.core.Point3D.create(x2, y2, 0),
                )

            elif stype == "rectangle_center":
                cx     = sketch_data["cx"] + offset_x
                cy     = sketch_data["cy"] + offset_y
                length = sketch_data["length"]
                width  = sketch_data["width"]
                curves.sketchLines.addCenterPointRectangle(
                    adsk.core.Point3D.create(cx * MM, cy * MM, 0),
                    adsk.core.Point3D.create((cx + length / 2) * MM, (cy + width / 2) * MM, 0),
                )

            elif stype == "polygon":
                cx     = sketch_data["cx"] + offset_x
                cy     = sketch_data["cy"] + offset_y
                sides  = sketch_data["sides"]
                radius = sketch_data["radius"]
                pts = [
                    adsk.core.Point3D.create(
                        (cx + radius * math.cos(2 * math.pi * k / sides)) * MM,
                        (cy + radius * math.sin(2 * math.pi * k / sides)) * MM,
                        0,
                    )
                    for k in range(sides)
                ]
                for k in range(sides):
                    curves.sketchLines.addByTwoPoints(pts[k], pts[(k + 1) % sides])

            else:
                _error("Repeat feature: unsupported sketch type {}.".format(stype))
                return

            adsk.doEvents()
            profiles = new_sketch.profiles
            if profiles.count == 0:
                _error(
                    "Repeat copy {} generated no closed profile. Aborting.".format(i)
                )
                return

            profile   = profiles.item(profiles.count - 1)
            extrudes  = root.features.extrudeFeatures
            ext_input = extrudes.createInput(profile, operation)
            dist      = adsk.core.ValueInput.createByReal(abs(dist_val) * MM)

            # Cut always symmetric (same constraint as extrude()).
            ext_type = data.get("extent", "one_side")
            if operation == adsk.fusion.FeatureOperations.CutFeatureOperation:
                ext_input.setSymmetricExtent(dist, True)
            elif ext_type == "symmetric":
                ext_input.setSymmetricExtent(dist, True)
            else:
                ext_input.setDistanceExtent(False, dist)

            new_feat = extrudes.add(ext_input)
            if new_feat.bodies.count > 0:
                state_manager.update_state("last_body_name", new_feat.bodies.item(0).name)

        _speak(
            "Repeat feature complete. {} copies created along the {} axis, "
            "{} millimetres apart.".format(count - 1, direction.upper(), int(spacing))
        )

    except Exception:
        ui.messageBox(traceback.format_exc())
