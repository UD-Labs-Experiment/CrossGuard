"""Bounded async RGB inference; observations retain image capture time/pose.

Only latest pending image batch is retained. Display/physics do not wait for
inference; dropped inference jobs are counted. No simulator actor truth enters
this module. Errors are surfaced to the simulation owner.
"""
from dataclasses import asdict
from crossguard.provenance import register_source
register_source(__name__,__file__)
import queue
import threading
import time
from .tracking import GroundTracker


class RGBPipeline:
    def __init__(self, detector):
        self.detector = detector
        self.pending = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.trackers = {}
        self.result = {"observations": {}, "detections": {}, "source_detection_counts": {},
                       "source_frames": {}, "inference_latency_ms": None,
                       "preview_images": {}, "completed_batches": 0, "dropped_batches": 0, "error": None}
        self.thread = threading.Thread(target=self._run, name="rgb-yolo", daemon=True)
        self.thread.start()

    def submit(self, samples):
        try:
            self.pending.put_nowait(samples)
        except queue.Full:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            with self.lock:
                self.result["dropped_batches"] += 1
            self.pending.put_nowait(samples)

    def snapshot(self):
        with self.lock:
            return {**self.result, "observations": dict(self.result["observations"]),
                    "detections": dict(self.result["detections"]),
                    "source_detection_counts": dict(self.result["source_detection_counts"]),
                    "source_frames": dict(self.result["source_frames"])}

    def _run(self):
        while not self.stopping.is_set():
            try:
                samples = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            begin = time.perf_counter()
            observations, detections, counts, frames = {}, {}, {}, {}
            try:
                for source, sample in samples.items():
                    boxes = self.detector.detect(sample["rgb"])
                    tracker = self.trackers.setdefault(source, GroundTracker(source, velocity_window_s=0.6))
                    observations[source] = tracker.update(
                        boxes, sample["size"], sample["fov"], sample["transform"],
                        sample["timestamp_s"], sample["frame_id"], sample["ground_z"])
                    detections[source] = [asdict(box) for box in boxes]
                    counts[source] = len(boxes)
                    frames[source] = {"frame_id": sample["frame_id"],
                                      "captured_at_s": sample["timestamp_s"],
                                      "ground_tracks": len(observations[source])}
                with self.lock:
                    self.result.update(observations=observations, detections=detections,
                                       source_detection_counts=counts, source_frames=frames,
                                       preview_images={k:v["rgb"] for k,v in samples.items()},
                                       inference_latency_ms=(time.perf_counter()-begin)*1000)
                    self.result["completed_batches"] += 1
            except Exception as exc:
                with self.lock:
                    self.result["error"] = f"{type(exc).__name__}: {exc}"
                return

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("RGB worker did not shut down")
