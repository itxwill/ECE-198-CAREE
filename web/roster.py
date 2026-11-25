"""Keep student counts and box assignments in SQLite. Callers hold the service lock.
PhoneService calls Roster.credit with new reps from the student's phone.
For a linked webcam track, compare successive totals to find the new reps.
Each box heartbeat reads its student's totals and delivers one pending command.
Track links and pending commands last only for this run of the dashboard.
"""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

DEVICE_TIMEOUT_SECONDS = 20


class Roster:
    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        # The connection context commits/rolls back; closing releases the file handle.
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL,
                arms_reps INTEGER NOT NULL, arms_partial INTEGER NOT NULL,
                legs_reps INTEGER NOT NULL, legs_partial INTEGER NOT NULL)""")
            rows = connection.execute("SELECT * FROM students ORDER BY id").fetchall()
            connection.execute("""CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, ip TEXT NOT NULL,
                student_id INTEGER UNIQUE)""")
            device_rows = connection.execute("SELECT id, name, ip, student_id FROM devices ORDER BY name").fetchall()
            # Effort ratings (0-10) entered on the box's rotary dial after a set.
            connection.execute("""CREATE TABLE IF NOT EXISTS efforts (
                student_id INTEGER NOT NULL, value INTEGER NOT NULL, at REAL NOT NULL)""")
            effort_rows = connection.execute(
                "SELECT student_id, value FROM efforts ORDER BY at").fetchall()
        self.efforts = {student_id: value for student_id, value in effort_rows}  # latest per student
        self.students = {
            row[0]: {"id": row[0], "name": row[1], "mode": row[2],
                     "arms": {"reps": row[3], "partial": row[4]},
                     "legs": {"reps": row[5], "partial": row[6]}}
            for row in rows
        }
        self.links = {}  # temporary detection ID -> permanent student ID
        self.baselines = {}
        self.visible = {}
        self.devices = {
            row[0]: {"id": row[0], "name": row[1], "ip": row[2],
                     "student_id": row[3], "last_seen": None}
            for row in device_rows
        }
        self.commands = {}  # Short-lived, acknowledged device notifications; never replay on restart.

    def _save(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany("INSERT OR REPLACE INTO students VALUES (?, ?, ?, ?, ?, ?, ?)", [
                (s["id"], s["name"], s["mode"], s["arms"]["reps"], s["arms"]["partial"],
                 s["legs"]["reps"], s["legs"]["partial"]) for s in self.students.values()
            ])

    def add(self, name):
        student_id = max(self.students, default=0) + 1
        self.students[student_id] = {
            "id": student_id, "name": name, "mode": "arms",
            "arms": {"reps": 0, "partial": 0}, "legs": {"reps": 0, "partial": 0},
        }
        self._save()
        return student_id

    def update(self, student_id, **fields):
        if student_id not in self.students:
            return False
        self.students[student_id].update(fields)
        self._save()
        return True

    # Removing a student also frees their box and discards its pending commands.
    def remove(self, student_id):
        if student_id not in self.students:
            return False
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM students WHERE id = ?", (student_id,))
            connection.execute("DELETE FROM efforts WHERE student_id = ?", (student_id,))
        self.efforts.pop(student_id, None)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE devices SET student_id = NULL WHERE student_id = ?", (student_id,))
        del self.students[student_id]
        self.links = {track: sid for track, sid in self.links.items() if sid != student_id}
        for device in self.devices.values():
            if device["student_id"] == student_id:
                device["student_id"] = None
                self.commands.pop(device["id"], None)
        return True

    # Refresh online status on every poll and save box details only when they change.
    def heartbeat(self, device_id, name, ip):
        previous = self.devices.get(device_id)
        if previous is None or previous["name"] != name or previous["ip"] != ip:
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("""INSERT INTO devices (id, name, ip) VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name, ip=excluded.ip""",
                                   (device_id, name, ip))
        self.devices[device_id] = {
            "id": device_id, "name": name, "ip": ip,
            "student_id": previous["student_id"] if previous else None,
            "last_seen": time.monotonic(),
        }

    # Keep at most one box per student and clear commands from the previous assignment.
    def assign_device(self, student_id, device_id):
        if student_id not in self.students:
            raise KeyError("Student not found.")
        if device_id is not None:
            device = self.devices.get(device_id)
            if device is None:
                raise KeyError("Device has not registered yet.")
            if device["student_id"] not in (None, student_id):
                raise ValueError("This device is already assigned to another student.")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE devices SET student_id = NULL WHERE student_id = ?", (student_id,))
            if device_id is not None:
                connection.execute("UPDATE devices SET student_id = ? WHERE id = ?", (student_id, device_id))
        for device in self.devices.values():
            if device["student_id"] == student_id:
                device["student_id"] = None
                self.commands.pop(device["id"], None)
        if device_id is not None:
            self.devices[device_id]["student_id"] = student_id
            self.commands.pop(device_id, None)

    # Only recently seen boxes receive beeps, with room for the latest eight commands.
    def queue_partial_beep(self, student_id):
        now = time.monotonic()
        for device in self.devices.values():
            if (device["student_id"] == student_id and device["last_seen"] is not None
                    and now - device["last_seen"] < DEVICE_TIMEOUT_SECONDS):
                queue = [command for command in self.commands.get(device["id"], []) if command["expires"] > now]
                queue.append({"id": uuid4().hex, "type": "partial_rep", "duration_ms": 180, "expires": now + 5})
                self.commands[device["id"]] = queue[-8:]

    def queue_effort_prompt(self, student_id):
        """Ask the student's box for a 0-10 effort rating. Returns True if a box was asked."""
        now = time.monotonic()
        asked = False
        for device in self.devices.values():
            if (device["student_id"] == student_id and device["last_seen"] is not None
                    and now - device["last_seen"] < DEVICE_TIMEOUT_SECONDS):
                queue = [command for command in self.commands.get(device["id"], []) if command["expires"] > now]
                # Reuse a pending effort prompt if the set ends again before the box answers.
                if not any(command["type"] == "effort_prompt" for command in queue):
                    # Effort goes first so a queued beep cannot hold it back; it waits up to 3 minutes.
                    queue.insert(0, {"id": uuid4().hex, "type": "effort_prompt", "expires": now + 180})
                self.commands[device["id"]] = queue[:8]
                asked = True
        return asked

    def record_effort(self, device_id, command_id, value):
        """Accept an answer only while its prompt is still queued for the assigned box."""
        pending = any(command["id"] == command_id and command["type"] == "effort_prompt"
                      for command in self.commands.get(device_id, []))
        student_id = self.devices.get(device_id, {}).get("student_id")
        if not pending or student_id not in self.students:
            return False
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO efforts VALUES (?, ?, ?)", (student_id, value, time.time()))
        self.efforts[student_id] = value
        return True

    # Drop expired or acknowledged commands and return the first remaining one.
    def device_reply(self, device_id, acknowledged_id=None):
        device = self.devices[device_id]
        now = time.monotonic()
        queue = [command for command in self.commands.get(device_id, [])
                 if command["expires"] > now and command["id"] != acknowledged_id]
        self.commands[device_id] = queue
        student = self.students.get(device["student_id"])
        return {
            "device_id": device_id, "device_name": device["name"],
            "heartbeat_interval_ms": 500,
            "student": {"id": student["id"], "name": student["name"], "mode": student["mode"],
                        **student[student["mode"]]} if student else None,
            "commands": [{key: value for key, value in command.items() if key != "expires"}
                         for command in queue[:1]],
        }

    def device_snapshot(self):
        now = time.monotonic()
        return [
            {"id": device["id"], "name": device["name"], "ip": device["ip"],
             "student_id": device["student_id"],
             "online": device["last_seen"] is not None and now - device["last_seen"] < DEVICE_TIMEOUT_SECONDS}
            for device in sorted(self.devices.values(), key=lambda value: (value["name"], value["id"]))
        ]

    def set_count(self, student_id, mode, field, value):
        if mode not in ("arms", "legs") or field not in ("reps", "partial"):
            raise ValueError("Invalid exercise or count field.")
        if type(value) is not int or not 0 <= value <= 2147483647:
            raise ValueError("Enter a whole number from 0 to 2147483647.")
        if student_id not in self.students:
            return False
        self.students[student_id][mode][field] = value
        self._save()
        return True

    # Save phone count increases before queuing feedback for the box.
    def credit(self, student_id, mode, reps_inc, partial_inc):
        if reps_inc == 0 and partial_inc == 0:
            return
        counts = self.students[student_id][mode]
        updated = {"reps": counts["reps"] + reps_inc, "partial": counts["partial"] + partial_inc}
        self.students[student_id][mode] = updated
        try:
            self._save()
        # Restore the previous totals in memory if SQLite cannot save the change.
        except Exception:
            self.students[student_id][mode] = counts
            raise
        if partial_inc > 0:
            self.queue_partial_beep(student_id)

    def ingest(self, detections):
        visible = {d["id"]: d for d in detections if d["tracked"]}
        # Tracking loss severs the link; a new/returning track must be attached explicitly.
        self.links = {track: student for track, student in self.links.items() if track in visible}
        changed = False
        for track, detection in visible.items():
            previous = self.baselines.get(track)
            if track in self.links and previous is not None:
                student = self.students[self.links[track]]
                mode = student["mode"]
                for field in ("reps", "partial"):
                    increment = max(0, detection[mode][field] - previous[mode][field])
                    student[mode][field] += increment
                    if field == "partial" and increment > 0:
                        self.queue_partial_beep(student["id"])
                    changed = changed or increment > 0
            self.baselines[track] = deepcopy(detection)
        self.baselines = {track: value for track, value in self.baselines.items() if track in visible}
        self.visible = deepcopy(visible)
        if changed:
            self._save()

    # Start from the current totals so earlier reps cannot be credited to a new student.
    def link(self, track_id, student_id):
        if track_id not in self.visible or student_id not in self.students:
            return False
        self.links = {track: student for track, student in self.links.items()
                      if track != track_id and student != student_id}
        self.links[track_id] = student_id
        self.baselines[track_id] = deepcopy(self.visible[track_id])
        return True

    # Keep saved student totals visible even when the webcam has stopped reporting.
    def snapshot(self, live=True):
        students = [
            {"id": s["id"], "name": s["name"], "mode": s["mode"],
             "device_id": next((device["id"] for device in self.devices.values() if device["student_id"] == s["id"]), None),
             "reps": s[s["mode"]]["reps"], "partial": s[s["mode"]]["partial"],
             "effort": self.efforts.get(s["id"]),
             "detection_id": next((track for track, sid in self.links.items() if sid == s["id"]), None) if live else None}
            for s in self.students.values()
        ]
        detections = []
        if live:
            for track, detection in self.visible.items():
                student = self.students.get(self.links.get(track))
                detections.append({**deepcopy(detection),
                                   "student_id": student["id"] if student else None,
                                   "name": student["name"] if student else "Unlinked",
                                   "mode": student["mode"] if student else None})
        return {"students": students, "detections": detections, "devices": self.device_snapshot()}
