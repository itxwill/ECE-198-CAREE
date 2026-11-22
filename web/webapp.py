import atexit
from copy import copy, deepcopy
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import time

from flask import Flask, jsonify, render_template, request
from roster import Roster

# Startup default; the website button can change this while Flask is running.
SHOW_OPENCV_PREVIEW = False
PREVIEW_WINDOW = "CAREE - detection preview (Q to hide)"
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))


def limb_snapshot(person, limb_type, frame_shape, threshold):
    active = getattr(person, f"active_{limb_type}")
    counter = getattr(person, f"{limb_type}_counter")

    def angle(limb):
        value = limb.calculate_angle(threshold, frame_shape) if limb else None
        return round(float(value), 1) if value is not None else None

    return {
        "reps": counter.reps,
        "partial": counter.partial,
        "stage": counter.stage,
        "angle": angle(active),
        "left": angle(getattr(person, f"left_{limb_type}")),
        "right": angle(getattr(person, f"right_{limb_type}")),
        "status": "Tracking" if active else "Lost tracking",
    }


def preview_person(person, detection_shape, preview_shape):
    """Scale drawing coordinates without modifying the detector's tracked joints."""
    scale_x = preview_shape[1] / detection_shape[1]
    scale_y = preview_shape[0] / detection_shape[0]
    rendered = copy(person)
    if person.head is not None:
        rendered.head = person.head * (scale_x, scale_y)
    for limb_name, joints in (
        ("left_arm", ("shoulder", "elbow", "wrist")),
        ("right_arm", ("shoulder", "elbow", "wrist")),
        ("left_leg", ("hip", "knee", "ankle")),
        ("right_leg", ("hip", "knee", "ankle")),
    ):
        limb = copy(getattr(person, limb_name))
        for joint in joints:
            point = getattr(limb, joint)
            if point is not None:
                setattr(limb, joint, point * (scale_x, scale_y))
        setattr(rendered, limb_name, limb)
    return rendered


class DetectionService:
    """One camera/inference worker per Flask process; requests only read snapshots."""

    def __init__(self):
        self._lock = Lock()
        self._detector_lock = Lock()
        self._detector = None
        self._stop = Event()
        self._thread = None
        self._last_frame = None
        self.roster = Roster(Path(__file__).resolve().parent / "data" / "students.sqlite3")
        self._preview_enabled = SHOW_OPENCV_PREVIEW
        self._data = {"status": "starting", "error": None, "updated_at": None}

    def start(self):
        with self._lock:
            if self._thread is None:
                self._thread = Thread(target=self._run, name="pose-detector", daemon=True)
                self._thread.start()

    def snapshot(self):
        with self._lock:
            data = deepcopy(self._data)
            if (data["status"] == "running" and self._last_frame is not None
                    and time.monotonic() - self._last_frame > 5):
                data.update(status="stale", error="Camera processing is not updating.")
            data.update(self.roster.snapshot(live=data["status"] == "running"))
            return data

    def link(self, track_id, student_id):
        with self._detector_lock:
            with self._lock:
                if (self._data["status"] != "running" or self._last_frame is None
                        or time.monotonic() - self._last_frame > 5):
                    return False
                if student_id is None:
                    self.roster.links.pop(track_id, None)
                    return True
                if not self.roster.link(track_id, student_id):
                    return False
                person = self._detector.people.get(track_id)
                if person:
                    person.arm_counter.reset_movement()
                    person.leg_counter.reset_movement()
                return True

    def rename(self, person_id, name):
        with self._lock:
            return self.roster.update(person_id, name=name)

    def change_mode(self, student_id, mode):
        with self._detector_lock:
            with self._lock:
                if not self.roster.update(student_id, mode=mode):
                    return False
                for track, sid in self.roster.links.items():
                    if sid == student_id and self._detector:
                        person = self._detector.people.get(track)
                        if person:
                            person.arm_counter.reset_movement()
                            person.leg_counter.reset_movement()
                return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def preview_enabled(self):
        with self._lock:
            return self._preview_enabled

    def set_preview(self, enabled):
        with self._lock:
            if enabled and self._data["status"] == "error":
                return False
            self._preview_enabled = enabled
            return True

    def _run(self):
        cap = None
        window_open = False
        try:
            # Lazy import keeps the page available while model dependencies load.
            from detection import Detector, YOLO, cv2, draw_arms, draw_legs, draw_head_id

            model = YOLO(str(PROJECT_DIR / "yolo26s-pose_openvino_model"))
            detector = Detector(model, frame_interval=3, frame_width=640, conf_threshold=0.5)
            self._detector = detector
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                raise RuntimeError("Could not open camera 0. Close other apps using the camera.")
            # The camera may choose the closest resolution it supports.
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)

            while not self._stop.is_set():
                success, camera_frame = cap.read()
                if not success:
                    raise RuntimeError("Could not read a frame from camera 0.")
                frame = detector.prepare_frame(camera_frame)
                with self._detector_lock:
                    people = detector.detect_people(frame)
                    detections = self._student_snapshot(people, frame.shape, detector.conf_threshold)
                    visible_people = [deepcopy(person) for person in people.values() if person.tracked] if self.preview_enabled() else []
                    with self._lock:
                        self.roster.ingest(detections)
                        self._data = {"status": "running", "error": None, "updated_at": time.time()}
                        self._last_frame = time.monotonic()
                if self.preview_enabled():
                    if not window_open:
                        cv2.namedWindow(PREVIEW_WINDOW, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
                        cv2.resizeWindow(PREVIEW_WINDOW, CAMERA_WIDTH, CAMERA_HEIGHT)
                        window_open = True
                    preview = camera_frame.copy()
                    for person in visible_people:
                        rendered = preview_person(person, frame.shape, preview.shape)
                        draw_arms(preview, rendered, detector.conf_threshold)
                        draw_legs(preview, rendered, detector.conf_threshold)
                        draw_head_id(preview, rendered)
                    cv2.imshow(PREVIEW_WINDOW, preview)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        cv2.destroyWindow(PREVIEW_WINDOW)
                        window_open = False
                        self.set_preview(False)
                    elif cv2.getWindowProperty(PREVIEW_WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                        window_open = False
                        self.set_preview(False)
                elif window_open:
                    cv2.destroyWindow(PREVIEW_WINDOW)
                    cv2.waitKey(1)
                    window_open = False
        except Exception as exc:
            app.logger.exception("Headless detection stopped")
            with self._lock:
                self._data.update(status="error", error=str(exc))
        finally:
            self.set_preview(False)
            if cap is not None:
                cap.release()
            if window_open:
                cv2.destroyWindow(PREVIEW_WINDOW)

    @staticmethod
    def _student_snapshot(people, frame_shape, threshold):
        return [
            {
                "id": int(person.personid),
                "name": f"Person {person.personid}",
                "tracked": person.tracked,
                "arms": limb_snapshot(person, "arm", frame_shape, threshold),
                "legs": limb_snapshot(person, "leg", frame_shape, threshold),
            }
            for person in sorted(people.values(), key=lambda person: person.personid)
        ]


app = Flask(__name__)
detection_service = DetectionService()
atexit.register(detection_service.stop)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/students")
def students():
    detection_service.start()
    response = jsonify(detection_service.snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.patch("/api/students/<int:person_id>/name")
def rename_student(person_id):
    body = request.get_json(silent=True)
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        return jsonify(error="Name must contain 1–80 characters."), 400
    name = name.strip()
    if not detection_service.rename(person_id, name):
        return jsonify(error="Student not found."), 404
    return jsonify(id=person_id, name=name)


@app.route("/api/preview", methods=["GET", "PUT"])
def preview():
    if request.method == "PUT":
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return jsonify(error="enabled must be true or false."), 400
        if not detection_service.set_preview(body["enabled"]):
            return jsonify(error="Detection has stopped. Restart Flask to retry the camera."), 409
        detection_service.start()
    response = jsonify(enabled=detection_service.preview_enabled())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/students")
def add_student():
    body = request.get_json(silent=True)
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        return jsonify(error="Name must contain 1–80 characters."), 400
    with detection_service._lock:
        student_id = detection_service.roster.add(name.strip())
    return jsonify(id=student_id), 201


@app.patch("/api/students/<int:student_id>/mode")
def change_mode(student_id):
    body = request.get_json(silent=True)
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("arms", "legs"):
        return jsonify(error="Choose arms or legs."), 400
    if not detection_service.change_mode(student_id, mode):
        return jsonify(error="Student not found."), 404
    return jsonify(id=student_id, mode=mode)


@app.delete("/api/students/<int:student_id>")
def remove_student(student_id):
    with detection_service._lock:
        if not detection_service.roster.remove(student_id):
            return jsonify(error="Student not found."), 404
    return jsonify(id=student_id, removed=True)


@app.patch("/api/students/<int:student_id>/counts")
def edit_count(student_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Provide an exercise, count field, and integer value."), 400
    with detection_service._lock:
        try:
            found = detection_service.roster.set_count(
                student_id, body.get("mode"), body.get("field"), body.get("value"))
        except ValueError as error:
            return jsonify(error=str(error)), 400
    if not found:
        return jsonify(error="Student not found."), 404
    return jsonify(id=student_id, mode=body["mode"], field=body["field"], value=body["value"])


@app.put("/api/detections/<int:track_id>/link")
def link_detection(track_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "student_id" not in body:
        return jsonify(error="Provide student_id, or null to unlink."), 400
    student_id = body["student_id"]
    if student_id is not None and type(student_id) is not int:
        return jsonify(error="student_id must be an integer or null."), 400
    if not detection_service.link(track_id, student_id):
        return jsonify(error="Detection is no longer visible or student does not exist."), 409
    return jsonify(id=track_id, student_id=student_id)


if __name__ == "__main__":
    # The reloader would start a second process competing for the same camera.
    try:
        app.run(debug=True, use_reloader=False)
    finally:
        detection_service.stop()
