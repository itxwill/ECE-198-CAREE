"""Count exercise repetitions from camera frames.
YOLO gives each person a track ID and reports joint positions (keypoints) and their confidence.
Each limb smooths three positions and checks whether they can give a usable angle.
For vectors u and v pointing out from the middle joint, cos(angle) = dot(u, v) / (|u| |v|).
RepCounter follows the elbow or knee angle through a bend and a return to extension.
"""
from ultralytics import YOLO
import cv2
import numpy as np


def draw_head_id(frame, person):
    """Label the detected head with the same temporary ID used by the dashboard."""
    if person.head is None:
        return
    height, width = frame.shape[:2]
    x, y = map(int, person.head)
    if not (0 <= x < width and 0 <= y < height):
        return
    cv2.circle(frame, (x, y), 7, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.circle(frame, (x, y), 5, (59, 52, 201), -1, cv2.LINE_AA)
    label = f"ID {person.personid}"
    font, scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2
    (text_width, text_height), baseline = cv2.getTextSize(label, font, scale, thickness)
    label_x = max(4, min(x + 12, width - text_width - 4))
    label_y = max(text_height + 4, min(y - 12, height - baseline - 4))
    cv2.putText(frame, label, (label_x, label_y), font, scale, (0, 0, 0), 5, cv2.LINE_AA)
    cv2.putText(frame, label, (label_x, label_y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)


def draw_limbs(frame, person, kind, conf_threshold=0.5):
    """Draw both sides of the chosen limb and label angles at the elbows."""
    height, width = frame.shape[:2]
    for side in ("left", "right"):
        limb = getattr(person, f"{side}_{kind}")
        points = []
        for point, confidence in zip(limb.points, limb.confidences):
            visible = point is not None and confidence > conf_threshold
            # Legs omit off-screen joints. OpenCV clips arm lines at the frame edge.
            if visible and kind == "leg":
                visible = (np.all(np.isfinite(point)) and
                           0 <= point[0] < width and 0 <= point[1] < height)
            points.append(tuple(map(int, point)) if visible else None)

        for start_point, end_point in zip(points, points[1:]):
            if start_point is not None and end_point is not None:
                cv2.line(frame, start_point, end_point, (0, 255, 0), 2)

        for name, point in zip(limb.names, points):
            if point is not None:
                cv2.circle(frame, point, 4, (0, 0, 255), -1)
                cv2.putText(
                    frame, f"{limb.side.title()} {name}", point,
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1
                )

        if kind == "arm":
            angle = limb.calculate_angle(conf_threshold, frame.shape)
            if angle is not None:
                x, y = points[1]
                cv2.putText(frame, f"Angle: {angle:.1f}", (x, y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)


def joint_angle(joints, conf_threshold=0.5, frame_shape=None):
    """Return the angle at the middle joint, or None for unusable joints."""
    for point, confidence in joints:
        if (point is None or not confidence > conf_threshold
                or not np.all(np.isfinite(point))):
            return None
        if frame_shape is not None:
            height, width = frame_shape[:2]
            if not (0 <= point[0] < width and 0 <= point[1] < height):
                return None
    a, b, c = (np.asarray(point) for point, _ in joints)
    ba, bc = a - b, c - b
    denominator = np.linalg.norm(ba) * np.linalg.norm(bc)
    if denominator == 0:
        return None
    cosine = np.dot(ba, bc) / denominator
    return float(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0))))


# Default transitions in degrees. An attempt starts 15 degrees below extension.
# waiting -- >=150 --> extended -- <=135 --> attempting -- <=70 --> bent
# extended can also go straight to bent at <=70.
# bent       -- >=150 --> extended (+1 full rep)
# attempting -- >=150 --> extended (+1 partial rep)
# any state  -- bad angle --> waiting
class RepCounter:
    """Turn a stream of joint angles into full and partial reps.

        waiting --(angle >= 150)--> extended --(drops below 135)--> attempting --(<= 70)--> bent
                                       ^                              |                      |
                                       +------- back to >= 150 -------+  = PARTIAL rep       |
                                       +------------------- back to >= 150 ------------------+  = FULL rep

    A missing or impossible angle (joint hidden, NaN, outside 0-180) sends it back to "waiting",
    so a rep only counts if the whole movement was seen. Two thresholds (hysteresis) stop small
    wobbles around one value from being counted.
    """

    def __init__(self, bent_threshold=70, extended_threshold=150, attempt_margin=15):
        if not 0 <= bent_threshold < extended_threshold <= 180:
            raise ValueError("Thresholds must satisfy 0 <= bent < extended <= 180")
        if not 0 < attempt_margin < extended_threshold - bent_threshold:
            raise ValueError("attempt_margin must be positive and smaller than the threshold gap")
        self.bent_threshold = bent_threshold
        self.extended_threshold = extended_threshold
        self.attempt_margin = attempt_margin
        self.reps = 0
        self.partial = 0
        self.stage = "waiting"

    def reset_movement(self):
        """Discard an unfinished movement without changing the rep total."""
        self.stage = "waiting"

    def update(self, angle):
        if angle is None or not np.isfinite(angle) or not 0 <= angle <= 180:
            self.reset_movement()
            return
        if angle >= self.extended_threshold:
            if self.stage == "bent":
                self.reps += 1
            elif self.stage == "attempting":
                self.partial += 1
            self.stage = "extended"
        elif self.stage in ("extended", "attempting"):
            if angle <= self.bent_threshold:
                self.stage = "bent"
            elif angle <= self.extended_threshold - self.attempt_margin:
                self.stage = "attempting"


class Limb:
    # COCO lists the joints from the torso outward on each side.
    INDICES = {
        "arm": {"left": (5, 7, 9), "right": (6, 8, 10)},
        "leg": {"left": (11, 13, 15), "right": (12, 14, 16)},
    }
    NAMES = {"arm": ("Shoulder", "Elbow", "Wrist"), "leg": ("Hip", "Knee", "Ankle")}

    def __init__(self, kind, side, alpha=0.96, conf_threshold=0.5):
        if kind not in self.INDICES or side not in self.INDICES[kind]:
            raise ValueError(f"Invalid limb kind or side: {kind}, {side}")
        self.side = side
        self.indices = self.INDICES[kind][side]
        self.names = self.NAMES[kind]
        self.alpha = alpha
        self.conf_threshold = conf_threshold
        self.points = [None, None, None]
        self.confidences = [0.0, 0.0, 0.0]

    def update(self, keypoints, confidence):
        for joint, index in enumerate(self.indices):
            score = confidence[index]
            self.confidences[joint] = float(score)
            # Keep the previous position on a weak reading, but record its new score.
            if score < self.conf_threshold:
                continue
            previous = self.points[joint]
            current = keypoints[index]
            if previous is None:
                self.points[joint] = current.copy()
            else:
                self.points[joint] = self.alpha * current + (1 - self.alpha) * previous

    def calculate_angle(self, conf_threshold=0.5, frame_shape=None):
        return joint_angle(list(zip(self.points, self.confidences)), conf_threshold, frame_shape)


class Person:
    def __init__(self, personid, keypoints, confidence, frame_shape=None,
                 conf_threshold=0.5):
        self.personid = personid
        self._reset_limbs()

        self.active_arm = None
        self.active_leg = None
        self.arm_counter = RepCounter()
        self.leg_counter = RepCounter()

        self.update(keypoints, confidence, frame_shape, conf_threshold)

    def update(self, keypoints, confidence, frame_shape=None, conf_threshold=0.5):
        self.tracked = True
        # COCO pose indices 0–4 are nose, eyes, and ears. Never retain a stale head.
        head_points = [point for point, score in zip(keypoints[:5], confidence[:5])
                       if score > conf_threshold and np.all(np.isfinite(point))
                       and (frame_shape is None or
                            (0 <= point[0] < frame_shape[1] and 0 <= point[1] < frame_shape[0]))]
        self.head = np.mean(head_points, axis=0) if head_points else None
        for limb in (self.left_arm, self.right_arm, self.left_leg, self.right_leg):
            limb.update(keypoints, confidence)

        self.active_arm = self._update_counter(
            self.active_arm, self.right_arm, self.left_arm,
            self.arm_counter, frame_shape, conf_threshold
        )
        self.active_leg = self._update_counter(
            self.active_leg, self.right_leg, self.left_leg,
            self.leg_counter, frame_shape, conf_threshold
        )

    def mark_lost(self):
        """Keep completed counts, but discard stale joints and unfinished movement."""
        self.tracked = False
        self.head = None
        self.active_arm = None
        self.active_leg = None
        self.arm_counter.reset_movement()
        self.leg_counter.reset_movement()
        self._reset_limbs()

    def _reset_limbs(self):
        self.left_arm = Limb("arm", "left")
        self.right_arm = Limb("arm", "right")
        self.left_leg = Limb("leg", "left")
        self.right_leg = Limb("leg", "right")

    @staticmethod
    def _update_counter(active, right, left, counter, frame_shape, conf_threshold):
        # Try the active limb first. With no active limb, start on the right.
        selected, angle = None, None
        candidates = (right, left)
        if active:
            other = left if active is right else right
            candidates = (active, other)
        for limb in candidates:
            angle = limb.calculate_angle(conf_threshold, frame_shape)
            if angle is not None:
                selected = limb
                break
        if selected is not active:
            # Two different limbs cannot complete the same repetition.
            counter.reset_movement()
        counter.update(angle)
        return selected


class Detector:
    def __init__(self, model, frame_interval=3, frame_width=480, conf_threshold=0.5):
        if not isinstance(frame_interval, int) or frame_interval < 1:
            raise ValueError("frame_interval must be a positive integer")
        if not isinstance(frame_width, int) or frame_width < 1:
            raise ValueError("frame_width must be a positive integer")
        if not 0 <= conf_threshold <= 1:
            raise ValueError("conf_threshold must be between 0 and 1")

        self.model = model
        self.conf_threshold = conf_threshold
        self.frame_interval = frame_interval
        self.frame_width = frame_width
        self._frame_count = 0
        self.people = {}

    def mark_missing(self, active_records):
        for record_id, person in self.people.items():
            if record_id not in active_records and person.tracked:
                person.mark_lost()

    def prepare_frame(self, frame):
        height, width = frame.shape[:2]
        if width <= self.frame_width:
            return frame
        return cv2.resize(
            frame,
            (self.frame_width, max(1, round(height * self.frame_width / width))),
            interpolation=cv2.INTER_AREA
        )

    def detect_people(self, frame):
        # Skipped frames reuse the previous result, including its tracking status.
        analyze = self._frame_count % self.frame_interval == 0
        self._frame_count += 1
        if not analyze:
            return self.people

        results = self.model.track(frame, persist=True, show=False, verbose=False)

        # An analyzed frame without poses or track IDs ends unfinished movements.
        if not results:
            self.mark_missing(set())
            return self.people

        result = results[0]

        if (result.keypoints is None or
                result.keypoints.conf is None or
                result.boxes is None or
                result.boxes.id is None):
            self.mark_missing(set())
            return self.people

        ids = result.boxes.id.int().cpu().tolist()
        xy = result.keypoints.xy.cpu().numpy()
        conf = result.keypoints.conf.cpu().numpy()

        active_records = set()
        for personid, person_kpts, person_conf in zip(ids, xy, conf):
            active_records.add(personid)
            if personid in self.people:
                self.people[personid].update(
                    person_kpts, person_conf, frame.shape, self.conf_threshold
                )
            else:
                self.people[personid] = Person(
                    personid, person_kpts, person_conf, frame.shape, self.conf_threshold
                )

        self.mark_missing(active_records)

        return self.people


if __name__ == "__main__":

    def draw_arm_status(frame, person, row=0):
        arm_side = person.active_arm.side.title() if person.active_arm else "Not visible"
        stage = person.arm_counter.stage.title()
        lines = (
            f"ID {person.personid} | Reps: {person.arm_counter.reps}",
            f"Arm: {arm_side} | Stage: {stage}",
        )
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale, thickness = 0.75, 2
        text_width = max(cv2.getTextSize(text, font, scale, thickness)[0][0]
                        for text in lines)
        # Fit narrower video frames while keeping the status inside the image.
        scale *= min(1.0, max(1, frame.shape[1] - 40) / max(1, text_width))
        top = 10 + row * 90
        cv2.rectangle(frame, (10, top), (frame.shape[1] - 10, top + 80), (20, 20, 20), -1)
        for index, text in enumerate(lines):
            color = (255, 255, 255) if index == 0 else (0, 255, 255)
            cv2.putText(
                frame, text, (20, top + 30 + index * 34), font,
                scale, color, thickness, cv2.LINE_AA
            )

    detector = Detector(YOLO("yolo26s-pose_openvino_model"), frame_width=640)

    cap = cv2.VideoCapture(0)
    cv2.namedWindow("arm demo", cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    cv2.resizeWindow("arm demo", 1280, 720)

    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            break

        frame = detector.prepare_frame(frame)
        people = detector.detect_people(frame)

        for person in people.values():
            draw_limbs(frame, person, "arm", detector.conf_threshold)
            draw_head_id(frame, person)

        # Draw status last so joint labels cannot cover it.
        for row, person in enumerate(people.values()):
            draw_arm_status(frame, person, row)
        cv2.imshow("arm demo", frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
