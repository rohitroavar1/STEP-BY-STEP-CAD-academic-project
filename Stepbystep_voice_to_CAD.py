# ================================================
# Stepbystep_voice_to_CAD.py
# Fusion-Safe Dispatcher — StepByStepCAD_VoiceAuto
# ================================================

import adsk.core
import adsk.fusion
import traceback
import threading
import time
import sys
import os
import json
import importlib

_addin_dir = os.path.dirname(os.path.realpath(__file__))
if _addin_dir not in sys.path:
    sys.path.insert(0, _addin_dir)

command_reader = importlib.import_module("command_reader")
state_manager  = importlib.import_module("state_manager")
sketch_engine  = importlib.import_module("sketch_engine")
solid_engine   = importlib.import_module("solid_engine")
modify_engine  = importlib.import_module("modify_engine")

app = adsk.core.Application.get()
ui  = app.userInterface

handlers      = []
command_event = None
worker_thread = None
running       = False
_executing    = False
_palette      = None

TRIGGER_FILE      = os.path.join(_addin_dir, "mic_trigger.txt")
MIC_STATUS_FILE   = os.path.join(_addin_dir, "mic_status.txt")
PALETTE_ID        = "StepByStepCAD_Palette"
PALETTE_TITLE     = "StepByStepCAD Voice"
PALETTE_HTML_FILE = os.path.join(_addin_dir, "palette.html")
HOTKEY_CMD_ID     = "StepByStepCAD_RecordHotkey"


# ================================================
# PALETTE HTML
# Written to palette.html on startup.
# This string is the authoritative source —
# the standalone palette.html is overwritten
# every time the add-in loads.
# ================================================

PALETTE_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #1e1e1e;
    color: #ffffff;
    display: flex;
    flex-direction: column;
    align-items: center;
    height: 100vh;
    padding: 14px;
    gap: 10px;
    user-select: none;
  }
  .title {
    font-size: 11px;
    font-weight: 600;
    color: #888;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-top: 4px;
  }
  #recordBtn {
    width: 100%;
    padding: 13px 0;
    background: #e53935;
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    transition: background 0.15s, transform 0.1s;
  }
  #recordBtn:hover  { background: #c62828; }
  #recordBtn:active { transform: scale(0.97); }
  #recordBtn.active {
    background: #388e3c;
    animation: pulse 1s infinite;
  }
  @keyframes pulse {
    0%   { opacity: 1.0; }
    50%  { opacity: 0.65; }
    100% { opacity: 1.0; }
  }
  #status {
    font-size: 11px;
    color: #aaa;
    text-align: center;
    min-height: 16px;
    width: 100%;
  }
  .hint {
    font-size: 10px;
    color: #444;
    text-align: center;
  }
  #log-wrap {
    width: 100%;
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  #log-label {
    font-size: 9px;
    color: #444;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-bottom: 4px;
  }
  #log {
    flex: 1;
    overflow-y: auto;
    font-size: 10px;
    line-height: 1.55;
    color: #666;
    border-top: 1px solid #2a2a2a;
    padding-top: 6px;
  }
  #log .entry { padding: 1px 0; border-bottom: 1px solid #252525; }
  #log .entry.speak { color: #5cb3ff; }
  #log .entry.error { color: #f28b82; }
  #log .entry.done  { color: #81c995; }
</style>
</head>
<body>
  <div class="title">StepByStepCAD &#9671; Voice</div>
  <button id="recordBtn" onclick="record()">&#127908; RECORD</button>
  <div id="status">Ready &#8212; click to speak a command</div>
  <div class="hint">Ctrl+Shift+R also works as hotkey</div>
  <div id="log-wrap">
    <div id="log-label">&#128266; System log</div>
    <div id="log"></div>
  </div>

  <script>
    var btn   = document.getElementById('recordBtn');
    var status = document.getElementById('status');
    var log   = document.getElementById('log');
    var busy  = false;
    var _voice = null;

    var PREFERRED_VOICES = [
      'Microsoft Jenny Online (Natural) - English (United States)',
      'Microsoft Aria Online (Natural) - English (United States)',
      'Microsoft Guy Online (Natural) - English (United States)',
      'Microsoft Natasha Online (Natural) - English (Australia)',
      'Google US English',
      'Microsoft David - English (United States)',
      'Microsoft Zira - English (United States)',
      'Samantha',
      'Alex'
    ];

    function _selectVoice() {
      var voices = window.speechSynthesis.getVoices();
      if (!voices || voices.length === 0) return;
      for (var p = 0; p < PREFERRED_VOICES.length; p++) {
        for (var v = 0; v < voices.length; v++) {
          if (voices[v].name === PREFERRED_VOICES[p]) { _voice = voices[v]; return; }
        }
      }
      for (var v = 0; v < voices.length; v++) {
        if (voices[v].lang && voices[v].lang.indexOf('en') === 0) { _voice = voices[v]; return; }
      }
    }
    window.speechSynthesis.onvoiceschanged = _selectVoice;
    _selectVoice();

    // AUDIO-01 FIX: many Chromium-based embedded webviews (including
    // Fusion's palette) enforce an autoplay policy that blocks
    // speechSynthesis.speak() entirely until the page has received AT
    // LEAST ONE genuine user gesture (a click or keypress) anywhere on
    // the page. Previously, the only such gesture was clicking RECORD —
    // so voice feedback worked after a manual click, but a session
    // driven purely by wake word (which never touches the palette
    // directly) could leave speech permanently locked with no audio
    // ever playing, even though the text log kept updating normally.
    // This listens for the FIRST click or keypress ANYWHERE on the
    // palette (not just the record button) to unlock audio as early as
    // possible, regardless of which trigger method (record button, wake
    // word, or keyboard shortcut) is actually used afterward.
    var _audioUnlocked = false;
    function _unlockAudio() {
      if (_audioUnlocked) return;
      _audioUnlocked = true;
      try {
        var u = new SpeechSynthesisUtterance(' ');
        u.volume = 0;
        window.speechSynthesis.speak(u);
      } catch (e) {}
    }
    document.addEventListener('click', _unlockAudio, { once: true });
    document.addEventListener('keydown', _unlockAudio, { once: true });

    function _speak(text) {
      window.speechSynthesis.cancel();
      // Defensive: some browsers leave the speech queue in a "paused"
      // state after periods of inactivity, silently dropping subsequent
      // .speak() calls. Explicitly resuming before every utterance is a
      // well-known, harmless workaround for that class of bug.
      window.speechSynthesis.resume();
      var u = new SpeechSynthesisUtterance(text);
      u.rate = 1.0; u.pitch = 1.0; u.volume = 1.0;
      if (_voice) u.voice = _voice;
      window.speechSynthesis.speak(u);
    }

    function _log(text, cls) {
      var d = document.createElement('div');
      d.className = 'entry ' + (cls || '');
      d.textContent = text;
      log.appendChild(d);
      while (log.children.length > 40) { log.removeChild(log.firstChild); }
      log.scrollTop = log.scrollHeight;
    }

    function record() {
      if (busy) return;
      busy = true;
      btn.classList.add('active');
      btn.textContent = 'LISTENING...';
      status.textContent = 'Mic active — speak now';
      adsk.fusionSendData('record', '{}');
      setTimeout(function() {
        btn.classList.remove('active');
        btn.textContent = '&#127908; RECORD';
        status.textContent = 'Processing...';
        busy = false;
      }, 10500);
    }

    window.fusionJavaScriptHandler = {
      handle: function(action, data) {

        if (action === 'recording') {
          // Wake word detected — same visual as clicking RECORD manually
          // No timeout: Python sends 'processing' then 'ready' to reset
          btn.classList.add('active');
          btn.textContent = 'LISTENING...';
          status.textContent = 'Mic active — speak now';
          busy = true;
          _log('🎤 Wake word detected — mic active', 'speak');

        } else if (action === 'processing') {
          btn.classList.remove('active');
          btn.textContent = '🎤 RECORD';
          status.textContent = 'Processing...';
          busy = false;
          _log('⏳ Processing...', 'done');

        } else if (action === 'speak') {
          status.textContent = data;
          _speak(data);
          _log('🔊 ' + data, 'speak');

        } else if (action === 'error') {
          status.textContent = data;
          _speak(data);
          _log('⚠ ' + data, 'error');

        } else if (action === 'status') {
          status.textContent = data;
          _log('✓ ' + data, 'done');

        } else if (action === 'ready') {
          status.textContent = 'Ready — click to speak a command';
          btn.classList.remove('active');
          btn.textContent = '🎤 RECORD';
          busy = false;
        }

        return true;
      }
    };
  </script>
</body>
</html>
"""


# ================================================
# TWO-WAY COMMUNICATION FUNCTIONS
# These are the only functions that write to the
# palette. All engine modules call these via the
# set_speak() / set_error() injection pattern.
#
#   _speak(msg)  → TTS + blue log   (success, guidance, index feedback)
#   _error(msg)  → TTS + red log    (user-facing errors — no traceback)
#   _status(msg) → silent + green   (internal status, no speech)
#
# ui.messageBox() is ONLY used for:
#   - tracebacks (developer errors that need full text)
#   - add-in startup / stop notifications
# ================================================

def _speak(message):
    global _palette
    try:
        if _palette and _palette.isVisible:
            _palette.sendInfoToHTML("speak", message)
    except Exception:
        pass


def _error(message):
    global _palette
    try:
        if _palette and _palette.isVisible:
            _palette.sendInfoToHTML("error", message)
    except Exception:
        pass


def _status(message):
    global _palette
    try:
        if _palette and _palette.isVisible:
            _palette.sendInfoToHTML("status", message)
    except Exception:
        pass


def _set_recording():
    """Tell palette to go green — same visual as clicking RECORD button."""
    global _palette
    try:
        if _palette and _palette.isVisible:
            _palette.sendInfoToHTML("recording", "")
    except Exception:
        pass


def _set_processing():
    """Tell palette mic stopped recording, now processing."""
    global _palette
    try:
        if _palette and _palette.isVisible:
            _palette.sendInfoToHTML("processing", "")
    except Exception:
        pass


def _set_ready():
    """Tell palette to reset to idle state."""
    global _palette
    try:
        if _palette and _palette.isVisible:
            _palette.sendInfoToHTML("ready", "")
    except Exception:
        pass


# ================================================
# STATUS LOOP
#
# Polls mic_status.txt every 250ms — fast enough
# for near-realtime visual feedback when a wake
# word fires in the external voice listener.
#
# Status tokens written by voice_listener_VAD.py:
#   "active"     → palette goes green / LISTENING
#   "processing" → palette shows PROCESSING...
#   "ready"      → palette resets to idle
#
# This runs in its own daemon thread alongside
# background_loop so command polling (2s interval)
# and status polling (250ms interval) are independent.
# ================================================

def status_loop():
    global running
    while running:
        try:
            if os.path.exists(MIC_STATUS_FILE):
                with open(MIC_STATUS_FILE, "r") as f:
                    content = f.read().strip()
                try:
                    os.remove(MIC_STATUS_FILE)
                except Exception:
                    pass
                if content == "active":
                    _set_recording()
                elif content == "processing":
                    _set_processing()
                elif content == "ready":
                    _set_ready()
        except Exception:
            pass
        time.sleep(0.25)   # 250ms — fast enough, light on CPU


# ================================================
# STARTUP RESET
# ================================================

def _reset_on_startup():
    state_manager.clear_state()
    try:
        with open(os.path.join(_addin_dir, "command.json"), "w") as f:
            json.dump({}, f)
    except Exception:
        pass
    try:
        if os.path.exists(TRIGGER_FILE):
            os.remove(TRIGGER_FILE)
    except Exception:
        pass
    try:
        with open(PALETTE_HTML_FILE, "w", encoding="utf-8") as f:
            f.write(PALETTE_HTML)
    except Exception:
        pass


# ================================================
# MIC TRIGGER
# ================================================

def _write_mic_trigger():
    try:
        with open(TRIGGER_FILE, "w") as f:
            f.write("record")
    except Exception:
        pass


# ================================================
# PALETTE MESSAGE HANDLER (JS -> Python)
# ================================================

class PaletteMessageHandler(adsk.core.HTMLEventHandler):

    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            event_args = adsk.core.HTMLEventArgs.cast(args)
            if event_args.action == "record":
                _write_mic_trigger()
                app.log("StepByStepCAD: Mic activated")
        except Exception:
            ui.messageBox(traceback.format_exc())


# ================================================
# HOTKEY COMMAND (HOTKEY-01)
#
# The palette's "Ctrl+Shift+R also works as hotkey" hint previously had
# NO backing implementation anywhere in this add-in — no keyboard
# listener, no registered command. This adds a real, standard Fusion
# CommandDefinition that triggers the mic exactly like the RECORD
# button. Fusion does not support hardcoding a global keyboard shortcut
# purely from a script in a fully reliable, version-independent way —
# the correct, Fusion-native mechanism is to register a command (done
# here) and then let the shortcut be ASSIGNED to it via Fusion's own
# standard UI, which only needs doing once:
#
#   Tools tab -> right-click "StepByStepCAD Record" in any panel
#   (or Ctrl+click search for it) -> "Change Keyboard Shortcut..."
#   -> set to Ctrl+Shift+R.
#
# Once assigned, Fusion remembers it across sessions on this machine,
# the same way any other custom keyboard shortcut works.
# ================================================

class HotkeyExecuteHandler(adsk.core.CommandCreatedEventHandler):

    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            _write_mic_trigger()
            app.log("StepByStepCAD: Hotkey triggered mic")
        except Exception:
            ui.messageBox(traceback.format_exc())


def _register_hotkey_command():
    try:
        existing = ui.commandDefinitions.itemById(HOTKEY_CMD_ID)
        if existing:
            existing.deleteMe()
        cmd_def = ui.commandDefinitions.addButtonDefinition(
            HOTKEY_CMD_ID,
            "StepByStepCAD Record",
            "Trigger a StepByStepCAD voice command recording, same as "
            "clicking RECORD in the palette. Assign a keyboard shortcut "
            "to this command via Tools > right-click > Change Keyboard "
            "Shortcut for one-touch voice control.",
        )
        hotkey_handler = HotkeyExecuteHandler()
        cmd_def.commandCreated.add(hotkey_handler)
        handlers.append(hotkey_handler)
        return cmd_def
    except Exception:
        ui.messageBox(traceback.format_exc())
        return None


# ================================================
# CAD COMMAND EXECUTE HANDLER (Fusion main thread)
# ================================================

class CommandExecuteHandler(adsk.core.CustomEventHandler):

    def __init__(self):
        super().__init__()

    def notify(self, args):
        global _executing
        if _executing:
            return
        _executing = True
        try:
            command = command_reader.read_command()
            if not command:
                return
            if not command_reader.is_new_command(command):
                return
            if not command_reader.validate_command(command):
                return

            cmd = command["command"]

            if cmd == "undo":
                command_reader.execute_undo()
                state_manager.reset_after_undo()
                _speak("Undo complete.")

            elif cmd in sketch_engine.SKETCH_COMMANDS:
                sketch_engine.execute(command)

            elif cmd in solid_engine.SOLID_COMMANDS:
                solid_engine.execute(command)

            elif cmd in modify_engine.MODIFY_COMMANDS:
                modify_engine.execute(command)

            else:
                _error(
                    "Unknown command: {}. Check spelling and try again.".format(cmd)
                )
                return

            command_reader.update_last_command(command)

            # Silent status tick — engines speak their own confirmations.
            # This just updates the log panel with a green tick.
            _status("Done: {}  (id {})".format(cmd, command.get("id")))

        except Exception:
            ui.messageBox(traceback.format_exc())
        finally:
            _executing = False


# ================================================
# BACKGROUND THREAD
# ================================================

def background_loop():
    global running
    while running:
        try:
            app.fireCustomEvent("VoiceCADCommandEvent")
        except Exception:
            pass
        time.sleep(2)


# ================================================
# CREATE FLOATING PALETTE
# ================================================

def _create_palette():
    global _palette

    existing = ui.palettes.itemById(PALETTE_ID)
    if existing:
        existing.deleteMe()

    if not os.path.exists(PALETTE_HTML_FILE):
        ui.messageBox(
            "palette.html not found in add-in folder.\n"
            "Commands still execute via command.json."
        )
        return None

    html_url = PALETTE_HTML_FILE.replace("\\", "/")
    if not html_url.startswith("/"):
        html_url = "/" + html_url
    html_url = "file://" + html_url

    _palette = ui.palettes.add(
        PALETTE_ID,
        PALETTE_TITLE,
        html_url,
        True,    # isVisible
        True,    # showCloseButton
        True,    # isResizable
        260,     # width px
        320,     # height px — taller to show log panel
        False,   # useNewWebBrowser
    )

    if _palette:
        _palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateFloating
        msg_handler = PaletteMessageHandler()
        _palette.incomingFromHTML.add(msg_handler)
        handlers.append(msg_handler)

    return _palette


# ================================================
# INJECT SPEAK FUNCTIONS INTO ENGINE MODULES
# Called once after palette is created so all
# three engines can call _speak/_error without
# importing this module (which would be circular).
# ================================================

def _inject_speak():
    sketch_engine.set_speak(_speak)
    sketch_engine.set_error(_error)
    solid_engine.set_speak(_speak)
    solid_engine.set_error(_error)
    modify_engine.set_speak(_speak)
    modify_engine.set_error(_error)


# ================================================
# RUN ADD-IN
# ================================================

def run(context):
    global command_event, worker_thread, running

    try:
        _reset_on_startup()

        command_event = app.registerCustomEvent("VoiceCADCommandEvent")
        exec_handler  = CommandExecuteHandler()
        command_event.add(exec_handler)
        handlers.append(exec_handler)

        _create_palette()
        _inject_speak()
        _register_hotkey_command()

        running       = True
        worker_thread = threading.Thread(target=background_loop, daemon=True)
        worker_thread.start()
        status_thread = threading.Thread(target=status_loop, daemon=True)
        status_thread.start()

        ui.messageBox(
            "StepByStepCAD Voice Add-in  Started OK\n\n"
            "Floating panel is open with voice feedback enabled.\n"
            "Start voice_listener_VAD.py in a terminal,\n"
            "then click RECORD to speak a command.\n\n"
            "The system will now speak back confirmations,\n"
            "index numbers, and guidance messages aloud.\n\n"
            "One-time setup for a keyboard shortcut: right-click\n"
            "'StepByStepCAD Record' in any toolbar panel (or search\n"
            "for it via the top search bar) and choose 'Change\n"
            "Keyboard Shortcut...' to assign Ctrl+Shift+R or any key\n"
            "combination you prefer. Fusion remembers it after that."
        )

    except Exception:
        ui.messageBox(traceback.format_exc())


# ================================================
# STOP ADD-IN
# ================================================

def stop(context):
    global running, command_event, _palette

    try:
        running = False

        try:
            existing = ui.palettes.itemById(PALETTE_ID)
            if existing:
                existing.deleteMe()
            _palette = None
        except Exception:
            pass

        try:
            hotkey_def = ui.commandDefinitions.itemById(HOTKEY_CMD_ID)
            if hotkey_def:
                hotkey_def.deleteMe()
        except Exception:
            pass

        if command_event:
            try:
                app.unregisterCustomEvent("VoiceCADCommandEvent")
            except Exception:
                pass
            command_event = None

        try:
            if os.path.exists(TRIGGER_FILE):
                os.remove(TRIGGER_FILE)
        except Exception:
            pass

        ui.messageBox("StepByStepCAD Voice Add-in  Stopped")

    except Exception:
        ui.messageBox(traceback.format_exc())
