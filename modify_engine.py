# ================================================
# modify_engine.py
# Modify Feature Engine — StepByStepCAD_VoiceAuto
# ================================================

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
# _speak(msg) — spoken aloud + blue log  (confirmations, index feedback, guidance)
# _error(msg) — spoken aloud + red log   (user-facing errors the designer can act on)
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

MODIFY_COMMANDS = [
    "fillet",
    "chamfer",
    "select_edge",
    "select_face",
    "select_profile",
    "show_faces",
    "show_profiles",
    "show_bodies",
    "select_body",
    "body_visibility",
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


# ================================================
# MAIN EXECUTION
# ================================================

def execute(command):
    try:
        cmd = command["command"]
        dispatch = {
            "fillet"          : fillet,
            "chamfer"         : chamfer,
            "select_edge"     : select_edge,
            "select_face"     : select_face,
            "select_profile"  : select_profile,
            "show_faces"      : show_faces,
            "show_profiles"   : show_profiles,
            "show_bodies"     : show_bodies,
            "select_body"     : select_body,
            "body_visibility" : body_visibility,
        }
        fn = dispatch.get(cmd)
        if fn:
            fn(command)
        else:
            _error("Modify engine received an unknown command: {}.".format(cmd))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# SELECT EDGE — click edge first, then say "select edge"
# ================================================

def select_edge(command):
    try:
        selections = ui.activeSelections
        if selections.count == 0:
            _error(
                "No edge selected. "
                "Click an edge in the viewport first, then say select edge."
            )
            return
        selected = selections.item(0).entity
        if not isinstance(selected, adsk.fusion.BRepEdge):
            _error("The selected item is not an edge. Click directly on an edge and try again.")
            return
        body  = selected.body
        edges = body.edges
        for i in range(edges.count):
            if edges.item(i) == selected:
                # MULTI-01: switch the active body context to whichever
                # body this edge belongs to, so a subsequent "fillet" or
                # "chamfer" (which resolve the body via state) target the
                # same body the edge was clicked on, not whatever body was
                # last active before this click.
                state_manager.update_state("last_body_name", body.name)
                _speak(
                    "Edge {} selected on body {}. "
                    "You can now say fillet radius 3 edge {}, "
                    "or chamfer distance 2 edge {}.".format(i, body.name, i, i)
                )
                return
        _error("Edge not found. Try clicking it again.")
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# SELECT FACE — click face first, then say "select face"
# ================================================

def select_face(command):
    try:
        selections = ui.activeSelections
        if selections.count == 0:
            _error(
                "No face selected. "
                "Click a face in the viewport first, then say select face."
            )
            return
        face = selections.item(0).entity
        if not isinstance(face, adsk.fusion.BRepFace):
            _error("The selected item is not a face. Click directly on a face and try again.")
            return

        # MULTI-01 FIX: derive the owning body directly from the clicked
        # face, instead of resolving "the current body" from state first.
        # Previously this called _resolve_body() up front, which returns
        # whatever body was LAST active in state — so clicking a face on
        # an older body (e.g. one buried under bodies built on top of it
        # afterward) would incorrectly report "not a face on the current
        # body," since the click was being checked against the wrong
        # body's face list entirely. Matching select_edge's existing
        # (correct) pattern of body = selected.body.
        body  = face.body
        faces = body.faces
        for i in range(faces.count):
            if faces.item(i) == face:
                # Switch the active body context to whichever body was
                # just clicked, and drop any stale face state left over
                # from a DIFFERENT body — index N on the old body has no
                # relationship to index N on this one.
                state_manager.update_state_bulk({
                    "last_body_name" : body.name,
                    "last_face_index": i,
                })
                state_manager.record_single_face_fingerprint(i, face)
                _speak(
                    "Face {} selected on body {} and saved to state. "
                    "You can now say create sketch on face index {}, "
                    "or extrude face {} distance 10.".format(i, body.name, i, i)
                )
                return
        _error("Face not found on its own body. Try clicking it again.")
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# SHOW BODIES / SELECT BODY — MULTI-01
#
# For a design with several bodies (e.g. stacked features each
# built as a separate "new body"), state only ever remembers ONE
# "active" body at a time (last_body_name). These two commands
# give explicit, fully voice-driven control over WHICH body that
# is — the equivalent of show_faces/select_face, one level up.
#
# This is what makes "go back and sketch on an older body's face,
# even after building more bodies on top of it" possible without
# needing to click: say "show bodies" to hear the index/name of
# every body, then "select body N" to make that one active, then
# proceed with show_faces / create sketch on face index as normal.
# ================================================

def show_bodies(command):
    try:
        root   = get_root()
        bodies = root.bRepBodies
        count  = bodies.count
        if count == 0:
            _error("No solid bodies found in the design.")
            return

        parts = ["The design has {} {}. ".format(
            count, "body" if count == 1 else "bodies")]
        for i in range(count):
            b = bodies.item(i)
            vis = "visible" if b.isVisible else "hidden"
            parts.append("Body {}: {}, {} faces, {}. ".format(
                i, b.name, b.faces.count, vis))
        parts.append(
            "Say select body, followed by a number, to make that body active."
        )
        _speak("".join(parts))

    except Exception:
        ui.messageBox(traceback.format_exc())


def select_body(command):
    try:
        root   = get_root()
        bodies = root.bRepBodies
        if bodies.count == 0:
            _error("No solid bodies found in the design.")
            return

        idx = command.get("body_index")
        if idx is None or idx < 0 or idx >= bodies.count:
            _error(
                "Body index {} is out of range. The design has {} "
                "{}. Say show bodies to hear the current list.".format(
                    idx, bodies.count, "body" if bodies.count == 1 else "bodies")
            )
            return

        body = bodies.item(idx)
        # Switching bodies invalidates any face index/fingerprint from
        # whatever body was previously active — index N on the old body
        # has no relationship to index N on this one, so drop them rather
        # than risk a misleading "doesn't match" or, worse, a coincidental
        # false match on the new body.
        state_manager.update_state_bulk({
            "last_body_name"   : body.name,
            "last_face_index"  : None,
            "face_fingerprints": {},
        })
        _speak(
            "Body {} selected: {}. Say show faces to list its faces.".format(
                idx, body.name)
        )

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# SELECT PROFILE — click profile first, then say "select profile"
# ================================================

def select_profile(command):
    try:
        sketch = state_manager.get_last_sketch()
        if not sketch:
            _error("No active sketch in state. Create a sketch first.")
            return
        selections = ui.activeSelections
        if selections.count == 0:
            _error(
                "No profile selected. "
                "Click inside a closed profile region in the sketch, "
                "then say select profile."
            )
            return
        selected = selections.item(0).entity
        profiles = sketch.profiles
        for i in range(profiles.count):
            if profiles.item(i) == selected:
                state_manager.update_state("last_profile_index", i)
                _speak(
                    "Profile {} selected and saved. "
                    "You can now say extrude join profile {}, "
                    "or extrude cut profile {}.".format(i, i, i)
                )
                return
        _error("The selected item is not a valid profile in the current sketch.")
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# SHOW FACES — lists all faces with orientation
# ================================================

def show_faces(command):
    try:
        body = _resolve_body()
        if not body:
            return

        count = body.faces.count
        parts = ["The body has {} {}. ".format(
            count, "face" if count == 1 else "faces")]

        for i in range(count):
            face = body.faces.item(i)
            geo  = face.geometry
            if isinstance(geo, adsk.core.Plane):
                n = geo.normal
                # Convert normal to plain English direction
                if n.z > 0.9:
                    direction = "top face, normal pointing up"
                elif n.z < -0.9:
                    direction = "bottom face, normal pointing down"
                elif n.y < -0.9:
                    direction = "front face"
                elif n.y > 0.9:
                    direction = "back face"
                elif n.x > 0.9:
                    direction = "right face"
                elif n.x < -0.9:
                    direction = "left face"
                else:
                    direction = "planar face, normal {:.1f}, {:.1f}, {:.1f}".format(
                        n.x, n.y, n.z)
                parts.append("Face {}: {}. ".format(i, direction))
            else:
                # MULTI-02: report distinguishing dimensions for curved
                # faces instead of a generic "curved surface" for all of
                # them. On a stepped/radial part with several different
                # cylindrical faces, "curved surface" alone gives no way
                # to tell them apart by ear — radius does.
                try:
                    if isinstance(geo, adsk.core.Cylinder):
                        parts.append(
                            "Face {}: cylindrical surface, radius {} "
                            "millimetres. ".format(i, int(round(geo.radius / MM)))
                        )
                    elif isinstance(geo, adsk.core.Sphere):
                        parts.append(
                            "Face {}: spherical surface, radius {} "
                            "millimetres. ".format(i, int(round(geo.radius / MM)))
                        )
                    elif isinstance(geo, adsk.core.Cone):
                        parts.append("Face {}: conical surface. ".format(i))
                    elif isinstance(geo, adsk.core.Torus):
                        parts.append("Face {}: toroidal surface. ".format(i))
                    else:
                        parts.append("Face {}: curved surface. ".format(i))
                except Exception:
                    parts.append("Face {}: curved surface. ".format(i))

        # FP-01: snapshot fingerprints for every face right now, keyed by
        # the same index just narrated. This is what lets a purely spoken
        # "create sketch on face index 3" (no click) be verified later —
        # without this, only click-confirmed indices from select_face
        # would ever be checkable.
        state_manager.record_face_fingerprints(body)

        parts.append(
            "Click a face in the viewport, then say select face to save its index. "
            "Or say create sketch on face index, followed by a number."
        )
        _speak("".join(parts))

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# SHOW PROFILES — lists all profiles in sketch
# ================================================

def show_profiles(command):
    try:
        sketch = state_manager.get_last_sketch()
        if not sketch:
            _error("No active sketch in state.")
            return
        count = sketch.profiles.count
        if count == 0:
            _error("No closed profiles found in the current sketch.")
            return
        if count == 1:
            _speak(
                "The sketch has 1 profile. Profile zero is ready to extrude. "
                "Click it and say select profile to confirm the selection."
            )
        else:
            _speak(
                "The sketch has {} profiles, numbered 0 to {}. "
                "Click a profile region and say select profile to choose one.".format(
                    count, count - 1)
            )
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# FILLET
# ================================================

def fillet(command):
    try:
        root = get_root()
        body = _resolve_body()
        if not body:
            return
        edge_indices = command.get("edges", [])
        radius       = command.get("radius", 1)

        if not edge_indices:
            _error(
                "No edges specified. "
                "Click an edge and say select edge to get its index number, "
                "then say fillet radius 3 edge 0."
            )
            return

        max_idx = body.edges.count - 1
        for idx in edge_indices:
            if idx > max_idx:
                _error(
                    "Edge index {} is out of range. "
                    "The body has {} edges, so the maximum index is {}.".format(
                        idx, body.edges.count, max_idx)
                )
                return

        edge_col = adsk.core.ObjectCollection.create()
        for idx in edge_indices:
            edge_col.add(body.edges.item(idx))

        fillet_input = root.features.filletFeatures.createInput()
        fillet_input.addConstantRadiusEdgeSet(
            edge_col,
            adsk.core.ValueInput.createByReal(radius * MM),
            True,
        )
        root.features.filletFeatures.add(fillet_input)
        _speak(
            "Fillet applied. Radius {} millimetres on {} {}.".format(
                radius,
                len(edge_indices),
                "edge" if len(edge_indices) == 1 else "edges")
        )
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# CHAMFER
# ================================================

def chamfer(command):
    try:
        root = get_root()
        body = _resolve_body()
        if not body:
            return
        edge_indices = command.get("edges", [])
        distance     = command.get("distance", 1)

        if not edge_indices:
            _error(
                "No edges specified. "
                "Click an edge and say select edge to get its index number, "
                "then say chamfer distance 2 edge 0."
            )
            return

        max_idx = body.edges.count - 1
        for idx in edge_indices:
            if idx > max_idx:
                _error(
                    "Edge index {} is out of range. "
                    "The body has {} edges, so the maximum index is {}.".format(
                        idx, body.edges.count, max_idx)
                )
                return

        edge_col = adsk.core.ObjectCollection.create()
        for idx in edge_indices:
            edge_col.add(body.edges.item(idx))

        chamfer_features = root.features.chamferFeatures
        chamfer_input    = chamfer_features.createInput(edge_col, True)
        chamfer_input.setToEqualDistance(adsk.core.ValueInput.createByReal(distance * MM))
        chamfer_features.add(chamfer_input)
        _speak(
            "Chamfer applied. Distance {} millimetres on {} {}.".format(
                distance,
                len(edge_indices),
                "edge" if len(edge_indices) == 1 else "edges")
        )
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# BODY VISIBILITY
# ================================================

def body_visibility(command):
    try:
        body = _resolve_body()
        if not body:
            return
        action = command.get("action", "hide")
        if action == "hide":
            body.isVisible = False
            _speak("Body is now hidden.")
        elif action == "show":
            body.isVisible = True
            _speak("Body is now visible.")
        else:
            _error("Unknown visibility action: {}. Use hide or show.".format(action))
    except Exception:
        ui.messageBox(traceback.format_exc())
