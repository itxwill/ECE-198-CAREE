"""Process camera frames from phones that have joined the class.
The /api/phone/frame route hands each JPEG to PhoneService. A worker decodes
frames and runs the pose model outside the roster lock so requests can continue.
Person feeds joint angles to RepCounter. Roster.credit saves new counts for
the student's exercise, ready for the next box heartbeat.
"""
from dataclasses import dataclass, field
import logging
import re
import secrets
from threading import Condition, Event, Thread
from time import monotonic


MAX_JPEG = 2 * 1024 * 1024
ONLINE_SECONDS = 10
EXPIRE_SECONDS = 600
SET_DONE_SECONDS = 20  # No new reps for this long ends the set and asks the box for effort.
log = logging.getLogger(__name__)


class PhoneError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass
class Frame:
    jpeg: bytes
    generation: int
    done: Event = field(default_factory=Event)
    error: str = ""


@dataclass
class Session:
    student_id: int
    joined: float = field(default_factory=monotonic)
    last_frame: float | None = None
    pending: Frame | None = None
    generation: int = 0
    person: object = None
    processing_ms: int = 0
    error: str = ""
    reps_in_set: int = 0            # Full + partial reps since the last effort prompt.
    last_rep: float | None = None


class PhoneService:
    def __init__(self, roster, lock, project_dir):
        self.roster = roster
        self.project_dir = project_dir
        self.code = f"{secrets.randbelow(10000):04d}"
        self.condition = Condition(lock)
        self.sessions = {}
        self.worker = None
        self.stopping = False
        self.model = None
        self.resizer = None

    # Wake any waiting upload when its session closes.
    def _remove(self, token):
        session = self.sessions.pop(token)
        if session.pending:
            session.pending.done.set()

    def _expire(self):
        now = monotonic()
        for token, session in list(self.sessions.items()):
            if now - (session.last_frame or session.joined) >= EXPIRE_SECONDS:
                self._remove(token)

    # The caller holds the shared lock for these lookups and roster mutations.
    def session_for(self, student_id):
        self._expire()
        return next((s for s in self.sessions.values() if s.student_id == student_id), None)

    # Expire idle tokens first so an old phone must join again.
    def require(self, token):
        self._expire()
        if token not in self.sessions:
            raise PhoneError("Your phone session has ended. Join again.", 401)
        return self.sessions[token]

    def disconnect(self, student_id):
        for token, session in list(self.sessions.items()):
            if session.student_id == student_id:
                self._remove(token)

    # A phone can stop appearing online before its session expires.
    def online_ids(self):
        self._expire()
        now = monotonic()
        return {s.student_id for s in self.sessions.values()
                if s.last_frame is not None and now - s.last_frame < ONLINE_SECONDS}

    # Match returning students by name and validate the optional box before assigning it.
    def join(self, code, name, box=None):
        if code != self.code:
            raise PhoneError("Check the game code on the dashboard.")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise PhoneError("Enter a name between 1 and 40 characters.")
        if box is not None and not isinstance(box, str):
            raise PhoneError("Enter the six hex digits from your box.")
        name = name.strip()
        box = (box or "").strip().upper()
        with self.condition:
            sid = next((s["id"] for s in self.roster.students.values()
                        if s["name"].casefold() == name.casefold()), None)
            device = None
            if box:
                suffix = box.removeprefix("ESP32-")
                if not re.fullmatch("[0-9A-F]{6}", suffix):
                    raise PhoneError("Enter the six hex digits from your box.")
                device = next((d for d in self.roster.devices.values()
                               if d["id"].replace(":", "").upper().endswith(suffix)), None)
                if device is None:
                    raise PhoneError("Box not found - check the code on your box's screen and that it is on the hotspot")
                if device["student_id"] not in (None, sid):
                    raise PhoneError("This box is already assigned to a different student.", 409)
            if sid is None:
                sid = self.roster.add(name)
            if device and device["student_id"] != sid:
                self.roster.assign_device(sid, device["id"])
            # A new phone session takes over counting from earlier phone or webcam links.
            self.disconnect(sid)
            self.roster.links = {track: owner for track, owner in self.roster.links.items() if owner != sid}
            token = secrets.token_urlsafe(16)
            session = self.sessions[token] = Session(sid)
            self.start()
            return {"token": token, **self.status(session)}

    def start(self):
        """Start the worker; it loads the model right away so the first phone isn't kept waiting."""
        if self.worker is None:
            self.worker = Thread(target=self._run, name="phone-pose", daemon=True)
            self.worker.start()

    def _load_model(self):
        from detection import Detector, YOLO, np
        if self.model is None:
            path = self.project_dir / "yolo26s-pose_openvino_model"
            self.model = YOLO(str(path) if path.exists() else "yolo26n-pose.pt", task="pose")
            # One dummy inference absorbs the slow first call (library and backend start-up).
            self.model.predict(np.zeros((480, 640, 3), np.uint8), verbose=False)
        if self.resizer is None:
            self.resizer = Detector(self.model, frame_width=640)

    # Changing the generation discards results from frames already being processed.
    def reset_movement(self, student_id):
        session = self.session_for(student_id)
        if session:
            session.generation += 1
            if session.person:
                session.person.mark_lost()

    # Show the active limb angle alongside the saved totals for the selected exercise.
    def status(self, session):
        student = self.roster.students[session.student_id]
        mode = student["mode"]
        kind = "arm" if mode == "arms" else "leg"
        person = session.person
        limb = getattr(person, f"active_{kind}", None)
        angle = limb.calculate_angle() if limb else None
        box = next((d["name"] for d in self.roster.devices.values()
                     if d["student_id"] == session.student_id), None)
        return {"student_id": student["id"], "name": student["name"], "mode": mode,
                **student[mode], "box": box, "processing_ms": session.processing_ms,
                "tracking": bool(person and person.tracked),
                "stage": getattr(person, f"{kind}_counter").stage if person else "waiting",
                "angle": round(angle, 1) if angle is not None else None,
                **({"error": session.error} if session.error else {})}

    def leave(self, token):
        with self.condition:
            session = self.require(token)
            if session.reps_in_set:
                self.roster.queue_effort_prompt(session.student_id)  # Leaving also ends the set.
            self._remove(token)

    def _ask_effort_when_set_ends(self):
        """Caller holds the lock. A set ends after SET_DONE_SECONDS with no new reps."""
        now = monotonic()
        for session in self.sessions.values():
            if session.reps_in_set and now - session.last_rep >= SET_DONE_SECONDS:
                self.roster.queue_effort_prompt(session.student_id)
                session.reps_in_set = 0

    def submit_frame(self, token, jpeg_bytes):
        with self.condition:
            session = self.require(token)
            # Reject oversized uploads and missing JPEG headers before queuing any work.
            if len(jpeg_bytes) > MAX_JPEG:
                raise PhoneError("Frames must be 2 MB or smaller.", 413)
            if not jpeg_bytes.startswith(b"\xff\xd8"):
                raise PhoneError("Send a JPEG camera frame.")
            now = monotonic()
            # After a long gap, the next frame starts a fresh movement.
            if session.last_frame and now - session.last_frame >= ONLINE_SECONDS:
                self.reset_movement(session.student_id)
            session.last_frame = now
            # Keep only the newest waiting frame so a slow model can catch up.
            if session.pending:
                session.pending.error = "A newer frame replaced this one."
                session.pending.done.set()
            job = session.pending = Frame(jpeg_bytes, session.generation)
            self.condition.notify()
        finished = job.done.wait(3)
        with self.condition:
            self.require(token)
            if not finished:
                return {**self.status(session), "error": "Waiting for the pose model."}
            return {**self.status(session), **({"error": job.error} if job.error else {})}

    # Take one pending frame per phone and process the batch outside the lock.
    def _run(self):
        try:
            self._load_model()
        # A failed warm-up leaves the worker available to try inference on a real frame.
        except Exception:
            log.exception("Phone pose model failed to load; retrying on the first frame")
        while True:
            with self.condition:
                self._expire()
                if self.stopping:
                    return
                self._ask_effort_when_set_ends()
                batch = [(token, s.pending) for token, s in self.sessions.items() if s.pending]
                if not batch:
                    self.condition.wait(1)  # Wake each second so a finished set is noticed promptly.
                    continue
                for token, _ in batch:
                    self.sessions[token].pending = None
            try:
                self._predict(batch)
            # Release every waiting phone if the batch fails.
            except Exception as exc:
                log.exception("Phone pose inference failed")
                for token, job in batch:
                    if not job.done.is_set():
                        self._finish(token, job, error=str(exc) or "Pose inference failed.")

    def _predict(self, batch):
        from detection import cv2, np

        started = monotonic()
        frames, jobs = [], []
        self._load_model()
        for token, job in batch:
            try:
                frame = cv2.imdecode(np.frombuffer(job.jpeg, np.uint8), cv2.IMREAD_COLOR)
                # Even with a valid JPEG header, damaged image data can fail to decode.
                if frame is None:
                    raise ValueError("Could not decode the JPEG frame.")
                frames.append(self.resizer.prepare_frame(frame))
                jobs.append((token, job))
            except Exception as exc:
                log.warning("Invalid phone frame: %s", exc)
                self._finish(token, job, error=str(exc))
        if frames:
            results = self.model.predict(frames, verbose=False)
            for (token, job), result, frame in zip(jobs, results, frames, strict=True):
                try:
                    pose = self._largest_pose(result)
                except Exception as exc:
                    log.exception("Invalid pose result")
                    self._finish(token, job, error=str(exc))
                    continue
                self._finish(token, job, pose, frame.shape, round((monotonic() - started) * 1000))

    @staticmethod
    # Use the largest confident person in a phone image when several are visible.
    def _largest_pose(result):
        boxes, points = result.boxes, result.keypoints
        if boxes is None or points is None or points.conf is None:
            return None
        coords = boxes.xyxy.cpu().numpy()
        eligible = (boxes.conf.cpu().numpy() >= 0.5).nonzero()[0]
        if not len(eligible):
            return None
        areas = (coords[:, 2] - coords[:, 0]) * (coords[:, 3] - coords[:, 1])
        index = eligible[areas[eligible].argmax()]
        return points.xy[index].cpu().numpy(), points.conf[index].cpu().numpy()

    def _finish(self, token, job, pose=None, shape=None, elapsed=0, error=""):
        with self.condition:
            try:
                session = self.sessions.get(token)
                # A result may arrive after the student left or changed exercise.
                if not session or session.generation != job.generation:
                    return
                session.error = error
                session.processing_ms = elapsed
                person = session.person
                if pose is None:
                    if person:
                        person.mark_lost()
                    return
                from detection import Person
                if person is None:
                    session.person = Person(session.student_id, *pose, shape)
                    return
                mode = self.roster.students[session.student_id]["mode"]
                counter = person.arm_counter if mode == "arms" else person.leg_counter
                # Credit only the increases caused by this frame.
                reps, partial = counter.reps, counter.partial
                person.update(*pose, shape)
                new_reps = max(0, counter.reps - reps)
                new_partial = max(0, counter.partial - partial)
                self.roster.credit(session.student_id, mode, new_reps, new_partial)
                if new_reps or new_partial:
                    session.reps_in_set += new_reps + new_partial
                    session.last_rep = monotonic()
            except Exception as exc:
                log.exception("Could not update phone counts")
                session.error = str(exc)
                session.person = None
            # Release the upload even when the pose was lost or saving the counts failed.
            finally:
                job.done.set()

    def stop(self):
        with self.condition:
            self.stopping = True
            for token in list(self.sessions):
                self._remove(token)
            self.condition.notify_all()
        if self.worker:
            self.worker.join(timeout=5)
