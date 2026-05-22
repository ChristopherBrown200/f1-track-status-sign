"""
F1 Sign Middleware Raspberry Pi Server
By: Christopher Brown (https://github.com/ChristopherBrown200)

Connected to the F1 live timing stream via FastF1: https://github.com/theOehrly/Fast-F1

Extracts track status, checks whether a session is currently active,
detects session end and winner color, and serves it to an ESP32.
"""

import ast
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import fastf1
from flask import Flask, jsonify
from fastf1.livetiming.client import SignalRClient

# == Config =======================================================================================
BASE_DIR                = Path(__file__).parent
OUTPUT_FILE             = BASE_DIR / 'f1_stream.txt'
WINNER_STATE_FILE       = BASE_DIR / 'winner_state.json'
CACHE_DIR               = BASE_DIR / 'cache'
FLASK_PORT              = 5000
SESSION_DURATION        = 4
SESSION_BUFFER          = 30
SCHEDULE_REFRESH        = 3600
RECONNECT_DELAY         = 15
WINNER_DISPLAY_MINS     = 30
WINNER_SESSION_TYPES    = ['Race', 'Sprint', 'Qualifying']

# == FastF1 cache =================================================================================
CACHE_DIR.mkdir(exist_ok=True)
fastf1.Cache.enable_cache(str(CACHE_DIR))

# == Stored Session and Status Values =============================================================
# Shared state found at "/status"
state = {
    'status':           '0',
    'message':          'No session',
    'session_active':   False,
    'winner_color':    None,
}
stateLock = threading.Lock()
winnerSetTime = None

# Top Three
lastTopThree = None
topThreeLock = threading.Lock()

# Schedule
sessionWindows = []
scheduleLock = threading.Lock()

# Session tracking
sessionEnded = False
sessionType = None
sessionStarted = False

# == Load schedule ================================================================================
def refreshSchedule():
    global sessionWindows
    print("Refreshing F1 schedule...")

    try:
        year = datetime.now(timezone.utc).year
        schedule = fastf1.get_event_schedule(year, include_testing=False)
        windows = []

        for _, event in schedule.iterrows():
            for i in range(1, 6):
                date = f"Session{i}DateUtc"
                name = f"Session{i}"

                if date not in event or event[date] is None:
                    continue

                sessionDate = event[date]
                if hasattr(sessionDate, 'isnull') and sessionDate.isnull():
                    continue
                try:
                    start = sessionDate.to_pydatetime()

                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                    
                    window_start = start - timedelta(minutes=SESSION_BUFFER)
                    window_end   = start + timedelta(hours=SESSION_DURATION)
                    windows.append((window_start, window_end, str(event[name])))
                except Exception:
                    continue

        with scheduleLock:
            sessionWindows = windows
        print(f"Schedule loaded: {len(windows)} session windows found.")

    except Exception as e:
        print(f"Schedule refresh failed: {e}")

def schedule_refresh_loop():
    while True:
        refreshSchedule()
        time.sleep(SCHEDULE_REFRESH)

# == Checking for Active Sessions =================================================================
def isSessionActive():
    now = datetime.now(timezone.utc)
    with scheduleLock:
        for start, end, name in sessionWindows:
            if start <= now <= end:
                return True, name
    return False, None

def sessionCheckLoop():
    global sessionEnded, sessionType, winnerSetTime, lastTopThree
    prevActive = False

    while True:
        active, name = isSessionActive()
        now = datetime.now(timezone.utc)

        with stateLock:
            # If session has been marked complete with "Finalised", don't let the schedule override it back to active
            if not sessionEnded:
                state["session_active"] = active
                if not active:
                    state["status"]  = "0"
                    state["message"] = "No session"

            if active and not prevActive and not sessionEnded:
                print(f"[Schedule] Session starting: {name}")
                state["winner_color"] = None
                winnerSetTime = None
                with topThreeLock:
                    lastTopThree = None

            # Winner display timeout
            if (state["winner_color"] is not None and winnerSetTime is not None and (not active or sessionEnded)):
                elapsed = (now - winnerSetTime).total_seconds() / 60

                if elapsed >= WINNER_DISPLAY_MINS:
                    state["winner_color"] = None
                    winnerSetTime = None
                    try:
                        if os.path.exists(WINNER_STATE_FILE):
                            os.remove(WINNER_STATE_FILE)
                    except Exception:
                        pass
                    print(f"[Winner] Display timeout ({WINNER_DISPLAY_MINS} mins) — clearing.")

        prevActive = active
        time.sleep(30)

# == File Prcessing ============================================================================
def processWinner():
    global winnerSetTime

    with topThreeLock:
        topThree = lastTopThree

    if not topThree:
        print("[Winner] No TopThree data available.")
        return

    lines = topThree.get("Lines", {})
    p1 = None
    if isinstance(lines, dict):
        for key, entry in lines.items():
            if isinstance(entry, dict) and str(entry.get("Position", "")) == "1":
                p1 = entry
                break
        if not p1 and "0" in lines and isinstance(lines["0"], dict):
            p1 = lines["0"]

    if not p1:
        print("[Winner] Could not find P1 in TopThree.")
        return

    color = p1.get("TeamColour")
    name = p1.get("FullName", "Unknown")
    team = p1.get("Team", "Unknown")

    if not color:
        print("[Winner] No TeamColor in TopThree P1 entry.")
        return

    print(f"[Winner] P1: {name} — {team} (#{color})")
    with stateLock:
        state["winner_color"] = color
    winnerSetTime = datetime.now(timezone.utc)

    # Save Winner to File Incase of Restart
    try:
        with open(WINNER_STATE_FILE, 'w') as f:
            json.dump({
                "winner_color": color,
                "winner_set_at": winnerSetTime.isoformat()
            }, f)
        print(f"[Winner] State saved to disk.")
    except Exception as e:
        print(f"[Winner] Could not save state: {e}")

    print(f"[Winner] Color set to #{color} — will clear in {WINNER_DISPLAY_MINS} mins.")

def shouldShowWinner(stype):
    if not stype:
        return False
    return any(s.lower() in stype.lower() for s in WINNER_SESSION_TYPES)

def parseLine(line):
    entry = ast.literal_eval(line)
    if not isinstance(entry, list) or len(entry) < 2:
        return None, None
    category = entry[0]
    data = entry[1]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
    return category, data

def tailAndParse(filepath):
    global sessionEnded, sessionType, lastTopThree
    print(f"[Watcher] Thread started, looking for: {filepath}, exists={os.path.exists(filepath)}")

    try:
        while True:
            while not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                time.sleep(0.2)

            print(f"[Watcher] Starting — file size: {os.path.getsize(filepath)} bytes")

            # Reset sessionStarted for new file so stale data from previous sessions can't set the status
            session_started = False

            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    print(f"[Watcher] File opened successfully")
                    f.seek(0)
                    while True:
                        # Truncation detection
                        try:
                            currentPos = f.tell()
                            actual_size = os.path.getsize(filepath)
                            if currentPos > actual_size:
                                print(f"[Watcher] Truncated — resetting.")
                                f.seek(0)
                        except OSError:
                            pass

                        line = f.readline()
                        if not line:
                            # Check if file was deleted/recreated
                            if not os.path.exists(filepath):
                                print("[Watcher] File gone — restarting watcher loop.")
                                break
                            time.sleep(0.05)
                            continue

                        line = line.strip()
                        if not line:
                            continue

                        try:
                            category, data = parseLine(line)
                            if category is None or not isinstance(data, dict):
                                continue

                            # Track status
                            if category == "TrackStatus":
                                with stateLock:
                                    if state["session_active"] and not sessionEnded and session_started:
                                        state["status"]  = str(data.get("Status",  state["status"]))
                                        state["message"] = str(data.get("Message", state["message"]))
                                print(f"[TrackStatus] {state['status']} — {state['message']} (started={session_started})")

                            # Session info
                            elif category == "SessionInfo":
                                name = data.get("Name") or data.get("Type")
                                if name and name != sessionType:
                                    sessionType = name
                                    print(f"[SessionInfo] Session type: {sessionType}")

                            # TopThree
                            elif category == "TopThree":
                                lines_data = data.get("Lines")
                                if lines_data:
                                    with topThreeLock:
                                        if lastTopThree is None:
                                            lastTopThree = {"Lines": {}}

                                        if isinstance(lines_data, list):
                                            # Post session data store as is
                                            for i, entry in enumerate(lines_data):
                                                lastTopThree["Lines"][str(i)] = entry

                                        elif isinstance(lines_data, dict):
                                            for key, entry in lines_data.items():
                                                if not isinstance(entry, dict):
                                                    continue

                                                # Only store if it's a full entry with TeamColour
                                                if "TeamColour" in entry:
                                                    lastTopThree["Lines"][key] = entry

                                                elif key in lastTopThree["Lines"]:
                                                    # Merge non-TeamColour fields into existing entry
                                                    lastTopThree["Lines"][key].update(entry)

                                    p1_name = lastTopThree["Lines"].get("0", {}).get("FullName", "?")
                                    print(f"[TopThree] Updated — P1: {p1_name}")

                                    with stateLock:
                                        winner_already_set = state["winner_color"] is not None

                                    if sessionEnded and not winner_already_set and shouldShowWinner(sessionType):
                                        print("[TopThree] Session ended, processing winner now.")
                                        processWinner()

                            # Session status 
                            elif category == "SessionStatus":
                                status_val = data.get("Status", "")
                                print(f"[SessionStatus] {status_val} | type: {type(data).__name__} | {str(data)[:80]}")

                                if status_val == "Started":
                                    session_started = True
                                    sessionEnded   = False
                                    with topThreeLock:
                                        lastTopThree = None
                                    with stateLock:
                                        state["status"]  = "1"
                                        state["message"] = "AllClear"
                                    print("[SessionStatus] Session started — status set to green.")

                                elif status_val == "Finalised" and not sessionEnded:
                                    sessionEnded = True
                                    print(f"[SessionStatus] Session finalised — type: {sessionType}")

                                    with stateLock:
                                        state["session_active"] = False
                                        state["status"]         = "0"
                                        state["message"]        = "No session"
                                    print("[SessionStatus] Session marked inactive.")

                                    if shouldShowWinner(sessionType):
                                        processWinner()
                                    else:
                                        print(f"[SessionStatus] Not showing winner for: {sessionType}")

                        except Exception:
                            continue

            except Exception as e:
                print(f"[Watcher] File read error: {e} — retrying...")
                time.sleep(1)

    except Exception as e:
        print(f"[Watcher] FATAL ERROR: {e}")
        traceback.print_exc()

# == Flask server =================================================================================
app = Flask(__name__)

@app.route('/status')
def status():
    with stateLock:
        return jsonify({
            "status":         state["status"],
            "message":        state["message"],
            "session_active": state["session_active"],
            "winner_color":  state["winner_color"],
        })

@app.route('/health')
def health():
    active, name = isSessionActive()
    with stateLock:
        winner = state["winner_color"]
    with topThreeLock:
        hasTopThree = lastTopThree is not None
    mins_remaining = None
    if winner and winnerSetTime:
        elapsed = (datetime.now(timezone.utc) - winnerSetTime).total_seconds() / 60
        mins_remaining = max(0, round(WINNER_DISPLAY_MINS - elapsed, 1))
    return jsonify({
        "ok":                    True,
        "session_active":        active,
        "session_name":          name,
        "session_type":          sessionType,
        "session_ended":         sessionEnded,
        "utc_time":              datetime.now(timezone.utc).isoformat(),
        "has_top_three":         hasTopThree,
        "winner_color":         winner,
        "winner_mins_remaining": mins_remaining,
    })

# == Main =========================================================================================
def main():
    print("Starting F1 sign middleware server...")

    # Remove any stream file from previous run
    if os.path.exists(OUTPUT_FILE):
        os.remove(OUTPUT_FILE)
        print(f"[Main] Removed old stream file.")

    # Restore winner state from file if it exists and hasn't expired
    global winnerSetTime
    try:
        if os.path.exists(WINNER_STATE_FILE):
            with open(WINNER_STATE_FILE, 'r') as f:
                saved = json.load(f)
            color = saved.get("winner_color")
            saved_at = datetime.fromisoformat(saved.get("winner_set_at"))
            elapsed = (datetime.now(timezone.utc) - saved_at).total_seconds() / 60

            if elapsed < WINNER_DISPLAY_MINS and color:
                state["winner_color"] = color
                winnerSetTime          = saved_at
                print(f"[Main] Restored winner color #{color} ({elapsed:.1f} mins ago, {WINNER_DISPLAY_MINS - elapsed:.1f} mins remaining).")

            else:
                os.remove(WINNER_STATE_FILE)
                print("[Main] Winner state expired — cleared.")

    except Exception as e:
        print(f"[Main] Could not restore winner state: {e}")

    refreshSchedule()

    sched_thread = threading.Thread(target=schedule_refresh_loop, daemon=True)
    check_thread = threading.Thread(target=sessionCheckLoop, daemon=True)
    watcher = threading.Thread(target=tailAndParse, args=(OUTPUT_FILE,), daemon=True)
    flask_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=FLASK_PORT, debug=False), daemon=True)

    sched_thread.start()
    check_thread.start()
    watcher.start()
    flask_thread.start()
    print(f"Flask server running on port {FLASK_PORT}")

    while True:
        try:
            print("Connecting to F1 live timing stream...")
            client = SignalRClient(filename=OUTPUT_FILE, filemode='w', timeout=60)
            client.start()
            print(f"Stream disconnected — reconnecting in {RECONNECT_DELAY}s...")

        except KeyboardInterrupt:
            print("\nStopped.")
            break

        except Exception as e:
            print(f"Stream error: {e} — reconnecting in {RECONNECT_DELAY}s...")

        time.sleep(RECONNECT_DELAY)

if __name__ == "__main__":
    main()
