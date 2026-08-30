# ================================================
# state_manager.py
# Memory Engine for StepByStepCAD_VoiceAuto
# ================================================

import json
import os
import adsk.core
import adsk.fusion

# ================================================
# GLOBAL SETUP
# ================================================

app = adsk.core.Application.get()
ui  = app.userInterface

ADDIN_DIR  = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(ADDIN_DIR, "state.json")


# ================================================
# FP-01: FACE FINGERPRINTING
#
# Problem: body.faces ordering is an internal Fusion
# B-Rep artifact, not guaranteed stable across feature
# history changes (fillet, chamfer, hole, boolean,
# mirror, pattern can all add/split/merge faces and
# renumber the collection). Storing a raw integer index
# in state and blindly re-using body.faces.item(index)
# later is a silent-wrong-geometry risk: the index stays
# "valid" (in range) but may now point at a completely
# different face than the one the designer selected.
#
# Fix: whenever a face index is recorded (show_faces,
# select_face), also snapshot a geometric "fingerprint"
# of that face — centroid + surface normal at the
# centroid + area. Before any command trusts a face
# index later (create_sketch face_index, extrude_face,
# hole), re-fingerprint the live face at that index and
# compare. A mismatch means the topology changed and the
# index no longer means what it did — surfaced as an
# explicit spoken error instead of silently sketching
# on the wrong face.
#
# verify_face_fingerprint() returns:
#   True  -> fingerprint matches, safe to use
#   False -> fingerprint recorded but does NOT match
#            (face identity changed — block and warn)
#   None  -> no fingerprint on record for this index
#            (never verified — proceed, but caller may
#            want to speak a caution)
# ================================================

FACE_FINGERPRINT_TOLERANCE = 1e-4   # cm — generous vs float noise, tight vs real topology change

def _face_fingerprint(face):
    try:
        c = face.centroid
        point = adsk.core.Point3D.create(c.x, c.y, c.z)
        _, normal = face.evaluator.getNormalAtPoint(point)
        return {
            "cx": c.x, "cy": c.y, "cz": c.z,
            "nx": normal.x, "ny": normal.y, "nz": normal.z,
            "area": face.area,
        }
    except Exception:
        return None


def record_face_fingerprints(body):
    """Snapshot centroid/normal/area for EVERY face on the body, keyed by
    index. Called by show_faces() so any face index spoken afterward —
    even without a click — can be verified later."""
    try:
        fps = {}
        for i in range(body.faces.count):
            fp = _face_fingerprint(body.faces.item(i))
            if fp:
                fps[str(i)] = fp
        state = read_state()
        state["face_fingerprints"] = fps
        _write_raw(state)
    except Exception:
        pass


def record_single_face_fingerprint(index, face):
    """Record/refresh the fingerprint for one face index. Called by
    select_face() after a click-confirmed selection."""
    try:
        fp = _face_fingerprint(face)
        if not fp:
            return
        state = read_state()
        fps = state.get("face_fingerprints") or {}
        fps[str(index)] = fp
        state["face_fingerprints"] = fps
        _write_raw(state)
    except Exception:
        pass


def verify_face_fingerprint(index, face):
    """Compare the live face at `index` against its recorded fingerprint.
    See module docstring above for return-value semantics."""
    try:
        state   = read_state()
        fps     = state.get("face_fingerprints") or {}
        stored  = fps.get(str(index))
        if not stored:
            return None
        current = _face_fingerprint(face)
        if not current:
            return None
        for key in ("cx", "cy", "cz", "nx", "ny", "nz"):
            if abs(stored[key] - current[key]) > FACE_FINGERPRINT_TOLERANCE:
                return False
        if stored.get("area") and current.get("area"):
            area_tol = max(FACE_FINGERPRINT_TOLERANCE, 0.01 * stored["area"])
            if abs(stored["area"] - current["area"]) > area_tol:
                return False
        return True
    except Exception:
        return None


# ================================================
# DEFAULT STATE — every key the system ever uses
# ================================================

DEFAULT_STATE = {
    "last_command_id"   : -1,
    "last_sketch_name"  : None,
    "last_profile_index": None,
    "last_body_name"    : None,
    "last_face_index"   : None,
    "last_axis_name"    : None,
    "last_plane_name"   : None,
    "last_feature_type" : None,
    "last_feature_name" : None,
    "last_sketch_data"  : None,
    "last_feature_data" : None,
    "face_fingerprints" : {},
}


# ================================================
# INITIALIZE STATE FILE
# ================================================

def initialize_state():
    if not os.path.exists(STATE_FILE):
        _write_raw(DEFAULT_STATE.copy())


# ================================================
# READ STATE
# Back-fills any keys missing from older state
# files so the system never crashes on a KeyError.
# ================================================

def read_state():
    initialize_state()
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        # Back-fill missing keys from newer DEFAULT_STATE
        changed = False
        for k, v in DEFAULT_STATE.items():
            if k not in state:
                state[k] = v
                changed = True
        if changed:
            _write_raw(state)
        return state
    except Exception:
        return DEFAULT_STATE.copy()


# ================================================
# WRITE STATE  (internal — always silent)
# ================================================

def _write_raw(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
    except Exception:
        pass   # never show a popup from a state write failure


def write_state(new_state):
    _write_raw(new_state)


# ================================================
# UPDATE SINGLE FIELD
# ================================================

def update_state(key, value):
    state = read_state()
    state[key] = value
    _write_raw(state)


# ================================================
# UPDATE MULTIPLE FIELDS AT ONCE  (one file write)
#
# Both names are provided so any version of
# solid_engine.py works regardless of which name
# it calls — update_state_bulk OR update_state_many.
# ================================================

def update_state_bulk(updates):
    state = read_state()
    state.update(updates)
    _write_raw(state)

# Alias — keeps compatibility with both naming conventions
update_state_many = update_state_bulk


# ================================================
# CLEAR STATE
# ================================================

def clear_state():
    _write_raw(DEFAULT_STATE.copy())


# ================================================
# RESET AFTER UNDO — wipes all volatile refs so
# stale names never point to deleted features.
# SM-01 FIX: after clearing refs, scan Fusion's
# actual sketch list and restore the most recently
# created sketch so sketch-dependent commands
# (extrude, circle, etc.) don't fail immediately.
# ================================================

def reset_after_undo():
    state = read_state()
    state["last_face_index"]   = None
    state["last_body_name"]    = None
    state["last_profile_index"]= None
    state["last_feature_name"] = None
    state["last_feature_type"] = None
    state["last_feature_data"] = None
    state["face_fingerprints"] = {}   # FP-01: topology may have changed — invalidate all
    try:
        root = adsk.fusion.Design.cast(app.activeProduct).rootComponent
        if root.sketches.count > 0:
            state["last_sketch_name"] = root.sketches.item(root.sketches.count - 1).name
        else:
            state["last_sketch_name"] = None
    except Exception:
        state["last_sketch_name"] = None
    _write_raw(state)


# ================================================
# GET LAST BODY OBJECT
# ================================================

def get_last_body():
    body_name = read_state().get("last_body_name")
    if not body_name:
        return None
    try:
        root = adsk.fusion.Design.cast(app.activeProduct).rootComponent
        for body in root.bRepBodies:
            if body.name == body_name:
                return body
    except Exception:
        pass
    return None


# ================================================
# GET LAST SKETCH OBJECT
# ================================================

def get_last_sketch():
    sketch_name = read_state().get("last_sketch_name")
    if not sketch_name:
        return None
    try:
        root = adsk.fusion.Design.cast(app.activeProduct).rootComponent
        for sketch in root.sketches:
            if sketch.name == sketch_name:
                return sketch
    except Exception:
        pass
    return None


# ================================================
# GET LAST PROFILE
# ================================================

def get_last_profile():
    sketch = get_last_sketch()
    if not sketch:
        return None
    profile_index = read_state().get("last_profile_index")
    if profile_index is not None and profile_index < sketch.profiles.count:
        return sketch.profiles.item(profile_index)
    return None


# ================================================
# GET LAST FACE
# ================================================

def get_last_face():
    body = get_last_body()
    if not body:
        return None
    face_index = read_state().get("last_face_index")
    if face_index is not None and face_index < body.faces.count:
        return body.faces.item(face_index)
    return None


# ================================================
# RESOLVE BODY — shared by every engine
#
# BODY-01 FIX (shared implementation): previously each engine file
# (modify_engine.py, solid_engine.py) had its own copy of this logic,
# and sketch_engine.py's create_sketch() had NO equivalent at all — it
# called get_last_body() directly with no fallback, meaning even a
# design with exactly one pre-existing body (e.g. built manually before
# voice control was ever used) would incorrectly error "No body in
# state" on the very first voice command. This single shared function
# fixes both problems: one implementation, used consistently everywhere.
#
# Returns (body, error_message). If body is None, error_message is
# always a non-empty string the caller should speak via its own
# _error() (this function has no access to any engine's speak/error
# injection, so it can't speak the message itself).
# ================================================

def resolve_body_or_none(root):
    body = get_last_body()
    if body:
        return body, None
    bodies = root.bRepBodies
    if bodies.count == 0:
        return None, "No solid body found in the design."
    if bodies.count > 1:
        return None, (
            "Multiple bodies exist and none is currently selected. "
            "Say show bodies, then select body, followed by a number — "
            "or click a face on the correct body and say select face."
        )
    return bodies.item(0), None