# ================================================
# sketch_engine.py
# Sketch Feature Engine — StepByStepCAD_VoiceAuto
# ================================================

import math
import adsk.core
import adsk.fusion
import traceback
import sys
import os
import importlib

_addin_dir = os.path.dirname(os.path.realpath(__file__))
if _addin_dir not in sys.path:
    sys.path.insert(0, _addin_dir)

state_manager = importlib.import_module("state_manager")

# ================================================
# GLOBALS
# ================================================

app = adsk.core.Application.get()
ui  = app.userInterface

MM = 1.0 / 10.0   # mm -> cm (Fusion internal unit)


# ================================================
# TWO-WAY COMMUNICATION INJECTION
#
# _speak_fn and _error_fn are injected by
# Stepbystep_voice_to_CAD.py at startup via
# set_speak() and set_error().
#
# _speak(msg) — spoken aloud + blue log  (confirmations, guidance, index feedback)
# _error(msg) — spoken aloud + red log   (user-facing errors the designer can act on)
#
# ui.messageBox() is ONLY used for tracebacks —
# runtime errors that need full text on screen.
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
        # Fallback before injection: use messageBox so nothing is silently lost
        ui.messageBox(msg)


# ================================================
# COMMAND LIST
# ================================================

SKETCH_COMMANDS = [
    "create_sketch",
    "create_offset_plane",
    "line",
    "rectangle_2point",
    "rectangle_center",
    "circle",
    "polygon",
    "trim",
    "trim_arc",
    "restore_circle",
    "show_curves",
    "add_constraint",
    "read_point",
    "sketch_mirror",
    "finish_sketch",
]


# ================================================
# HELPERS
# ================================================

def get_design():
    return adsk.fusion.Design.cast(app.activeProduct)

def get_root():
    return get_design().rootComponent

def _get_sketch_or_error():
    sketch = _get_active_or_tracked_sketch()
    if not sketch:
        _error("No active sketch. Please run create sketch first.")
    return sketch

def _get_active_or_tracked_sketch():
    """SKETCH-01 FIX: prefer whatever sketch Fusion currently has open for
    editing (app.activeEditObject) over our own tracked last_sketch_name.

    Symptom this fixes: drawing geometry lands in the wrong sketch (e.g.
    an old sketch on the XZ plane, centred at global origin) instead of
    whatever sketch is visibly open in Fusion — because the designer
    opened a DIFFERENT sketch manually (double-click / click in the
    browser tree) than whichever one our own state.json last pointed at.
    Every sketch-geometry command in this engine (circle, line, trim,
    constraints, etc.) should follow whatever is actually visually
    active, not a stale internal pointer.

    Opportunistically syncs state.json to match reality when they
    disagree, so downstream lookups (finish_sketch's profile index,
    etc.) stay consistent afterward.

    Deliberately scoped to sketch-EDITING commands only — NOT applied to
    state_manager.get_last_sketch() globally, because solid_engine.py's
    revolve()/repeat_feature() use that function to mean "the sketch
    behind the last extrude," not "whatever's currently open." Casually
    opening an unrelated old sketch to look at it shouldn't silently
    redirect a later extrude command.
    """
    try:
        edit_obj = app.activeEditObject
        if isinstance(edit_obj, adsk.fusion.Sketch):
            stored_name = state_manager.read_state().get("last_sketch_name")
            if stored_name != edit_obj.name:
                state_manager.update_state("last_sketch_name", edit_obj.name)
            return edit_obj
    except Exception:
        pass
    return state_manager.get_last_sketch()


# ================================================
# Y-AXIS AUTO-CORRECTION
# ================================================
#
# Fusion's sketch local Y axis direction depends on
# which plane the sketch is on:
#
#   XY plane   -> local Y = global +Y  -> no flip needed
#   XZ plane   -> local Y = global -Z  -> flip needed
#   YZ plane   -> local Y = global -Z  -> flip needed
#   Top face   -> local Y = global +Y  -> no flip needed
#   Front face -> local Y = global -Z  -> flip needed
#   Back face  -> local Y = global +Z  -> no flip needed
#
# _get_y_flip() reads the actual sketch Y axis from
# the Fusion API and returns +1 or -1 automatically.
# Users always give positive Y = upward. The engine
# corrects it transparently.
# ================================================

def _get_y_flip(sketch):
    try:
        y_axis = sketch.yDirection
        if y_axis.z < -0.5:
            return -1
        if y_axis.y < -0.5:
            return -1
        return 1
    except Exception:
        return 1


def _sy(sketch, y_val):
    """Convert Y from mm to cm with automatic sign correction."""
    return _get_y_flip(sketch) * y_val * MM


# ================================================
# CURVE ENUMERATION
# Shared by show_curves, trim_arc, and add_constraint
# so every command indexes curves the same way:
#   lines first, then arcs, then circles,
#   each in Fusion's native collection order.
# ================================================

def _enumerate_curves(sketch):
    items = []
    for line in sketch.sketchCurves.sketchLines:
        items.append(("line", line))
    for arc in sketch.sketchCurves.sketchArcs:
        items.append(("arc", arc))
    for circle in sketch.sketchCurves.sketchCircles:
        items.append(("circle", circle))
    return items


def _curve_at(sketch, index):
    items = _enumerate_curves(sketch)
    if index is None or index < 0 or index >= len(items):
        return None
    return items[index][1]


# ================================================
# MAIN EXECUTION
# ================================================

def execute(command):
    try:
        cmd = command["command"]
        dispatch = {
            "create_offset_plane": create_offset_plane,
            "create_sketch"      : create_sketch,
            "line"               : draw_line,
            "rectangle_2point"   : draw_rectangle_2point,
            "rectangle_center"   : draw_rectangle_center,
            "circle"             : draw_circle,
            "polygon"            : draw_polygon,
            "trim"               : lambda c: trim_curve(c),
            "trim_arc"           : lambda c: trim_circle_arc(c),
            "restore_circle"     : lambda c: restore_circle(c),
            "show_curves"        : lambda c: show_curves(c),
            "add_constraint"     : lambda c: add_constraint(c),
            "read_point"         : lambda c: read_point(c),
            "sketch_mirror"      : lambda c: mirror_sketch(c),
            "finish_sketch"      : lambda c: finish_sketch(c),
        }
        fn = dispatch.get(cmd)
        if fn:
            fn(command)
        else:
            _error("Sketch engine received an unknown command: {}.".format(cmd))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# CREATE SKETCH
# ================================================

def create_sketch(command):
    try:
        root   = get_root()
        plane  = command.get("plane")
        sketch = None
        _face_index_unverified = False

        if plane == "xy":
            sketch = root.sketches.add(root.xYConstructionPlane)

        elif plane == "yz":
            sketch = root.sketches.add(root.yZConstructionPlane)

        elif plane == "xz":
            sketch = root.sketches.add(root.xZConstructionPlane)

        elif plane == "offset":
            state      = state_manager.read_state()
            plane_name = state.get("last_plane_name")
            if not plane_name:
                _error("No offset plane stored. Please run create offset plane first.")
                return
            for p in root.constructionPlanes:
                if p.name == plane_name:
                    sketch = root.sketches.add(p)
                    break
            if sketch is None:
                _error("Offset plane {} not found in the design.".format(plane_name))
                return

        elif plane == "face":
            body, err = state_manager.resolve_body_or_none(root)
            if err:
                _error(err)
                return
            face_type  = command.get("face_type", "top")
            normal_map = {
                "top"   : lambda n: n.z >  0.9,
                "bottom": lambda n: n.z < -0.9,
                "front" : lambda n: n.y < -0.9,
                "back"  : lambda n: n.y >  0.9,
                "right" : lambda n: n.x >  0.9,
                "left"  : lambda n: n.x < -0.9,
            }
            test = normal_map.get(face_type)
            if test is None:
                _error("Unknown face type: {}. Use top, bottom, front, back, left, or right.".format(face_type))
                return
            selected_face = None
            for face in body.faces:
                geo = face.geometry
                if isinstance(geo, adsk.core.Plane) and test(geo.normal):
                    selected_face = face
                    break
            if selected_face is None:
                _error("No {} planar face found on the body.".format(face_type))
                return
            sketch = root.sketches.add(selected_face)

        elif plane == "face_index":
            body, err = state_manager.resolve_body_or_none(root)
            if err:
                _error(err)
                return
            face_index = command.get("face_index")
            if face_index is None:
                _error("Face index is missing from the command.")
                return
            if face_index >= body.faces.count:
                _error("Face index {} is out of range. The body has {} faces.".format(
                    face_index, body.faces.count))
                return
            target_face = body.faces.item(face_index)

            # FP-01: the raw index alone doesn't guarantee this is still the
            # same face the designer saw via show_faces/select_face — fillet,
            # chamfer, hole, boolean ops etc. can renumber body.faces.
            # Verify the stored geometric fingerprint before trusting it.
            fp_status = state_manager.verify_face_fingerprint(face_index, target_face)
            if fp_status is False:
                _error(
                    "Face index {} no longer matches the face you last saw there. "
                    "The model has changed since then. Say show faces again to "
                    "refresh, then pick a face index.".format(face_index)
                )
                return

            sketch = root.sketches.add(target_face)
            _face_index_unverified = (fp_status is None)

        else:
            _error("Unknown plane: {}. Use X Y, Y Z, X Z, offset, face, or face index.".format(plane))
            return

        sketch.isVisible = True
        state_manager.update_state("last_sketch_name", sketch.name)
        app.activeViewport.refresh()

        msg = "Sketch created on the {} plane.".format(
            plane if plane not in ("face", "face_index") else "{} face".format(
                command.get("face_type", "selected")))
        if plane == "face_index" and _face_index_unverified:
            msg += (
                " Note: this face index wasn't verified against a prior show "
                "faces, so please confirm visually it's the right one."
            )
        _speak(msg)

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# CREATE OFFSET PLANE
# ================================================

def create_offset_plane(command):
    try:
        root = get_root()
        body, err = state_manager.resolve_body_or_none(root)
        if err:
            _error(err)
            return

        offset_mm = command.get("offset", 0) * MM

        ref_face = None
        for face in body.faces:
            geo = face.geometry
            if isinstance(geo, adsk.core.Plane) and geo.normal.z > 0.9:
                ref_face = face
                break
        if ref_face is None:
            _error("No top planar face found on the body.")
            return

        plane_input = root.constructionPlanes.createInput()
        plane_input.setByOffset(ref_face, adsk.core.ValueInput.createByReal(offset_mm))
        new_plane = root.constructionPlanes.add(plane_input)

        state_manager.update_state("last_plane_name", new_plane.name)
        _speak("Offset construction plane created successfully.")

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# DRAW LINE
# Positive Y always = upward. _sy() auto-corrects.
# ================================================

def draw_line(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return
        x1 = command["x1"] * MM
        y1 = _sy(sketch, command["y1"])
        x2 = command["x2"] * MM
        y2 = _sy(sketch, command["y2"])
        sketch.sketchCurves.sketchLines.addByTwoPoints(
            adsk.core.Point3D.create(x1, y1, 0),
            adsk.core.Point3D.create(x2, y2, 0),
        )
        state_manager.update_state("last_sketch_data", {
            "type": "line",
            "x1": command["x1"], "y1": command["y1"],
            "x2": command["x2"], "y2": command["y2"],
        })
        _speak("Line drawn.")
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# RECTANGLE — TWO CORNER POINTS
# ================================================

def draw_rectangle_2point(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return
        x1 = command["x1"]
        y1 = command["y1"]
        x2 = command["x2"]
        y2 = command["y2"]
        sketch.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(x1 * MM, _sy(sketch, y1), 0),
            adsk.core.Point3D.create(x2 * MM, _sy(sketch, y2), 0),
        )
        state_manager.update_state("last_sketch_data", {
            "type": "rectangle", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        })
        _speak("Rectangle drawn from {},{} to {},{}.".format(
            int(x1), int(y1), int(x2), int(y2)))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# RECTANGLE — CENTER POINT
# ================================================

def draw_rectangle_center(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return
        cx     = command["cx"]
        cy     = command["cy"]
        length = command["length"]
        width  = command["width"]
        sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(cx * MM, _sy(sketch, cy), 0),
            adsk.core.Point3D.create(
                (cx + length / 2) * MM,
                _sy(sketch, cy + width / 2),
                0,
            ),
        )
        state_manager.update_state("last_sketch_data", {
            "type": "rectangle_center", "cx": cx, "cy": cy,
            "length": length, "width": width,
        })
        _speak("Centre rectangle drawn. {} by {} millimetres.".format(
            int(length), int(width)))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# DRAW CIRCLE
# ================================================

def draw_circle(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return
        cx       = command["cx"]
        cy       = command["cy"]
        diameter = command["diameter"]
        sketch.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(cx * MM, _sy(sketch, cy), 0),
            (diameter / 2) * MM,
        )
        state_manager.update_state("last_sketch_data", {
            "type": "circle", "cx": cx, "cy": cy, "diameter": diameter,
        })
        _speak("Circle drawn. Diameter {} millimetres at centre {},{}.".format(
            int(diameter), int(cx), int(cy)))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# DRAW POLYGON
# FIX: cy centre offset is sign-corrected separately
# from the sin component so the polygon is placed
# correctly when cy != 0 on a flipped plane.
# ================================================

def draw_polygon(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return
        cx     = command["cx"]
        cy     = command["cy"]
        sides  = command["sides"]
        radius = command["radius"]
        flip   = _get_y_flip(sketch)
        cy_cm  = _sy(sketch, cy)

        points = []
        for i in range(sides):
            angle = 2 * math.pi * i / sides
            points.append(adsk.core.Point3D.create(
                (cx + radius * math.cos(angle)) * MM,
                cy_cm + radius * math.sin(angle) * MM * flip,
                0,
            ))
        lines = sketch.sketchCurves.sketchLines
        for i in range(sides):
            lines.addByTwoPoints(points[i], points[(i + 1) % sides])

        state_manager.update_state("last_sketch_data", {
            "type": "polygon", "cx": cx, "cy": cy, "sides": sides, "radius": radius,
        })
        _speak("{}-sided polygon drawn. Radius {} millimetres.".format(sides, int(radius)))
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# SHOW CURVES — lists every line/arc/circle in the
# active sketch with an index number, so trim and
# constraint commands can be given entirely by voice
# after at most one glance (no click required to
# learn the index).
# ================================================

def show_curves(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return
        items = _enumerate_curves(sketch)
        if not items:
            _error("No curves found in the current sketch.")
            return

        parts = ["The sketch has {} curves. ".format(len(items))]
        for i, (kind, curve) in enumerate(items):
            if kind == "line":
                sp = curve.startSketchPoint.geometry
                ep = curve.endSketchPoint.geometry
                parts.append(
                    "Curve {}: line, {} {} to {} {}. ".format(
                        i,
                        int(round(sp.x / MM)), int(round(sp.y / MM)),
                        int(round(ep.x / MM)), int(round(ep.y / MM)),
                    )
                )
            elif kind == "arc":
                parts.append(
                    "Curve {}: arc, radius {} millimetres. ".format(
                        i, int(round(curve.radius / MM))
                    )
                )
            elif kind == "circle":
                parts.append(
                    "Curve {}: circle, radius {} millimetres. ".format(
                        i, int(round(curve.radius / MM))
                    )
                )

        parts.append(
            "Say tangent, parallel, perpendicular, or symmetric, "
            "followed by the curve numbers, or say trim minor arc "
            "circle followed by a number."
        )
        _speak("".join(parts))

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# TRIM — click a line/arc segment in the viewport,
# then say "trim". Uses the exact click point so
# Fusion knows which segment to remove (this is the
# same click-then-speak pattern as select_edge).
# ================================================

def _line_circle_intersections(center, radius, line):
    """Analytic 2D line-circle intersection, computed directly rather than
    via Fusion's Circle3D.intersectWithCurve() (see TR-04 note below).
    `line` is a SketchLine; center is a Point3D-like with .x/.y; both are
    assumed to already be in the same 2D sketch-plane coordinate system
    (z=0), which is true for all geometry created by this engine.
    Returns a list of 0, 1, or 2 Point3D intersection points, restricted
    to the BOUNDED segment (not the infinite line)."""
    p1 = line.startSketchPoint.geometry
    p2 = line.endSketchPoint.geometry
    dx, dy = p2.x - p1.x, p2.y - p1.y
    fx, fy = p1.x - center.x, p1.y - center.y

    a = dx * dx + dy * dy
    if a == 0:
        return []   # degenerate zero-length line

    b = 2 * (fx * dx + fy * dy)
    c = (fx * fx + fy * fy) - radius * radius
    disc = b * b - 4 * a * c
    if disc < 0:
        return []   # line does not reach the circle

    disc_sqrt = math.sqrt(disc)
    t1 = (-b - disc_sqrt) / (2 * a)
    t2 = (-b + disc_sqrt) / (2 * a)

    points = []
    for t in (t1, t2):
        if -1e-9 <= t <= 1 + 1e-9:   # within the bounded segment (small float tolerance)
            tc = min(max(t, 0.0), 1.0)
            points.append(adsk.core.Point3D.create(p1.x + tc * dx, p1.y + tc * dy, 0))
    return points


def _circle_split_points(sketch, circle):
    """Find every point where straight lines in the sketch cross this
    circle. Returns (points, skipped_arc_count). Arcs/other circles are
    not yet supported for analytic intersection — reported explicitly
    via skipped_arc_count rather than silently ignored."""
    center = circle.centerSketchPoint.geometry
    radius = circle.radius
    pts = []
    skipped = 0
    for kind, other in _enumerate_curves(sketch):
        if other == circle:
            continue
        if kind == "line":
            pts.extend(_line_circle_intersections(center, radius, other))
        else:
            skipped += 1
    return pts, skipped


def _replace_circle_with_arc(sketch, circle, keep_sweep):
    """Delete the full circle and construct exactly one explicit arc in
    its place — sweeping `keep_sweep` radians (signed: positive = CCW)
    starting from the circle's first split point. This sidesteps
    SketchCircle.trim(point) entirely, whose kept-side behavior proved
    unreliable (see TR-05 note below)."""
    center = circle.centerSketchPoint.geometry
    pts, _ = _circle_split_points(sketch, circle)
    p0 = pts[0]
    center_pt = adsk.core.Point3D.create(center.x, center.y, 0)
    start_pt  = adsk.core.Point3D.create(p0.x, p0.y, 0)
    circle.deleteMe()
    sketch.sketchCurves.sketchArcs.addByCenterStartSweep(center_pt, start_pt, keep_sweep)


# ================================================
# READ POINT — click a point (endpoint, midpoint,
# centre, or existing sketch point) then say "what
# are the coordinates" / "read point", to hear its
# location spoken back instead of having to
# pre-calculate it.
#
# If a sketch is currently active, coordinates are
# converted into that sketch's own local 2D plane
# (via Sketch.modelToSketchSpace) so the numbers match
# exactly what you'd say in a circle/line/rectangle
# command on that same sketch. With no active sketch,
# raw 3D model coordinates are reported instead.
#
# Honest limitation: this reads whatever point Fusion's
# own Selection already resolved from your click — it
# does not add extra "snap to nearest key point" logic
# on top. In practice, clicking directly on an existing
# endpoint, midpoint marker, or a circle/arc's centre
# point (Fusion shows these as small glyphs and selects
# them precisely when clicked) works well; clicking on
# open empty geometry does not "snap" to anything nearby
# the way an active drawing tool's OSNAP indicator would.
# ================================================

def read_point(command):
    try:
        selections = ui.activeSelections
        if selections.count == 0:
            _error(
                "No point selected. Click a point, endpoint, midpoint, or "
                "centre in the viewport, then say read point."
            )
            return

        sel = selections.item(0)
        world_point = sel.point
        if world_point is None:
            _error(
                "Could not read a coordinate from that selection. Click "
                "directly on a point and try again."
            )
            return

        sketch = _get_active_or_tracked_sketch()
        if sketch:
            try:
                local = sketch.modelToSketchSpace(world_point)
                x_mm = local.x / MM
                y_mm = local.y / MM
                _speak(
                    "Point at X {:.1f}, Y {:.1f} millimetres on the "
                    "current sketch.".format(x_mm, y_mm)
                )
                return
            except Exception:
                pass   # fall through to raw model coordinates below

        x_mm = world_point.x / MM
        y_mm = world_point.y / MM
        z_mm = world_point.z / MM
        _speak(
            "Point at X {:.1f}, Y {:.1f}, Z {:.1f} millimetres, "
            "in model space — no active sketch to convert to.".format(
                x_mm, y_mm, z_mm)
        )

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# TRIM — click a line/arc segment, or a circle, in
# the viewport, then say "trim".
#
# Lines and arcs: uses Fusion's standard curve.trim(point),
# which is the documented, reliable behavior for open curves
# (removes the segment nearest the click).
#
# Circles: TR-05 FIX — SketchCircle.trim(point) was observed
# removing the OPPOSITE arc from the one clicked (unreliable
# kept-side mapping for closed curves). Instead, this computes
# the circle's split points analytically, determines which of
# the two resulting arcs contains the click point, and
# deterministically reconstructs ONLY the other one (the one
# NOT clicked) — giving exact, predictable click-driven control.
# ================================================

def trim_curve(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return

        selections = ui.activeSelections
        if selections.count == 0:
            _error(
                "No curve selected. Click the line or arc segment you want "
                "removed, then say trim."
            )
            return

        sel   = selections.item(0)
        curve = sel.entity
        point = sel.point

        if not isinstance(curve, (adsk.fusion.SketchLine,
                                   adsk.fusion.SketchArc,
                                   adsk.fusion.SketchCircle)):
            _error("The selected item is not a trimmable sketch curve.")
            return

        if point is None:
            _error("Could not read the click location. Click directly on the curve and try again.")
            return

        if isinstance(curve, adsk.fusion.SketchCircle):
            pts, skipped = _circle_split_points(sketch, curve)
            if len(pts) != 2:
                detail = ""
                if skipped:
                    detail = (
                        " ({} arc or curved edge in the sketch was skipped — "
                        "only straight-line intersections are currently "
                        "supported.)".format(skipped)
                    )
                _error(
                    "Trimming a circle needs exactly one straight line "
                    "crossing it, giving two split points. Found {} split "
                    "point(s).{}".format(len(pts), detail)
                )
                return

            center = curve.centerSketchPoint.geometry
            p0, p1 = pts[0], pts[1]
            a0 = math.atan2(p0.y - center.y, p0.x - center.x)
            a1 = math.atan2(p1.y - center.y, p1.x - center.x)
            delta = (a1 - a0) % (2 * math.pi)   # CCW sweep p0 -> p1, in [0, 2π)

            click_angle = math.atan2(point.y - center.y, point.x - center.x)
            click_delta = (click_angle - a0) % (2 * math.pi)
            click_in_forward_arc = click_delta <= delta

            # Keep whichever arc does NOT contain the clicked point.
            keep_sweep = (delta - 2 * math.pi) if click_in_forward_arc else delta
            _replace_circle_with_arc(sketch, curve, keep_sweep)
            _speak("Segment trimmed.")
            return

        curve.trim(point)
        _speak("Segment trimmed.")

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# TRIM ARC — fully voice-driven major/minor arc
# removal on a circle. Requires exactly one straight
# line crossing the circle (giving two split points)
# so "major" vs "minor" is unambiguous. No click needed.
#
# TR-04 FIX: intersection points are computed with plain
# analytic geometry (_line_circle_intersections) instead
# of Fusion's Circle3D.intersectWithCurve(), which was
# silently swallowing exceptions in a bare try/except and
# reporting "0 intersections found" even when the geometry
# genuinely crossed — a false negative, not a real absence
# of intersections.
# ================================================

def trim_circle_arc(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return

        idx = command.get("curve_index")
        target = _curve_at(sketch, idx)

        if target is None:
            _error(
                "Curve index {} not found. Say show curves to hear current "
                "indices.".format(idx)
            )
            return

        if isinstance(target, adsk.fusion.SketchArc):
            # TR-06: this index almost certainly used to be a circle that
            # was already trimmed (a trim always converts a circle into an
            # arc, which then moves out of the "circle" section of
            # show_curves' listing). Don't guess and auto-fix silently —
            # that would be inference, which this project deliberately
            # avoids. Instead, name the deterministic recovery command.
            _error(
                "Curve {} is currently an arc, not a circle — it looks like "
                "it was already trimmed. Say restore circle {}, then show "
                "curves again, then trim it the other way.".format(idx, idx)
            )
            return

        if not isinstance(target, adsk.fusion.SketchCircle):
            _error(
                "Curve {} is not a circle. Say show curves to hear current "
                "indices.".format(idx)
            )
            return

        circle = target

        pts, skipped = _circle_split_points(sketch, circle)
        if len(pts) != 2:
            detail = ""
            if skipped:
                detail = (
                    " ({} arc or curved edge in the sketch was skipped — "
                    "only straight-line intersections are currently "
                    "supported.)".format(skipped)
                )
            _error(
                "Major or minor arc trim needs exactly one straight line "
                "crossing the circle, giving two split points. Found {} "
                "split point(s).{} Draw a line through the circle and "
                "try again.".format(len(pts), detail)
            )
            return

        center = circle.centerSketchPoint.geometry
        p0, p1 = pts[0], pts[1]
        a0 = math.atan2(p0.y - center.y, p0.x - center.x)
        a1 = math.atan2(p1.y - center.y, p1.x - center.x)
        delta = (a1 - a0) % (2 * math.pi)   # CCW sweep p0 -> p1, in [0, 2π)

        want_major     = command.get("arc_side", "minor") == "major"
        delta_is_major = delta > math.pi
        # If delta already represents the requested side, keep it as-is;
        # otherwise use the complementary (other-direction) sweep.
        keep_sweep = delta if (want_major == delta_is_major) else (delta - 2 * math.pi)

        if abs(delta - math.pi) < 1e-6:
            _speak(
                "Note: the crossing line passes through the circle's centre, "
                "so both arcs are equal halves — major and minor are the "
                "same size here."
            )

        _replace_circle_with_arc(sketch, circle, keep_sweep)
        _speak("{} arc kept on circle {}. The rest was removed.".format(
            command.get("arc_side", "minor").capitalize(), idx))

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# RESTORE CIRCLE — deterministically converts a
# previously-trimmed arc back into its original full
# circle, so the designer can trim it again with a
# different major/minor choice.
#
# TR-06: this is the reliable alternative to voice
# "undo" for the trim-arc workflow. It does NOT depend
# on Fusion's undo-grouping behavior (unverified — see
# fix register), and it needs no backup storage: a
# SketchArc already carries the same centre and radius
# as the circle it came from, so the full circle can
# always be reconstructed directly from the arc itself.
#
# Deliberately explicit rather than automatic — this
# project's design principle is that the system never
# infers or silently reinterprets a command; "trim major
# arc circle N" pointed at an arc always errors and names
# this command rather than guessing the designer meant
# "restore, then trim."
# ================================================

def restore_circle(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return

        idx = command.get("curve_index")
        target = _curve_at(sketch, idx)

        if target is None:
            _error(
                "Curve index {} not found. Say show curves to hear current "
                "indices.".format(idx)
            )
            return

        if isinstance(target, adsk.fusion.SketchCircle):
            _error(
                "Curve {} is already a full circle — nothing to restore.".format(idx)
            )
            return

        if not isinstance(target, adsk.fusion.SketchArc):
            _error(
                "Curve {} is not an arc, so it can't be restored to a circle.".format(idx)
            )
            return

        center = target.centerSketchPoint.geometry
        radius = target.radius
        target.deleteMe()
        sketch.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(center.x, center.y, 0), radius
        )
        _speak(
            "Curve {} restored to a full circle. Say show curves to get its "
            "new index, then trim it again.".format(idx)
        )

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# ADD CONSTRAINT — tangent, parallel, perpendicular,
# symmetric. Fully voice-driven: curves are referenced
# by index from show_curves, no click required.
# ================================================

def add_constraint(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return

        ctype = command.get("constraint_type")
        i1    = command.get("index_1")
        i2    = command.get("index_2")
        c1    = _curve_at(sketch, i1)
        c2    = _curve_at(sketch, i2)

        if not c1 or not c2:
            _error(
                "Curve index out of range. Say show curves to hear current indices."
            )
            return

        gc = sketch.geometricConstraints

        if ctype == "tangent":
            gc.addTangent(c1, c2)
            _speak("Tangent constraint applied between curve {} and curve {}.".format(i1, i2))

        elif ctype == "parallel":
            gc.addParallel(c1, c2)
            _speak("Parallel constraint applied between curve {} and curve {}.".format(i1, i2))

        elif ctype == "perpendicular":
            gc.addPerpendicular(c1, c2)
            _speak("Perpendicular constraint applied between curve {} and curve {}.".format(i1, i2))

        elif ctype == "symmetric":
            i3 = command.get("index_3")
            c3 = _curve_at(sketch, i3)
            if not c3 or not isinstance(c3, adsk.fusion.SketchLine):
                _error(
                    "Symmetric constraint needs a third curve index — a "
                    "straight line to mirror about."
                )
                return
            gc.addSymmetry(c1, c2, c3)
            _speak(
                "Symmetric constraint applied between curve {} and curve {} "
                "about line {}.".format(i1, i2, i3)
            )

        else:
            _error(
                "Unknown constraint type: {}. Use tangent, parallel, "
                "perpendicular, or symmetric.".format(ctype)
            )

    except RuntimeError:
        _error(
            "Fusion rejected that constraint — the geometry may already be "
            "fully constrained, or the curves cannot satisfy it. "
            "Try a different pair."
        )
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# MIRROR SKETCH
# Collects existing curves BEFORE adding axis line
# so the axis itself is not mirrored.
# ================================================

def mirror_sketch(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return
        if sketch.sketchCurves.count == 0:
            _error("No curves in the sketch to mirror.")
            return
        objects = adsk.core.ObjectCollection.create()
        for curve in sketch.sketchCurves:
            objects.add(curve)
        mirror_line = sketch.sketchCurves.sketchLines.addByTwoPoints(
            adsk.core.Point3D.create(0, -500 * MM, 0),
            adsk.core.Point3D.create(0,  500 * MM, 0),
        )
        sketch.mirror(objects, mirror_line)
        _speak("Sketch mirrored about the Y axis.")
    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# FINISH SKETCH
# ================================================

def finish_sketch(command):
    try:
        sketch = _get_sketch_or_error()
        if not sketch:
            return
        count = sketch.profiles.count
        if count == 0:
            _error("No closed profile found. Check that your sketch geometry forms a closed loop.")
            return
        state_manager.update_state("last_profile_index", 0)
        if count == 1:
            _speak("Sketch finished. One profile found and selected. Ready to extrude.")
        else:
            profile_names = ", ".join(["profile {}".format(i) for i in range(count)])
            _speak(
                "Sketch finished. {} profiles found: {}. "
                "Profile zero is selected by default. "
                "To use a different one, add profile index to your extrude command.".format(
                    count, profile_names)
            )
    except Exception:
        ui.messageBox(traceback.format_exc())
