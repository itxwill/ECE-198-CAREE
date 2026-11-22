from ultralytics import YOLO
import cv2
import numpy as np

# helper functions

def draw_arms(frame, person, conf_threshold=0.5):
    for arm in (person.left_arm, person.right_arm):
        joints = (
            ("Shoulder", arm.shoulder, arm.shoulder_confidence),
            ("Elbow", arm.elbow, arm.elbow_confidence),
            ("Wrist", arm.wrist, arm.wrist_confidence),
        )
        points = [
            tuple(map(int, coordinates))
            if coordinates is not None and confidence > conf_threshold else None
            for _, coordinates, confidence in joints
        ]

        for start_point, end_point in zip(points, points[1:]):
            if start_point is not None and end_point is not None:
                cv2.line(frame, start_point, end_point, (0, 255, 0), 2)

        for (name, _, _), point in zip(joints, points):
            if point is not None:
                cv2.circle(frame, point, 4, (0, 0, 255), -1)
                cv2.putText(
                    frame, f"{arm.side.title()} {name}", point,
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1
                )

        # put angle on the elbow
        angle = arm.calculate_angle(conf_threshold, frame.shape)
        if angle is not None:
            cv2.putText(
                frame, f"Angle: {angle:.1f}", (int(arm.elbow[0]), int(arm.elbow[1]) - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1
            )


def draw_legs(frame, person, conf_threshold=0.5):
    for leg in (person.left_leg, person.right_leg):
        joints = (
            ("Hip", leg.hip, leg.hip_confidence),
            ("Knee", leg.knee, leg.knee_confidence),
            ("Ankle", leg.ankle, leg.ankle_confidence),
        )
        height, width = frame.shape[:2]
        points = [
            tuple(map(int, coordinates))
            if (coordinates is not None and confidence > conf_threshold
                and np.all(np.isfinite(coordinates))
                and 0 <= coordinates[0] < width
                and 0 <= coordinates[1] < height) else None
            for _, coordinates, confidence in joints
        ]

        for start_point, end_point in zip(points, points[1:]):
            if start_point is not None and end_point is not None:
                cv2.line(frame, start_point, end_point, (0, 255, 0), 2)

        for (name, _, _), point in zip(joints, points):
            if point is not None:
                cv2.circle(frame, point, 4, (0, 0, 255), -1)
                cv2.putText(
                    frame, f"{leg.side.title()} {name}", point,
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1
                )


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

# classes

class RepCounter:
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


class Arm:
    INDICES = {
        "left": (5, 7, 9),
        "right": (6, 8, 10)
    }

    def __init__(self, side, alpha=0.96, conf_threshold=0.5):
        if side not in self.INDICES:
            raise ValueError("Invalid arm side")

        self.side = side
        self.alpha = alpha
        self.conf_threshold = conf_threshold

        self.shoulder = None
        self.elbow = None
        self.wrist = None

        self.shoulder_confidence = 0.0
        self.elbow_confidence = 0.0
        self.wrist_confidence = 0.0

    def smooth(self, previous, current, confidence):
        if confidence < self.conf_threshold:
            return previous

        if previous is None:
            return current.copy()

        return (
            self.alpha * current +
            (1 - self.alpha) * previous
        )

    def update(self, keypoints, confidence):
        s, e, w = self.INDICES[self.side]

        self.shoulder = self.smooth(
            self.shoulder, keypoints[s], confidence[s]
        )
        self.elbow = self.smooth(
            self.elbow, keypoints[e], confidence[e]
        )
        self.wrist = self.smooth(
            self.wrist, keypoints[w], confidence[w]
        )

        self.shoulder_confidence = float(confidence[s])
        self.elbow_confidence = float(confidence[e])
        self.wrist_confidence = float(confidence[w])

    def calculate_angle(self, conf_threshold=0.5, frame_shape=None):
        joints = (
            (self.shoulder, self.shoulder_confidence),
            (self.elbow, self.elbow_confidence),
            (self.wrist, self.wrist_confidence),
        )

        return joint_angle(joints, conf_threshold, frame_shape)

class Leg:
    INDICES = {
        "left": (11, 13, 15),
        "right": (12, 14, 16)
    }

    def __init__(self, side, alpha=0.96, conf_threshold=0.5):
        if side not in self.INDICES:
            raise ValueError("Invalid leg side")
        self.side = side
        self.alpha = alpha
        self.conf_threshold = conf_threshold
        self.hip = None
        self.knee = None
        self.ankle = None
        self.hip_confidence = 0.0
        self.knee_confidence = 0.0
        self.ankle_confidence = 0.0

    def smooth(self, previous, current, confidence):
        if confidence < self.conf_threshold:
            return previous
        if previous is None:
            return current.copy()
        return self.alpha * current + (1 - self.alpha) * previous

    def update(self, keypoints, confidence):
        h, k, a = self.INDICES[self.side]
        self.hip = self.smooth(self.hip, keypoints[h], confidence[h])
        self.knee = self.smooth(self.knee, keypoints[k], confidence[k])
        self.ankle = self.smooth(self.ankle, keypoints[a], confidence[a])
        self.hip_confidence = float(confidence[h])
        self.knee_confidence = float(confidence[k])
        self.ankle_confidence = float(confidence[a])

    def calculate_angle(self, conf_threshold=0.5, frame_shape=None):
        joints = (
            (self.hip, self.hip_confidence),
            (self.knee, self.knee_confidence),
            (self.ankle, self.ankle_confidence),
        )
        return joint_angle(joints, conf_threshold, frame_shape)


class Person:
    def __init__(self, personid, keypoints, confidence, frame_shape=None,
                 conf_threshold=0.5):
        self.personid = personid
        self.left_arm = Arm("left")
        self.right_arm = Arm("right")
        self.left_leg = Leg("left")
        self.right_leg = Leg("right")

        self.active_arm = None
        self.active_leg = None
        self.arm_counter = RepCounter(bent_threshold=70, extended_threshold=150)
        self.leg_counter = RepCounter(bent_threshold=70, extended_threshold=150)

        self.update(keypoints, confidence, frame_shape, conf_threshold)

    def update(self, keypoints, confidence, frame_shape=None, conf_threshold=0.5):
        self.tracked = True
        self.left_arm.update(keypoints, confidence)
        self.right_arm.update(keypoints, confidence)
        self.left_leg.update(keypoints, confidence)
        self.right_leg.update(keypoints, confidence)

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
        self.active_arm = None
        self.active_leg = None
        self.arm_counter.reset_movement()
        self.leg_counter.reset_movement()
        self.left_arm = Arm("left")
        self.right_arm = Arm("right")
        self.left_leg = Leg("left")
        self.right_leg = Leg("right")

    @staticmethod
    def _update_counter(active, right, left, counter, frame_shape, conf_threshold):
        # Keep the current limb while usable; otherwise prefer right, then left.
        selected, angle = None, None
        candidates = (active, left if active is right else right) if active else (right, left)
        for limb in candidates:
            angle = limb.calculate_angle(conf_threshold, frame_shape)
            if angle is not None:
                selected = limb
                break
        if selected is not active:
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
        self.track_records = {}

    def reassign(self, record_id, target_id):
        """Attach a visible track to a lost record, preserving its name/record ID."""
        saved = self.people.get(record_id)
        target = self.people.get(target_id)
        if saved is None or target is None or saved.tracked or not target.tracked:
            return False
        for kind in ("arm", "leg"):
            previous = getattr(saved, f"{kind}_counter")
            current = getattr(target, f"{kind}_counter")
            current.reps += previous.reps
            current.partial += previous.partial
            current.reset_movement()
        # Remove old track aliases so a later return cannot overwrite this record.
        self.track_records = {
            track: (record_id if record == target_id else record)
            for track, record in self.track_records.items() if record != record_id
        }
        target.personid = record_id
        self.people[record_id] = target
        del self.people[target_id]
        return True

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
        # frame skipping
        analyze = self._frame_count % self.frame_interval == 0
        self._frame_count += 1
        if not analyze:
            return self.people
        
        results = self.model.track(
            frame,
            persist=True,
            show=False,
            verbose=False
        )

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
        for track_id, person_kpts, person_conf in zip(ids, xy, conf):
            if track_id not in self.track_records:
                record_id = track_id
                # A previous reassignment can leave this ID occupied by another track.
                if record_id in self.people:
                    record_id = max([*self.people, *ids]) + 1
                self.track_records[track_id] = record_id
            personid = self.track_records[track_id]
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

# setup

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

    model = YOLO("yolo26s-pose_openvino_model")

    cap = cv2.VideoCapture(0)

    cv2.namedWindow("arm demo", cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    cv2.resizeWindow("arm demo", 1280, 720)


    myDetector = Detector(
        model, frame_interval=3, frame_width=640, conf_threshold=0.5
    )

    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            break
            

        frame = myDetector.prepare_frame(frame)
        people = myDetector.detect_people(frame)


        for person in people.values():
            draw_arms(frame, person, myDetector.conf_threshold)

        # Draw status last so joint labels cannot cover it.
        for row, person in enumerate(people.values()):
            draw_arm_status(frame, person, row)
        cv2.imshow("arm demo",frame)
        
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
