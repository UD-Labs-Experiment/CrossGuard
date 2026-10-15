"""YOLO11 COCO ONNX inference. Does not import PyTorch or simulator APIs."""
from pathlib import Path
import time
import numpy as np
from crossguard.contracts import Detection


class OnnxYoloDetector:
    def __init__(self, weights, confidence=0.35, device="gpu", profile_prefix=None):
        import cv2
        import onnxruntime as ort
        if device not in ("cpu", "gpu", "dml"):
            raise ValueError("ONNX device must be gpu/dml (DirectML) or cpu")
        self.adapter = None
        self.backend = "onnxruntime_cpu"
        providers = ["CPUExecutionProvider"]
        if device != "cpu":
            if "DmlExecutionProvider" not in ort.get_available_providers():
                raise RuntimeError("GPU requested but DirectML is unavailable; install onnxruntime-directml")
            from crossguard.perception.gpu import nvidia_adapter
            self.adapter = nvidia_adapter()
            self.backend = "onnxruntime_directml_gpu"
            providers = [("DmlExecutionProvider", {"device_id": self.adapter["device_id"]}), "CPUExecutionProvider"]
        if not Path(weights).is_file():
            raise FileNotFoundError(weights)
        self.confidence = confidence
        self.last_latency_s = 0.0
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        if device != "cpu":
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        if profile_prefix:
            options.enable_profiling = True
            options.profile_file_prefix = str(profile_prefix)
        self.session = ort.InferenceSession(str(weights), sess_options=options, providers=providers)
        self.session.disable_fallback()
        if device != "cpu" and "DmlExecutionProvider" not in self.session.get_providers():
            raise RuntimeError("DirectML initialization failed; refusing CPU-only inference")
        self.input = self.session.get_inputs()[0]
        if self.input.shape != [1, 3, 640, 640]:
            raise ValueError("Expected YOLO11n 640x640 float32 ONNX input")
        self.cv2 = cv2
        cv2.setNumThreads(1)

    def detect(self, rgb):
        if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError("RGB inference expects HWC uint8")
        begin = time.perf_counter()
        height, width = rgb.shape[:2]
        scale = min(640 / width, 640 / height)
        resized_width, resized_height = round(width * scale), round(height * scale)
        xpad, ypad = (640 - resized_width) // 2, (640 - resized_height) // 2
        image = np.full((640, 640, 3), 114, dtype=np.uint8)
        image[ypad:ypad + resized_height, xpad:xpad + resized_width] = self.cv2.resize(
            rgb, (resized_width, resized_height))
        tensor = np.ascontiguousarray(image.transpose(2, 0, 1)[None], dtype=np.float32) / 255
        output = self.session.run(None, {self.input.name: tensor})[0]
        detections = self.decode(output, scale, xpad, ypad, width, height)
        self.last_latency_s = time.perf_counter() - begin
        return detections

    def decode(self, output, scale, xpad, ypad, width, height):
        if output.shape != (1, 84, 8400) or not np.isfinite(output).all():
            raise ValueError("Unexpected YOLO11 output tensor")
        rows = output[0].T
        classes = rows[:, 4:].argmax(axis=1)
        scores = rows[np.arange(len(rows)), classes + 4]
        mapping = {0: "person", 1: "vehicle", 2: "vehicle", 3: "vehicle",
                   5: "vehicle", 7: "vehicle"}
        selected = np.flatnonzero((scores >= self.confidence) & np.isin(classes, list(mapping)))
        result = []
        # Class-specific NMS: nearby people must not suppress vehicle boxes.
        for cls in sorted(set(classes[selected])):
            indices = selected[classes[selected] == cls]
            boxes, confidences = [], []
            for index in indices:
                cx, cy, w, h = rows[index, :4]
                boxes.append([float(cx - w / 2), float(cy - h / 2), float(w), float(h)])
                confidences.append(float(scores[index]))
            kept = self.cv2.dnn.NMSBoxes(boxes, confidences, self.confidence, 0.45)
            for local in np.asarray(kept).reshape(-1):
                x, y, w, h = boxes[int(local)]
                bbox = (max(0.0, (x - xpad) / scale), max(0.0, (y - ypad) / scale),
                        min(float(width), (x + w - xpad) / scale),
                        min(float(height), (y + h - ypad) / scale))
                if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                    result.append(Detection(bbox, confidences[int(local)], mapping[int(cls)]))
        return result
