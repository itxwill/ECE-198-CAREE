import atexit
from copy import copy, deepcopy
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
import socket
import sys
import re
from threading import Event, Lock, Thread
import time

from flask import Flask, jsonify, render_template, request
from roster import Roster
from phones import MAX_JPEG, PhoneError, PhoneService

# Startup default; the website button can change this while Flask is running.
SHOW_OPENCV_PREVIEW = False
PREVIEW_WINDOW = "CAREE - detection preview (Q to hide)"
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))


def private_addresses():
    networks = [IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(IPv4Address(info[4][0]))
    except OSError:
        pass
    try:
        # UDP connect selects the local interface without sending a packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            ips.add(IPv4Address(sock.getsockname()[0]))
    except OSError:
        pass
    return sorted(ip for ip in ips if any(ip in network for network in networks))


PHONE_ADDRESSES = private_addresses()


def join_urls():
    return [f"https://{ip}:8443/join" for ip in PHONE_ADDRESSES]


def phone_certificate(directory, addresses):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert = directory / "cert.pem"
    key = directory / "key.pem"
    names = [x509.DNSName("localhost"), x509.IPAddress(IPv4Address("127.0.0.1"))]
    names.extend(x509.IPAddress(ip) for ip in addresses)
    now = datetime.now(timezone.utc)
    if cert.exists() and key.exists():
        try:
            saved = x509.load_pem_x509_certificate(cert.read_bytes())
            san = saved.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            private_key = serialization.load_pem_private_key(key.read_bytes(), password=None)
            if (saved.not_valid_before_utc <= now < saved.not_valid_after_utc
                    and all(name in san for name in names)
                    and saved.public_key() == private_key.public_key()):
                return str(cert), str(key)
        except (ValueError, x509.ExtensionNotFound):
            pass

    directory.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CAREE")])
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
                   .add_extension(x509.SubjectAlternativeName(names), critical=False)
                   .sign(private_key, hashes.SHA256()))
    key.write_bytes(private_key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key.chmod(0o600)
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return str(cert), str(key)


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
        self.phones = PhoneService(self.roster, self._lock, PROJECT_DIR)
        self._preview_enabled = SHOW_OPENCV_PREVIEW
        self._data = {"status": "starting", "error": None, "updated_at": None}

    @property
    def join_info(self):
        return {"code": self.phones.code, "urls": join_urls()}

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
            online = self.phones.online_ids()
            for student in data["students"]:
                student["phone_online"] = student["id"] in online
            data["join"] = self.join_info
            return data

    def link(self, track_id, student_id):
        with self._detector_lock:
            with self._lock:
                if student_id is not None and self.phones.session_for(student_id):
                    raise PhoneError("This student is counted by a phone. Leave the phone session before linking a webcam track.", 409)
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

    def change_mode(self, student_id, mode, phone_token=None):
        with self._detector_lock:
            with self._lock:
                if student_id is None:
                    session = self.phones.require(phone_token)
                    student_id = session.student_id
                if mode not in ("arms", "legs"):
                    raise PhoneError("Choose arms or legs.")
                if not self.roster.update(student_id, mode=mode):
                    return False
                self.phones.reset_movement(student_id)
                for track, sid in self.roster.links.items():
                    if sid == student_id and self._detector:
                        person = self._detector.people.get(track)
                        if person:
                            person.arm_counter.reset_movement()
                            person.leg_counter.reset_movement()
                return self.phones.status(session) if phone_token is not None else True

    def stop(self):
        self._stop.set()
        self.phones.stop()
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

            # The OpenVINO export is not in git; fall back to the downloadable nano model.
            model_path = PROJECT_DIR / "yolo26s-pose_openvino_model"
            model = YOLO(str(model_path) if model_path.exists() else "yolo26n-pose.pt", task="pose")
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


@app.errorhandler(PhoneError)
def phone_error(exc):
    return jsonify(error=str(exc)), exc.status


@app.get("/join")
def join_page():
    return render_template("join.html")


@app.get("/api/join-info")
def join_info():
    return jsonify(detection_service.join_info)


def phone_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@app.post("/api/phone/join")
def phone_join():
    body = phone_body()
    return jsonify(detection_service.phones.join(body.get("code"), body.get("name"), body.get("box")))


@app.post("/api/phone/frame")
def phone_frame():
    token = request.headers.get("X-Phone-Token")
    with detection_service._lock:
        detection_service.phones.require(token)
    if request.mimetype != "image/jpeg":
        raise PhoneError("Send a frame as image/jpeg.", 415)
    if request.content_length is not None and request.content_length > MAX_JPEG:
        raise PhoneError("Frames must be 2 MB or smaller.", 413)
    jpeg = request.stream.read(MAX_JPEG + 1)
    return jsonify(detection_service.phones.submit_frame(token, jpeg))


@app.patch("/api/phone/mode")
def phone_mode():
    return jsonify(detection_service.change_mode(None, phone_body().get("mode"),
                                                 request.headers.get("X-Phone-Token")))


@app.post("/api/phone/leave")
def phone_leave():
    detection_service.phones.leave(request.headers.get("X-Phone-Token"))
    return jsonify(left=True)


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
        detection_service.phones.disconnect(student_id)
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


@app.post("/api/devices/heartbeat")
def device_heartbeat():
    body = request.get_json(silent=True)
    device_id = body.get("device_id") if isinstance(body, dict) else None
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", device_id):
        return jsonify(error="Provide a device_id using 1–64 letters, numbers, colons, underscores or hyphens."), 400
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
        return jsonify(error="Device name must contain 1–40 characters."), 400
    acknowledged_id = body.get("acknowledged_command_id")
    if acknowledged_id is not None and (not isinstance(acknowledged_id, str) or len(acknowledged_id) > 64):
        return jsonify(error="Invalid command acknowledgement."), 400
    # Optional answer to an effort prompt: {"command_id": "...", "value": 0-10} from the box's dial.
    effort = body.get("effort")
    if effort is not None and not (isinstance(effort, dict) and isinstance(effort.get("command_id"), str)
                                   and type(effort.get("value")) is int and 0 <= effort["value"] <= 10):
        return jsonify(error="Effort must be a command_id and a whole number from 0 to 10."), 400
    device_id = device_id.lower()
    with detection_service._lock:
        detection_service.roster.heartbeat(device_id, name.strip(), request.remote_addr or "")
        if effort is not None:
            # Record before device_reply clears the acknowledged prompt.
            detection_service.roster.record_effort(device_id, effort["command_id"], effort["value"])
        reply = detection_service.roster.device_reply(device_id, acknowledged_id)
    return jsonify(reply)


@app.get("/api/devices")
def devices():
    with detection_service._lock:
        response = jsonify(devices=detection_service.roster.device_snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.put("/api/students/<int:student_id>/device")
def assign_device(student_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "device_id" not in body:
        return jsonify(error="Provide device_id, or null to clear the assignment."), 400
    device_id = body["device_id"]
    if device_id is not None and not isinstance(device_id, str):
        return jsonify(error="device_id must be a string or null."), 400
    with detection_service._lock:
        try:
            detection_service.roster.assign_device(student_id, device_id.lower() if device_id is not None else None)
        except KeyError as error:
            return jsonify(error=error.args[0]), 404
        except ValueError as error:
            return jsonify(error=str(error)), 409
    return jsonify(student_id=student_id, device_id=device_id)


if __name__ == "__main__":
    from werkzeug.serving import make_server

    https = None
    print(f"Phone join code: {detection_service.phones.code}", flush=True)
    detection_service.phones.start()  # Load the pose model now, not on the first phone frame.
    try:
        tls = phone_certificate(PROJECT_DIR / "web" / "data" / "tls", PHONE_ADDRESSES)
        https = make_server("0.0.0.0", 8443, app, threaded=True, ssl_context=tls)
        Thread(target=https.serve_forever, name="phone-https", daemon=True).start()
        for url in join_urls():
            print(f"Phone join: {url}", flush=True)
    except ImportError:
        PHONE_ADDRESSES = []
        print("Phone HTTPS unavailable. Run pip install cryptography and restart. Continuing with HTTP.")
    except (OSError, SystemExit) as error:
        PHONE_ADDRESSES = []
        print(f"Phone HTTPS unavailable ({error}). Continuing with HTTP.")

    # ESP32 boxes poll plain HTTP on 8080: the laptop firewall already allows it on private hotspots.
    box_http = None
    try:
        box_http = make_server("0.0.0.0", 8080, app, threaded=True)
        Thread(target=box_http.serve_forever, name="box-http", daemon=True).start()
        print("ESP32 boxes: port 8080", flush=True)
    except OSError as error:
        print(f"ESP32 box port 8080 unavailable ({error}).", flush=True)

    # The reloader would start a second process competing for the same camera.
    try:
        # Listen on the hotspot interface too; never expose Flask's debugger on the LAN.
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    finally:
        for server in (https, box_http):
            if server:
                server.shutdown()
                server.server_close()
        detection_service.stop()
