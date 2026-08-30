# ================================================
# command_reader.py
# Command I/O and ID-dedup for StepByStepCAD
# ================================================

import json
import os
import adsk.core

app = adsk.core.Application.get()
ui  = app.userInterface

BASE_DIR     = os.path.dirname(os.path.realpath(__file__))
COMMAND_FILE = os.path.join(BASE_DIR, "command.json")
STATE_FILE   = os.path.join(BASE_DIR, "state.json")


# ================================================
# READ COMMAND
# ================================================

def read_command():
    if not os.path.exists(COMMAND_FILE):
        return None
    try:
        with open(COMMAND_FILE, "r") as f:
            return json.load(f)
    except Exception:
        # Only show popup for a genuine parse error — not for "file missing"
        ui.messageBox("command.json parse error — check JSON syntax")
        return None


# ================================================
# LOAD / SAVE STATE  (local helpers)
# ================================================

def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
    except Exception:
        pass   # silent — disk errors shouldn't crash the add-in


# ================================================
# CHECK NEW COMMAND USING ID
# ================================================

def is_new_command(command):
    current_id = command.get("id", -1)
    if current_id == -1:
        # Missing ID — show once so the user can fix the JSON
        ui.messageBox("command.json is missing an 'id' field")
        return False
    last_id = _load_state().get("last_command_id", -1)
    return current_id != last_id


# ================================================
# UPDATE LAST COMMAND  (called after successful exec)
# ================================================

def update_last_command(command):
    state = _load_state()
    state["last_command_id"] = command["id"]
    _save_state(state)


# ================================================
# VALIDATE COMMAND
# ================================================

def validate_command(command):
    missing = [f for f in ("command", "id") if f not in command]
    if missing:
        ui.messageBox(f"command.json missing fields: {', '.join(missing)}")
        return False
    return True


# ================================================
# EXECUTE UNDO
# ================================================

def execute_undo():
    try:
        undoCmd = ui.commandDefinitions.itemById("UndoCommand")
        if undoCmd:
            undoCmd.execute()
    except Exception:
        ui.messageBox("Undo failed — no undo history available")
