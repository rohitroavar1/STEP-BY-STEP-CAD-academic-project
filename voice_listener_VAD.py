#!/usr/bin/env python3
# ================================================
# voice_listener_VAD.py
# Voice-to-CAD Command Pipeline
# StepByStepCAD_VoiceAuto
#
# RUNS OUTSIDE FUSION as a standalone script.
# Whisper STT -> NLP parser -> writes command.json
# which the Fusion add-in picks up every 2 seconds.
#
# ── INSTALL DEPENDENCIES (once) ─────────────────
#   pip install openai-whisper sounddevice numpy openwakeword
#
#   NO account, NO API key, NO sign-up required.
#   openWakeWord is fully open-source (Apache 2.0).
#
# ── FIRST RUN ───────────────────────────────────
#   On the very first run, openWakeWord automatically
#   downloads the pre-trained model files (~2-5 MB each)
#   from GitHub and caches them locally.
#   Every run after that is 100% offline.
#
# ── WAKE WORDS (pre-trained, no setup needed) ───
#   Say any one of:
#     "Hey Jarvis"    "Alexa"    "Hey Mycroft"
#   Mic activates → speak your CAD command
#   Mic auto-stops when you go silent (VAD)
#
#   Fallback: RECORD button in Fusion 360 palette
#   always works regardless of wake word status.
#
# ── CAN YOU TRAIN CUSTOM WAKE WORDS? ────────────
#   YES. openWakeWord supports training your own
#   custom wake words using 100% synthetic TTS data —
#   no real voice recordings needed.
#   Training pipeline: github.com/dscripka/openWakeWord
#   Export format: ONNX (.onnx) — drop the file in
#   the add-in folder and add it to OWW_MODELS below.
#
# ── RUN ─────────────────────────────────────────
#   python voice_listener_VAD.py
#   python voice_listener_VAD.py --addin-dir "C:\Users\user\AppData\Roaming\Autodesk\Autodesk Fusion 360\API\AddIns\Stepbystep_voice_to_CAD"
#
# ── STOP ────────────────────────────────────────
#   Press Ctrl+C in the terminal.
# ================================================

import json
import os
import re
import time
import argparse

import numpy as np

_whisper_model = None


# ================================================
# CONFIG
# ================================================

SAMPLE_RATE           = 16000
RECORD_SECONDS        = 15       # hard ceiling — safety cap if silence never triggers
SILENCE_THRESHOLD     = 0.01     # RMS below this = silence
SILENCE_DURATION      = 1.2      # seconds of trailing silence required to auto-stop
SPEECH_MIN_DURATION   = 0.4      # seconds of speech required before silence check activates
CHUNK_MS              = 30       # chunk size in ms for streaming VAD loop
WHISPER_MODEL         = "small"

DEFAULT_ADDIN_DIR = os.path.dirname(os.path.realpath(__file__))
TRIGGER_FILENAME   = "mic_trigger.txt"   # written by Fusion RECORD button
MIC_STATUS_FILENAME = "mic_status.txt"  # written here, read by Fusion status_loop


# ================================================
# OPENWAKEWORD CONFIG
#
# OWW_MODELS:
#   Built-in model names recognised by openWakeWord.
#   All three are pre-trained and downloaded automatically
#   on first use — no account or API key needed.
#
#   "hey_jarvis"  → say "Hey Jarvis"
#   "alexa"       → say "Alexa"
#   "hey_mycroft" → say "Hey Mycroft"
#
# OWW_THRESHOLD:
#   Confidence score required to trigger (0.0 – 1.0).
#   0.5 is the recommended default.
#   Raise to 0.65–0.7 if you get false triggers from
#   background speech or TV audio.
#   Lower to 0.35–0.4 if it misses your voice consistently.
#
# OWW_CHUNK_SIZE:
#   openWakeWord requires exactly 1280 samples per frame
#   (80ms at 16kHz). Do not change this value.
# ================================================

OWW_MODELS     = ["hey_jarvis", "alexa", "hey_mycroft"]
OWW_THRESHOLD  = 0.5
OWW_CHUNK_SIZE = 1280   # 80ms at 16kHz — required by openWakeWord


# ================================================
# COMMAND ID COUNTER
# ================================================

def _next_id(command_file):
    try:
        if os.path.exists(command_file):
            with open(command_file, "r") as f:
                data = json.load(f)
            return int(data.get("id", 0)) + 1
    except Exception:
        pass
    return 1


# ================================================
# WRITE COMMAND JSON
# ================================================

def write_command(addin_dir, payload):
    command_file = os.path.join(addin_dir, "command.json")
    payload["id"] = _next_id(command_file)
    try:
        with open(command_file, "w") as f:
            json.dump(payload, f, indent=2)
        print("  -> Wrote id={}  cmd={}".format(payload["id"], payload["command"]))
        return True
    except Exception as e:
        print("  [ERROR] Could not write command.json: {}".format(e))
        return False


# ================================================
# WHISPER LOADER
# ================================================

def _load_whisper():
    global _whisper_model
    if _whisper_model is None:
        print("Loading Whisper '{}' model ...".format(WHISPER_MODEL))
        import whisper
        _whisper_model = whisper.load_model(WHISPER_MODEL)
        print("Whisper ready.\n")
    return _whisper_model


# ================================================
# RECORD AUDIO  —  VAD auto-stop
#
# Streams audio in CHUNK_MS chunks.
# Stops automatically when trailing silence exceeds
# SILENCE_DURATION after speech has been detected.
# Hard cap at `seconds` (default RECORD_SECONDS).
#
# Tuning guide:
#   SILENCE_DURATION    — raise to 1.8 if mic cuts off
#                         trailing words; lower to 0.8
#                         for snappier short commands
#   SPEECH_MIN_DURATION — prevents stopping before you've
#                         said anything meaningful
#   RECORD_SECONDS      — hard safety cap
#   SILENCE_THRESHOLD   — raise if mic picks up room noise
# ================================================

def record_audio(seconds=None):
    if seconds is None:
        seconds = RECORD_SECONDS
    try:
        import sounddevice as sd

        chunk_samples     = int(SAMPLE_RATE * CHUNK_MS / 1000)
        max_chunks        = int(seconds * 1000 / CHUNK_MS)
        silence_chunks    = int(SILENCE_DURATION * 1000 / CHUNK_MS)
        speech_min_chunks = int(SPEECH_MIN_DURATION * 1000 / CHUNK_MS)

        frames             = []
        speech_detected    = False
        silence_count      = 0
        speech_chunk_count = 0

        print(
            "  [MIC ON] Listening (auto-stop on silence, cap={}s) ...".format(seconds),
            flush=True,
        )

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=chunk_samples,
        )

        with stream:
            for _ in range(max_chunks):
                chunk, _ = stream.read(chunk_samples)
                chunk = chunk.flatten()
                frames.append(chunk)

                rms = float(np.sqrt(np.mean(chunk ** 2)))

                if rms >= SILENCE_THRESHOLD:
                    speech_detected    = True
                    silence_count      = 0
                    speech_chunk_count += 1
                else:
                    if speech_detected and speech_chunk_count >= speech_min_chunks:
                        silence_count += 1
                        if silence_count >= silence_chunks:
                            print("  (silence detected — stopping early)", flush=True)
                            break

        audio    = np.concatenate(frames)
        duration = len(audio) / SAMPLE_RATE
        print("  Recorded {:.1f}s.".format(duration))
        return audio

    except Exception as e:
        print("\n  [MIC ERROR] {}".format(e))
        return None


# ================================================
# TRANSCRIBE
# ================================================

def transcribe(audio):
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms < SILENCE_THRESHOLD:
        return ""
    model  = _load_whisper()
    result = model.transcribe(audio, fp16=False, language="en")
    text   = result.get("text", "").strip().lower().strip(".,!?;:")
    print("  Heard: \"{}\"".format(text))
    return text


# ================================================
# TRIGGER FILE HELPERS  (RECORD button fallback)
# ================================================

def _trigger_file_path(addin_dir):
    return os.path.join(addin_dir, TRIGGER_FILENAME)

def _trigger_exists(addin_dir):
    return os.path.exists(_trigger_file_path(addin_dir))

def _clear_trigger(addin_dir):
    try:
        path = _trigger_file_path(addin_dir)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ================================================
# MIC STATUS FILE HELPERS
#
# voice_listener_VAD.py writes mic_status.txt with
# a one-word status string.  Stepbystep_voice_to_CAD.py
# reads it every 250ms via status_loop() and sends the
# matching visual state to the palette — making the UI
# respond to wake word detection exactly as if the
# RECORD button had been clicked.
#
# Status values written:
#   "active"     → mic is now recording (palette goes green)
#   "processing" → recording finished, Whisper running
#   "ready"      → cycle complete, palette resets
# ================================================

def _write_mic_status(addin_dir, status):
    """Write a status token for the Fusion palette to pick up."""
    try:
        path = os.path.join(addin_dir, MIC_STATUS_FILENAME)
        with open(path, "w") as f:
            f.write(status)
    except Exception:
        pass


# ================================================
# OPENWAKEWORD LISTENER
#
# Runs a continuous low-CPU audio stream through
# openWakeWord's on-device ONNX engine.
# Any of the three pre-trained phrases activates
# the VAD+Whisper recording pipeline.
#
# Model files are downloaded automatically on first
# run from GitHub (~2-5 MB each) and cached locally.
# All subsequent runs are 100% offline.
#
# Trigger priority:
#   1. openWakeWord detects a wake phrase  (primary)
#   2. RECORD button in Fusion palette     (file trigger,
#                                           checked every ~80ms)
#
# Fallback if openWakeWord cannot start:
#   - not installed     → file-only polling
#   - download fails    → file-only polling
#   - any other error   → file-only polling
#
# Returns: the name of the phrase detected, or "file"
# ================================================

def _wait_for_wake_word(addin_dir):

    # ── Primary: openWakeWord multi-model detection ──────────────────
    try:
        import openwakeword
        from openwakeword.model import Model
        import sounddevice as sd

        # Download pre-trained models on first run; uses cache after that
        print("  Loading openWakeWord models ...", flush=True)
        openwakeword.utils.download_models(OWW_MODELS)

        oww = Model(
            wakeword_models  = OWW_MODELS,
            inference_framework = "onnx",
        )

        wake_display = " / ".join(
            "'{}'".format(m.replace("_", " ").title()) for m in OWW_MODELS)

        print(
            "\n[Waiting — say {} to activate mic]".format(wake_display),
            flush=True,
        )
        print(
            "  (RECORD button in Fusion palette also works as backup)",
            flush=True,
        )

        with sd.InputStream(
            samplerate = SAMPLE_RATE,
            channels   = 1,
            dtype      = "int16",
            blocksize  = OWW_CHUNK_SIZE,
        ) as stream:

            while True:

                # Check RECORD button fallback every frame (~80ms)
                if _trigger_exists(addin_dir):
                    _clear_trigger(addin_dir)
                    # NOTE: RECORD button already turns palette green via JS.
                    # We still write "active" so status_loop stays in sync.
                    _write_mic_status(addin_dir, "active")
                    print("  [RECORD button — mic activating]", flush=True)
                    return "file"

                # Read one 80ms frame and run wake word inference
                frame, _ = stream.read(OWW_CHUNK_SIZE)
                prediction = oww.predict(frame.flatten())

                # prediction is a dict: {"hey_jarvis": 0.0-1.0, "alexa": ...}
                for model_name, score in prediction.items():
                    if score >= OWW_THRESHOLD:
                        # Reset all scores to prevent immediate re-trigger
                        oww.reset()
                        display = model_name.replace("_", " ").title()
                        # Signal Fusion palette to go green — same visual
                        # as clicking the RECORD button manually
                        _write_mic_status(addin_dir, "active")
                        print(
                            "  [Wake word '{}' detected (score={:.2f}) "
                            "— mic activating]".format(display, score),
                            flush=True,
                        )
                        return model_name

    except ImportError:
        print(
            "\n  [WARNING] openwakeword is not installed.\n"
            "  Install with:  pip install openwakeword\n"
            "  Falling back to RECORD button only.\n",
            flush=True,
        )

    except Exception as e:
        print(
            "\n  [WARNING] openWakeWord error: {}\n"
            "  Falling back to RECORD button only.\n".format(e),
            flush=True,
        )

    # ── Fallback: file-only polling (RECORD button) ──────────────────
    print("\n[Waiting — click RECORD in the Fusion palette]", flush=True)
    while True:
        if _trigger_exists(addin_dir):
            _clear_trigger(addin_dir)
            _write_mic_status(addin_dir, "active")
            print("  [RECORD button pressed]", flush=True)
            return "file"
        time.sleep(0.1)


# ================================================
# NLP HELPERS
# ================================================

def _num(text, pattern, default=None):
    m = re.search(pattern, text)
    if m:
        return float(m.group(1))
    return default

def _all_nums(text):
    return [float(n) for n in re.findall(r"[-]?\d+(?:\.\d+)?", text)]

def _extract_edges(text):
    explicit = re.findall(r"\bedge[s]?\s+(\d+)", text)
    if explicit:
        return [int(n) for n in explicit]
    cleaned = re.sub(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|deg|degrees|degree)", "", text)
    cleaned = re.sub(r"(?:radius|distance|size|by|x)\s+\d+(?:\.\d+)?", "", cleaned)
    fallback = [int(n) for n in re.findall(r"\b(\d+)\b", cleaned) if int(n) < 50]
    return fallback if fallback else [0]


# ================================================
# NLP PARSERS
# ================================================

def _parse_undo(text):
    if any(w in text for w in ("undo", "go back", "revert")):
        return {"command": "undo"}
    return None

def _parse_select_edge(text):
    if any(k in text for k in ["select edge", "which edge", "edge index", "identify edge"]):
        return {"command": "select_edge"}
    return None

def _parse_select_face(text):
    if any(k in text for k in ["select face", "which face", "identify face"]):
        return {"command": "select_face"}
    return None

def _parse_select_profile(text):
    if any(k in text for k in ["select profile", "which profile", "profile index"]):
        return {"command": "select_profile"}
    return None

def _parse_show_faces(text):
    if any(k in text for k in ["show faces", "list faces", "show all faces", "face numbers"]):
        return {"command": "show_faces"}
    return None

def _parse_show_profiles(text):
    if any(k in text for k in ["show profiles", "list profiles", "profile numbers"]):
        return {"command": "show_profiles"}
    return None

def _parse_show_bodies(text):
    # MULTI-01: filler-tolerant like show_curves — matches "show bodies",
    # "show me the bodies", "list all bodies", "how many bodies" etc.
    if not re.search(r"\bbodies\b", text):
        return None
    if re.search(r"\b(show|list|how many|what)\b", text):
        return {"command": "show_bodies"}
    return None

def _parse_select_body(text):
    if "select body" not in text and "use body" not in text:
        return None
    m = re.search(r"body\s+\D*?(\d+)", text)
    if not m:
        return None
    return {"command": "select_body", "body_index": int(m.group(1))}

def _parse_show_features(text):
    # MULTI-03: filler-tolerant, same pattern as show_bodies/show_curves.
    if not re.search(r"\bfeatures?\b", text):
        return None
    if re.search(r"\b(show|list|how many|what)\b", text):
        return {"command": "show_features"}
    return None

def _parse_select_feature(text):
    if "select feature" not in text and "use feature" not in text:
        return None
    m = re.search(r"feature\s+\D*?(\d+)", text)
    if not m:
        return None
    return {"command": "select_feature", "feature_index": int(m.group(1))}

def _parse_read_point(text):
    if any(k in text for k in ["read point", "what are the coordinates",
                                "coordinates of this point", "get coordinates",
                                "point coordinates", "read coordinates"]):
        return {"command": "read_point"}
    return None

def _parse_body_visibility(text):
    if any(w in text for w in ("hide body", "hide the body")):
        return {"command": "body_visibility", "action": "hide"}
    if any(w in text for w in ("show body", "unhide body", "reveal body")):
        return {"command": "body_visibility", "action": "show"}
    return None

def _parse_finish_sketch(text):
    if any(k in text for k in ["finish sketch", "close sketch", "done sketch",
                                "end sketch", "complete sketch", "stop sketch"]):
        return {"command": "finish_sketch"}
    return None

def _parse_mirror_sketch(text):
    if "mirror sketch" in text or ("mirror" in text and "sketch" in text):
        return {"command": "sketch_mirror"}
    return None

def _parse_offset_plane(text):
    if not any(k in text for k in ["offset plane", "create plane",
                                    "construction plane", "new plane"]):
        return None
    if "sketch" in text:
        return None
    offset = _num(text, r"(\d+(?:\.\d+)?)\s*(?:mm)?", 10)
    return {"command": "create_offset_plane", "offset": offset}

def _parse_create_sketch(text):
    if not any(k in text for k in ["create sketch", "new sketch", "start sketch",
                                    "make sketch", "draw on", "sketch on"]):
        return None
    if "face index" in text or "face number" in text:
        idx = int(_num(text, r"(\d+)", 0))
        return {"command": "create_sketch", "plane": "face_index", "face_index": idx}
    if "offset" in text:
        return {"command": "create_sketch", "plane": "offset"}
    face_checks = [
        ("top face","top"),("bottom face","bottom"),("front face","front"),
        ("back face","back"),("left face","left"),("right face","right"),
        ("top","top"),("bottom","bottom"),("front","front"),
        ("back","back"),("left","left"),("right","right"),
    ]
    for phrase, face_type in face_checks:
        if phrase in text:
            return {"command": "create_sketch", "plane": "face", "face_type": face_type}
    if "y z" in text or "yz" in text:
        return {"command": "create_sketch", "plane": "yz"}
    if "x z" in text or "xz" in text:
        return {"command": "create_sketch", "plane": "xz"}
    return {"command": "create_sketch", "plane": "xy"}

def _parse_circle(text):
    if "circle" not in text:
        return None
    diam_m = re.search(r"diameter\s+([-]?\d+(?:\.\d+)?)", text)
    rad_m  = re.search(r"radius\s+([-]?\d+(?:\.\d+)?)", text)
    at_m   = re.search(r"at\s+([-]?\d+(?:\.\d+)?)\s+([-]?\d+(?:\.\d+)?)", text)
    cx_m   = re.search(r"cx\s+([-]?\d+(?:\.\d+)?)", text)
    cy_m   = re.search(r"cy\s+([-]?\d+(?:\.\d+)?)", text)

    stripped = text
    for pat in [r"diameter\s+[-]?\d+(?:\.\d+)?", r"radius\s+[-]?\d+(?:\.\d+)?",
                r"at\s+[-]?\d+(?:\.\d+)?\s+[-]?\d+(?:\.\d+)?",
                r"cx\s+[-]?\d+(?:\.\d+)?", r"cy\s+[-]?\d+(?:\.\d+)?", r"circle"]:
        stripped = re.sub(pat, "", stripped)
    pos_nums = _all_nums(stripped)

    if diam_m:
        diameter = float(diam_m.group(1))
    elif rad_m:
        diameter = float(rad_m.group(1)) * 2
    else:
        diameter = pos_nums[2] if len(pos_nums) > 2 else (
                   pos_nums[0] if len(pos_nums) > 0 else 20)

    if cx_m:   cx = float(cx_m.group(1))
    elif at_m: cx = float(at_m.group(1))
    else:      cx = pos_nums[0] if len(pos_nums) > 0 else 0

    if cy_m:   cy = float(cy_m.group(1))
    elif at_m: cy = float(at_m.group(2))
    else:      cy = pos_nums[1] if len(pos_nums) > 1 else 0

    return {"command": "circle", "cx": cx, "cy": cy, "diameter": diameter}

def _parse_polygon(text):
    if not any(k in text for k in ["polygon","hexagon","pentagon","octagon","triangle"]):
        return None

    sides_map = {"triangle": 3, "hexagon": 6, "pentagon": 5, "octagon": 8}
    sides = 6
    for word, s in sides_map.items():
        if word in text:
            sides = s
            break
    m = re.search(r"(\d+)\s*sides?", text)
    if m:
        sides = int(m.group(1))
    m2 = re.search(r"sides?\s+(\d+)", text)
    if m2:
        sides = int(m2.group(1))

    radius_m = re.search(r"radius\s+([-]?\d+(?:\.\d+)?)", text)
    at_m     = re.search(r"at\s+([-]?\d+(?:\.\d+)?)\s+([-]?\d+(?:\.\d+)?)", text)
    cx_m     = re.search(r"cx\s+([-]?\d+(?:\.\d+)?)", text)
    cy_m     = re.search(r"cy\s+([-]?\d+(?:\.\d+)?)", text)

    stripped = text
    for pat in [r"radius\s+[-]?\d+(?:\.\d+)?", r"sides?\s+\d+", r"\d+\s*sides?",
                r"at\s+[-]?\d+(?:\.\d+)?\s+[-]?\d+(?:\.\d+)?",
                r"cx\s+[-]?\d+(?:\.\d+)?", r"cy\s+[-]?\d+(?:\.\d+)?",
                r"polygon", r"hexagon", r"pentagon", r"octagon", r"triangle"]:
        stripped = re.sub(pat, "", stripped)
    pos_nums = _all_nums(stripped)

    if cx_m:   cx = float(cx_m.group(1))
    elif at_m: cx = float(at_m.group(1))
    else:      cx = pos_nums[0] if len(pos_nums) > 0 else 0

    if cy_m:   cy = float(cy_m.group(1))
    elif at_m: cy = float(at_m.group(2))
    else:      cy = pos_nums[1] if len(pos_nums) > 1 else 0

    if radius_m: radius = float(radius_m.group(1))
    else:        radius = pos_nums[2] if len(pos_nums) > 2 else 20

    return {"command": "polygon", "cx": cx, "cy": cy, "sides": sides, "radius": radius}

def _parse_rectangle(text):
    has_rect = bool(re.search(r'\brect\b', text)) or "rectangle" in text or "square" in text
    if not has_rect:
        return None

    if any(w in text for w in ("center","centred","centre")):
        length_m = re.search(r"length\s+([-]?\d+(?:\.\d+)?)", text)
        width_m  = re.search(r"width\s+([-]?\d+(?:\.\d+)?)", text)
        at_m     = re.search(r"at\s+([-]?\d+(?:\.\d+)?)\s+([-]?\d+(?:\.\d+)?)", text)
        cx_m     = re.search(r"cx\s+([-]?\d+(?:\.\d+)?)", text)
        cy_m     = re.search(r"cy\s+([-]?\d+(?:\.\d+)?)", text)

        stripped = text
        for pattern in [r"length\s+[-]?\d+(?:\.\d+)?", r"width\s+[-]?\d+(?:\.\d+)?",
                        r"at\s+[-]?\d+(?:\.\d+)?\s+[-]?\d+(?:\.\d+)?",
                        r"cx\s+[-]?\d+(?:\.\d+)?", r"cy\s+[-]?\d+(?:\.\d+)?",
                        r"center", r"centred", r"centre", r"rectangle", r"rect"]:
            stripped = re.sub(pattern, "", stripped)
        pos_nums = _all_nums(stripped)

        _pos_has_anchor = at_m or cx_m or cy_m or len(pos_nums) > 2
        if cx_m:               cx = float(cx_m.group(1))
        elif at_m:             cx = float(at_m.group(1))
        elif _pos_has_anchor:  cx = pos_nums[0] if len(pos_nums) > 0 else 0
        else:                  cx = 0

        if cy_m:               cy = float(cy_m.group(1))
        elif at_m:             cy = float(at_m.group(2))
        elif _pos_has_anchor:  cy = pos_nums[1] if len(pos_nums) > 1 else 0
        else:                  cy = 0

        if length_m:
            length = float(length_m.group(1))
        else:
            if len(pos_nums) == 2 and not at_m and not cx_m:
                length = pos_nums[0]
            else:
                length = pos_nums[2] if len(pos_nums) > 2 else (
                         pos_nums[0] if len(pos_nums) > 0 else 50)

        if width_m:
            width = float(width_m.group(1))
        else:
            if len(pos_nums) == 2 and not at_m and not cy_m:
                width = pos_nums[1]
            else:
                width = pos_nums[3] if len(pos_nums) > 3 else (
                        pos_nums[1] if len(pos_nums) > 1 else 30)

        return {"command": "rectangle_center", "cx": cx, "cy": cy,
                "length": length, "width": width}

    x1_m   = re.search(r"x1\s+([-]?\d+(?:\.\d+)?)", text)
    y1_m   = re.search(r"y1\s+([-]?\d+(?:\.\d+)?)", text)
    x2_m   = re.search(r"x2\s+([-]?\d+(?:\.\d+)?)", text)
    y2_m   = re.search(r"y2\s+([-]?\d+(?:\.\d+)?)", text)
    from_m = re.search(r"from\s+([-]?\d+(?:\.\d+)?)\s+([-]?\d+(?:\.\d+)?)", text)
    to_m   = re.search(r"to\s+([-]?\d+(?:\.\d+)?)\s+([-]?\d+(?:\.\d+)?)", text)
    nums   = _all_nums(text)

    if x1_m:     x1 = float(x1_m.group(1))
    elif from_m: x1 = float(from_m.group(1))
    else:        x1 = nums[0] if len(nums) > 0 else 0

    if y1_m:     y1 = float(y1_m.group(1))
    elif from_m: y1 = float(from_m.group(2))
    else:        y1 = nums[1] if len(nums) > 1 else 0

    if x2_m:   x2 = float(x2_m.group(1))
    elif to_m: x2 = float(to_m.group(1))
    else:      x2 = nums[2] if len(nums) > 2 else 50

    if y2_m:   y2 = float(y2_m.group(1))
    elif to_m: y2 = float(to_m.group(2))
    else:      y2 = nums[3] if len(nums) > 3 else 30

    return {"command": "rectangle_2point", "x1": x1, "y1": y1, "x2": x2, "y2": y2}

def _parse_line(text):
    if not any(k in text for k in ["draw line","add line","line from","sketch line"]):
        return None
    nums = _all_nums(text)
    x1 = nums[0] if len(nums) > 0 else 0
    y1 = nums[1] if len(nums) > 1 else 0
    x2 = nums[2] if len(nums) > 2 else 50
    y2 = nums[3] if len(nums) > 3 else 50
    return {"command": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2}

def _parse_show_curves(text):
    # TR-03 FIX: match on the presence of "curve(s)" plus a listing verb
    # anywhere in the phrase, rather than a rigid substring like "show
    # curves". Tolerates natural filler ("show me the curves", "list
    # the curves", "what curves are there").
    if not re.search(r"\bcurves?\b", text):
        return None
    if re.search(r"\b(show|list|what)\b", text):
        return {"command": "show_curves"}
    return None

def _parse_trim(text):
    # "trim major arc circle 1" / "trim the minor arc on circle zero" —
    # fully voice-driven, no click needed. arc_m and idx_m are matched
    # independently (rather than one rigid phrase-order regex) so filler
    # words between "arc"/"circle"/the index don't break the match.
    # Must be checked before the generic click-then-speak trim below.
    if "trim" in text and "arc" in text and "circle" in text:
        arc_m = re.search(r"\b(major|minor)\b", text)
        idx_m = re.search(r"circle\s+\D*?(\d+)", text)
        if arc_m and idx_m:
            return {
                "command": "trim_arc",
                "arc_side": arc_m.group(1),
                "curve_index": int(idx_m.group(1)),
            }
        # major/minor + circle present but index unclear -> fall through
        # to generic trim below, which will prompt for a click instead.

    # Generic trim: click a line/arc segment in the viewport first, then say
    # any of these. Uses the exact click point, not a guessed midpoint.
    if any(k in text for k in ["trim", "trim curve", "trim line",
                                "trim segment", "trim here", "trim this",
                                "cut curve"]):
        return {"command": "trim"}
    return None

def _parse_restore_circle(text):
    # TR-06: "restore circle 1" / "restore arc 1" — converts a previously
    # trimmed arc back into a full circle. Deliberately requires the word
    # "restore" so it never fires on a plain "circle ..." draw command.
    if "restore" not in text:
        return None
    m = re.search(r"(?:circle|arc)\s+\D*?(\d+)", text)
    if not m:
        return None
    return {"command": "restore_circle", "curve_index": int(m.group(1))}

def _parse_constraint(text):
    # Fully voice-driven sketch constraints. Curve indices come from
    # "show curves" — no click required.
    nums = [int(n) for n in re.findall(r"\d+", text)]

    if "tangent" in text and len(nums) >= 2:
        return {"command": "add_constraint", "constraint_type": "tangent",
                "index_1": nums[0], "index_2": nums[1]}

    if "perpendicular" in text and len(nums) >= 2:
        return {"command": "add_constraint", "constraint_type": "perpendicular",
                "index_1": nums[0], "index_2": nums[1]}

    if "parallel" in text and len(nums) >= 2:
        return {"command": "add_constraint", "constraint_type": "parallel",
                "index_1": nums[0], "index_2": nums[1]}

    if "symmetric" in text and len(nums) >= 3:
        return {"command": "add_constraint", "constraint_type": "symmetric",
                "index_1": nums[0], "index_2": nums[1], "index_3": nums[2]}

    return None

def _parse_extrude_ring(text):
    # RING-01: "extrude ring circle 1 circle 2 distance 10 new body" —
    # extrudes the annular area between two concentric circles, excluding
    # the inner circle's area entirely. Must run before _parse_extrude
    # (shares the "extrude" keyword) and before _parse_circle (shares
    # "circle") — same ordering rule as trim/restore_circle above.
    if "ring" not in text:
        return None
    circle_indices = re.findall(r"circle\s+\D*?(\d+)", text)
    if len(circle_indices) < 2:
        return None
    inner_idx = int(circle_indices[0])
    outer_idx = int(circle_indices[1])

    # Distance: any number in the sentence that ISN'T one of the two
    # circle-index numbers just consumed above.
    stripped = re.sub(r"circle\s+\D*?\d+", "", text)
    remaining_nums = _all_nums(stripped)
    distance = remaining_nums[0] if remaining_nums else 10

    op = "new_body"
    if any(w in text for w in ("join", "add to", "boss", "merge")):
        op = "join"
    elif any(w in text for w in ("cut", "remove", "subtract", "pocket")):
        op = "cut"

    return {
        "command": "extrude_ring",
        "inner_curve_index": inner_idx,
        "outer_curve_index": outer_idx,
        "distance": distance,
        "operation": op,
    }

def _parse_extrude(text):
    if not any(k in text for k in ["extrude","pull","push out","add height","give height"]):
        return None
    if "extrude face" in text or "face extrude" in text:
        fi       = re.search(r"(?:index|face)\s+(\d+)", text)
        face_idx = int(fi.group(1)) if fi else None
        all_n    = _all_nums(text)
        remaining = [n for n in all_n if face_idx is None or int(n) != face_idx]
        distance = remaining[0] if remaining else 10
        op = "join"
        if any(w in text for w in ("new body","new_body")): op = "new_body"
        elif any(w in text for w in ("cut","remove","subtract","pocket")): op = "cut"
        cmd = {"command": "extrude_face", "distance": distance, "operation": op}
        if face_idx is not None:
            cmd["face_index"] = face_idx
        return cmd
    distance = _num(text, r"(\d+(?:\.\d+)?)\s*(?:mm)?", 10)
    op = "new_body"
    if any(w in text for w in ("join","add to","boss","merge")): op = "join"
    elif any(w in text for w in ("cut","remove","subtract","pocket")): op = "cut"
    extent = "one_side"
    if any(w in text for w in ("symmetric","both sides","both directions","symmetrical")):
        extent = "symmetric"
    cmd = {"command": "extrude", "distance": distance, "operation": op, "extent": extent}
    m = re.search(r"profile\s+(\d+)", text)
    if m:
        cmd["profile_index"] = int(m.group(1))
    return cmd

def _parse_revolve(text):
    if not any(k in text for k in ["revolve","rotate profile","spin profile"]):
        return None
    angle_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:deg|degree|degrees)", text)
    if angle_m:
        angle = float(angle_m.group(1))
    else:
        # VL-05 FIX: strip "axis N" before picking up angle so index isn't
        # mistakenly used as the angle value.
        stripped = re.sub(r"axis\s+\d+", "", text)
        nums  = _all_nums(stripped)
        angle = nums[0] if nums else 360
    axis_index = int(_num(text, r"axis\s+(\d+)", 0))
    # SE-03 FIX: emit operation so solid_engine revolve() can cut/join/new_body.
    op = "new_body"
    if any(w in text for w in ("cut", "remove", "subtract", "pocket")):
        op = "cut"
    elif any(w in text for w in ("join", "merge", "add to", "boss")):
        op = "join"
    return {"command": "revolve", "angle": angle, "axis_line_index": axis_index,
            "operation": op}

def _parse_hole(text):
    if not any(k in text for k in ["drill","drill hole","add hole","make hole",
                                    "create hole","create a hole","bore hole","cut a hole"]):
        return None
    diam_m  = re.search(r"diameter\s+([-]?\d+(?:\.\d+)?)", text)
    depth_m = re.search(r"depth\s+([-]?\d+(?:\.\d+)?)", text)
    at_m    = re.search(r"at\s+([-]?\d+(?:\.\d+)?)\s+([-]?\d+(?:\.\d+)?)", text)
    face_m  = re.search(r"face\s+(\d+)", text)

    stripped = text
    for pat in [r"diameter\s+[-]?\d+(?:\.\d+)?", r"depth\s+[-]?\d+(?:\.\d+)?",
                r"at\s+[-]?\d+(?:\.\d+)?\s+[-]?\d+(?:\.\d+)?",
                r"face\s+\d+", r"drill\s+hole", r"drill", r"hole",
                r"add\s+hole", r"make\s+hole", r"create\s+a\s+hole",
                r"create\s+hole", r"bore\s+hole"]:
        stripped = re.sub(pat, "", stripped)
    pos_nums = _all_nums(stripped)

    if at_m:
        x = float(at_m.group(1))
        y = float(at_m.group(2))
    else:
        x = pos_nums[0] if len(pos_nums) > 0 else 0
        y = pos_nums[1] if len(pos_nums) > 1 else 0

    diameter = float(diam_m.group(1)) if diam_m else (
               pos_nums[2] if len(pos_nums) > 2 else 10)
    depth    = float(depth_m.group(1)) if depth_m else (
               pos_nums[3] if len(pos_nums) > 3 else 15)

    cmd = {"command": "hole", "x": x, "y": y, "diameter": diameter, "depth": depth}
    if face_m:
        cmd["face_index"] = int(face_m.group(1))
    return cmd

def _parse_thread(text):
    if not any(k in text for k in ["add thread","apply thread","screw thread",
                                    "create thread","thread the"]):
        return None
    is_internal = any(w in text for w in ("internal","inside","nut","tapped"))
    m           = re.search(r"(m\d+(?:x[\d.]+)?)", text)
    designation = m.group(1).upper() if m else "M10x1.5"
    handedness  = "left" if "left" in text else "right"
    return {"command": "thread_internal" if is_internal else "thread_external",
            "designation": designation, "handedness": handedness}

def _parse_fillet(text):
    if not any(k in text for k in ["fillet","round edge","round corner","round the edge"]):
        return None
    radius = (_num(text, r"radius\s+(\d+(?:\.\d+)?)", None) or
              _num(text, r"(\d+(?:\.\d+)?)\s*mm", None) or 2.0)
    edges = _extract_edges(text)
    print("  [fillet] radius={}  edges={}".format(radius, edges))
    return {"command": "fillet", "edges": edges, "radius": radius}

def _parse_chamfer(text):
    if not any(k in text for k in ["chamfer","bevel","bevel edge","bevel the edge"]):
        return None
    distance = (_num(text, r"distance\s+(\d+(?:\.\d+)?)", None) or
                _num(text, r"(\d+(?:\.\d+)?)\s*mm", None) or 2.0)
    edges = _extract_edges(text)
    print("  [chamfer] distance={}  edges={}".format(distance, edges))
    return {"command": "chamfer", "edges": edges, "distance": distance}

def _parse_mirror_body(text):
    if not any(k in text for k in ["mirror body","mirror the body"]):
        return None
    plane = "xy"
    for p in ["yz","xz","xy"]:
        if p in text: plane = p; break
    return {"command": "mirror", "plane": plane}

def _extract_feature_index(text):
    """Shared helper: pulls an explicit 'feature N' reference out of a
    pattern/mirror command, e.g. 'circular pattern 6 copies feature 2'
    lets that command target feature 2 directly in one shot instead of
    requiring a separate select_feature first."""
    m = re.search(r"feature\s+\D*?(\d+)", text)
    return int(m.group(1)) if m else None

def _parse_mirror_feature(text):
    if not any(k in text for k in ["mirror feature","mirror extrude","mirror hole",
                                    "mirror the feature","mirror last feature"]):
        return None
    plane = "xy"
    for p in ["yz","xz","xy"]:
        if p in text: plane = p; break
    cmd = {"command": "mirror_feature", "plane": plane}
    feat_idx = _extract_feature_index(text)
    if feat_idx is not None:
        cmd["feature_index"] = feat_idx
    return cmd

def _parse_rectangular_pattern(text):
    if not any(k in text for k in ["rectangular pattern","linear pattern","pattern along",
                                    "repeat in a row","pattern in x","pattern in y"]):
        return None
    feat_idx = _extract_feature_index(text)
    # Strip "feature N" BEFORE positional number extraction below —
    # count_x/spacing_x/count_y/spacing_y are read purely by position,
    # so leaving that digit in would shift every subsequent value.
    stripped  = re.sub(r"feature\s+\D*?\d+", "", text)
    nums      = _all_nums(stripped)
    count_x   = int(nums[0]) if len(nums) > 0 else 2
    spacing_x = nums[1]      if len(nums) > 1 else 20
    count_y   = int(nums[2]) if len(nums) > 2 else 1
    spacing_y = nums[3]      if len(nums) > 3 else 20
    cmd = {"command": "rectangular_pattern",
           "count_x": count_x, "spacing_x": spacing_x,
           "count_y": count_y, "spacing_y": spacing_y}
    if feat_idx is not None:
        cmd["feature_index"] = feat_idx
    return cmd

def _parse_circular_pattern(text):
    if not any(k in text for k in ["circular pattern","radial pattern",
                                    "pattern around","rotate pattern"]):
        return None
    feat_idx = _extract_feature_index(text)
    stripped = re.sub(r"feature\s+\D*?\d+", "", text)
    count_m = re.search(r"(\d+)\s+(?:copies|times|instances|count)", stripped)
    count   = int(count_m.group(1)) if count_m else 4
    angle_m = re.search(r"(\d+(?:\.\d+)?)\s*deg", stripped)
    angle   = float(angle_m.group(1)) if angle_m else 360
    axis = "z"
    for a in ["x axis","y axis","z axis"]:
        if a in text: axis = a[0]; break
    cmd = {"command": "circular_pattern", "count": count, "angle": angle, "axis": axis}
    if feat_idx is not None:
        cmd["feature_index"] = feat_idx
    return cmd

def _parse_repeat_feature(text):
    if not any(k in text for k in ["repeat feature","repeat last","repeat extrude"]):
        return None
    nums      = _all_nums(text)
    count     = int(nums[0]) if len(nums) > 0 else 2
    spacing   = nums[1]      if len(nums) > 1 else 10
    direction = "y" if "y direction" in text or "along y" in text else \
                "both" if "both" in text else "x"
    return {"command": "repeat_feature", "count": count,
            "spacing": spacing, "direction": direction}


# ================================================
# PARSER REGISTRY
# ================================================

PARSERS = [
    _parse_undo,
    _parse_select_edge,
    _parse_select_face,
    _parse_select_profile,
    _parse_show_faces,
    _parse_show_profiles,
    _parse_show_curves,
    _parse_read_point,
    _parse_show_bodies,
    _parse_select_body,
    _parse_show_features,
    _parse_select_feature,
    _parse_body_visibility,
    _parse_finish_sketch,
    _parse_mirror_sketch,
    _parse_offset_plane,
    _parse_create_sketch,
    _parse_trim,           # must run before _parse_circle: "trim ... circle N"
    _parse_restore_circle, # must run before _parse_circle: "restore circle N"
    _parse_extrude_ring,   # must run before _parse_extrude AND _parse_circle
    _parse_constraint,     # must run before _parse_circle/_parse_rectangle etc.
    _parse_circle,
    _parse_polygon,
    _parse_rectangle,
    _parse_line,
    _parse_extrude,
    _parse_revolve,
    _parse_hole,
    _parse_thread,
    _parse_fillet,
    _parse_chamfer,
    _parse_mirror_feature,
    _parse_mirror_body,
    _parse_rectangular_pattern,
    _parse_circular_pattern,
    _parse_repeat_feature,
]


# ================================================
# NUMBER-WORD CONVERSION
#
# TR-02 FIX: Whisper's small model frequently outputs small
# index numbers ("zero", "one", "two"...) as words instead of
# digits, especially in short phrases like "line zero" or
# "circle one" — unlike longer dimension phrases ("sixty six
# millimetres") where digit output is far more reliable.
# This matters most for the new trim/constraint commands,
# which key off curve indices 0-20. Converted BEFORE any
# regex \d+ extraction runs.
# ================================================

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20",
}

def _words_to_digits(text):
    for word, digit in _NUMBER_WORDS.items():
        text = re.sub(r'\b{}\b'.format(word), digit, text)
    return text


# ================================================
# NORMALIZER
# Runs before any parser.
#
# 1. "minus N" / "negative N" -> "-N"
# 2. "33 point 5"             -> "33.5"
# 3. "comma"                  -> " "
# 4. "breadth"                -> "width"
# 5. VL-03: "with N"          -> "width N"
#    (Whisper mishears "width 30" as "with 30")
# 6. TR-02: word numbers      -> digits ("zero" -> "0")
# ================================================

def _normalize(text):
    text = _words_to_digits(text)
    text = re.sub(r'\b(?:minus|negative)\s+([\d]+(?:\.\d+)?)', r'-\1', text)
    text = re.sub(r'(\d+)\s+point\s+(\d+)', r'\1.\2', text)
    text = re.sub(r'\bcomma\b', ' ', text)
    text = re.sub(r'\bbreadth\b', 'width', text)
    text = re.sub(r'\bwith\s+([-]?\d)', r'width \1', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_text(text):
    text = text.lower().strip()
    if not text:
        return None
    text = _normalize(text)
    for parser in PARSERS:
        try:
            result = parser(text)
            if result:
                return result
        except Exception as e:
            print("  [parser error in {}]: {}".format(parser.__name__, e))
    return None


# ================================================
# MAIN LOOP
# ================================================

def main():
    global WHISPER_MODEL, RECORD_SECONDS, SILENCE_DURATION, SILENCE_THRESHOLD
    global OWW_THRESHOLD

    arg_parser = argparse.ArgumentParser(
        description="StepByStepCAD Voice Listener — openWakeWord edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Wake words: 'Hey Jarvis' / 'Alexa' / 'Hey Mycroft'\n\n"
            "Examples:\n"
            "  python voice_listener_VAD.py\n"
            "  python voice_listener_VAD.py --addin-dir "
            "\"C:/Users/user/AppData/Roaming/Autodesk/"
            "Autodesk Fusion 360/API/AddIns/Stepbystep_voice_to_CAD\"\n"
            "  python voice_listener_VAD.py --threshold 0.65"
        ),
    )
    arg_parser.add_argument(
        "--addin-dir",
        default=DEFAULT_ADDIN_DIR,
        help="Full path to the Fusion add-in folder (default: this file's directory)",
    )
    arg_parser.add_argument(
        "--threshold",
        type=float,
        default=OWW_THRESHOLD,
        help="openWakeWord confidence threshold 0.0-1.0 (default: {})".format(OWW_THRESHOLD),
    )
    arg_parser.add_argument(
        "--max-seconds",
        type=int,
        default=RECORD_SECONDS,
        help="Hard cap on recording duration in seconds (default: {})".format(RECORD_SECONDS),
    )
    arg_parser.add_argument(
        "--silence",
        type=float,
        default=SILENCE_DURATION,
        help="Seconds of trailing silence before auto-stop (default: {})".format(
            SILENCE_DURATION),
    )
    arg_parser.add_argument(
        "--rms-threshold",
        type=float,
        default=SILENCE_THRESHOLD,
        help="RMS silence threshold 0.0-1.0 (default: {})".format(SILENCE_THRESHOLD),
    )
    arg_parser.add_argument(
        "--model",
        default=WHISPER_MODEL,
        choices=["tiny","base","small","medium","large"],
        help="Whisper model size (default: {})".format(WHISPER_MODEL),
    )
    args = arg_parser.parse_args()

    WHISPER_MODEL     = args.model
    RECORD_SECONDS    = args.max_seconds
    SILENCE_DURATION  = args.silence
    SILENCE_THRESHOLD = args.rms_threshold
    OWW_THRESHOLD     = args.threshold

    addin_dir = os.path.realpath(args.addin_dir)

    print("=" * 60)
    print("  StepByStepCAD Voice Listener")
    print("  Add-in dir : {}".format(addin_dir))
    print("  Wake words : 'Hey Jarvis' / 'Alexa' / 'Hey Mycroft'")
    print("  Threshold  : {}  (openWakeWord confidence)".format(OWW_THRESHOLD))
    print("  Fallback   : RECORD button in Fusion palette")
    print("  Model      : {}  (Whisper)".format(WHISPER_MODEL))
    print("  VAD        : auto-stop after {:.1f}s silence".format(SILENCE_DURATION))
    print("  Hard cap   : {}s max per command".format(RECORD_SECONDS))
    print("  Stop       : Ctrl+C")
    print("=" * 60)

    if not os.path.isdir(addin_dir):
        print("\n  [ERROR] Add-in directory not found:")
        print("  {}".format(addin_dir))
        print("  Pass the correct path with --addin-dir")
        return

    # Clear any stale trigger file from previous session
    trigger_path = os.path.join(addin_dir, TRIGGER_FILENAME)
    if os.path.exists(trigger_path):
        os.remove(trigger_path)
        print("  (cleared stale trigger file from previous session)")

    _load_whisper()
    print("\n  Ready. Say 'Hey Jarvis', 'Alexa', or 'Hey Mycroft' to activate.")
    print("  RECORD button in Fusion palette also works as backup.\n")

    try:
        while True:
            # Block until wake word or RECORD button
            # (_wait_for_wake_word writes "active" → palette goes green)
            _wait_for_wake_word(addin_dir)

            # Record with VAD auto-stop
            audio = record_audio(RECORD_SECONDS)

            # Recording finished → palette leaves green, shows "Processing..."
            _write_mic_status(addin_dir, "processing")

            if audio is None:
                print("  Mic error — try again.")
                _write_mic_status(addin_dir, "ready")
                continue

            # Transcribe
            text = transcribe(audio)
            if not text:
                print("  (silence or noise — nothing sent)")
                _write_mic_status(addin_dir, "ready")
                continue

            # Stop check
            if any(w in text for w in ("quit","exit","stop listening")):
                print("  Stopping. Goodbye.")
                _write_mic_status(addin_dir, "ready")
                break

            # Parse and write command
            command = parse_text(text)
            if command is None:
                print("  Not recognised: \"{}\"".format(text))
                print("  Examples: 'create sketch on top face' / "
                      "'extrude 15 new body' / 'finish sketch'")
                _write_mic_status(addin_dir, "ready")
                continue

            write_command(addin_dir, command)
            # "ready" is written after command.json is confirmed written
            # so the palette resets only after dispatch is guaranteed
            _write_mic_status(addin_dir, "ready")

    except KeyboardInterrupt:
        print("\nStopped by Ctrl+C.")


if __name__ == "__main__":
    main()
