"""Pretrained Ultralytics YOLO RGB perception adapter for person and vehicle detection.

Converts RGB NumPy arrays to BGR for Ultralytics YOLO inference, validates local weights
paths without automatic downloading, measures per-frame inference latency, filters and maps
detected classes to canonical contracts.Detection objects ('person' and 'vehicle'), and validates
all bounding box bounds and confidence values.
"""
from __future__ import annotations

import math
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from crossguard.contracts import Detection

# Canonical label mapping from standard COCO class names to CrossGuard contract labels.
DEFAULT_LABEL_MAPPING: Dict[str, str] = {
    "person": "person",
    "pedestrian": "person",
    "car": "vehicle",
    "truck": "vehicle",
    "bus": "vehicle",
    "motorcycle": "vehicle",
    "bicycle": "vehicle",
    "vehicle": "vehicle",
}


def _to_list(val: Any) -> list:
    """Helper to convert PyTorch Tensors, NumPy arrays, or iterables to Python lists."""
    if hasattr(val, "cpu"):
        val = val.cpu()
    if hasattr(val, "numpy"):
        val = val.numpy()
    if hasattr(val, "tolist"):
        return val.tolist()
    return list(val)


class YoloDetector:
    """Pretrained Ultralytics YOLO RGB adapter returning contracts.Detection.

    Parameters
    ----------
    weights : str or Path or None
        Path to local pretrained YOLO weights file (e.g. 'yolov8n.pt'). Auto-download
        is strictly disabled; the file must exist on the local filesystem unless an
        injected model is provided via the `model` parameter.
    confidence : float, default 0.3
        Confidence threshold in (0.0, 1.0]. Detections with confidence below this
        threshold are filtered out.
    device : str, default 'cpu'
        Target compute device for inference (e.g. 'cpu', 'cuda:0').
    model : Any, optional (keyword-only)
        Injected pre-instantiated YOLO model or mock for testing/dependency-free execution.
        When provided, weights file existence check and Ultralytics library loading are
        bypassed, allowing tests to run in environments without GPU or torch/ultralytics.
    model_factory : Callable[[str], Any], optional (keyword-only)
        Optional factory callable taking weights path string and returning a model instance.
    class_mapping : dict[str, str], optional (keyword-only)
        Custom dictionary mapping normalized model class names to canonical label strings.
        Defaults to mapping COCO person and vehicle classes to 'person' and 'vehicle'.
    """

    def __init__(
        self,
        weights: Union[str, Path, None],
        confidence: float = 0.3,
        device: str = "cpu",
        *,
        model: Optional[Any] = None,
        model_factory: Optional[Callable[[str], Any]] = None,
        class_mapping: Optional[Dict[str, str]] = None,
    ) -> None:
        if not math.isfinite(confidence) or not 0.0 < confidence <= 1.0:
            raise ValueError(f"Confidence must be a finite float in (0.0, 1.0], got {confidence}")
        if not isinstance(device, str) or not device.strip():
            raise ValueError(f"device must be a non-empty string, got {device!r}")

        self.confidence: float = float(confidence)
        self.device: str = device.strip()
        self.last_latency_s: Optional[float] = None
        self._mode: str = "rgb"

        # Model instantiation or dependency-free injection
        if model is not None:
            self.weights: Optional[Path] = Path(weights) if weights is not None else None
            self.model: Any = model
        elif model_factory is not None:
            if weights is None:
                raise ValueError("weights path must be provided when using model_factory")
            path = Path(weights)
            if not path.is_file():
                raise FileNotFoundError(
                    f"Pretrained YOLO weights file not found: {weights}. "
                    "Auto-downloading weights is disabled; provide an existing local weights file."
                )
            self.weights = path
            self.model = model_factory(str(path))
        else:
            if weights is None:
                raise ValueError("weights path must be provided when model is not injected")
            path = Path(weights)
            if not path.is_file():
                raise FileNotFoundError(
                    f"Pretrained YOLO weights file not found: {weights}. "
                    "Auto-downloading weights is disabled; provide an existing local weights file."
                )
            self.weights = path

            try:
                from ultralytics import YOLO  # type: ignore
            except ImportError as err:
                raise ImportError(
                    "The 'ultralytics' package is required for YoloDetector RGB inference. "
                    "Install optional perception dependencies (e.g. ultralytics, torch) first."
                ) from err

            self.model = YOLO(str(path))

        # Inspect model class names and configure mapping
        mapping = class_mapping if class_mapping is not None else DEFAULT_LABEL_MAPPING
        self._class_to_label: Dict[int, str] = {}

        if not hasattr(self.model, "names") or self.model.names is None:
            raise ValueError("The supplied detector model does not expose class names via .names")

        names = self.model.names
        if isinstance(names, (list, tuple)):
            names_dict = dict(enumerate(names))
        elif isinstance(names, dict):
            names_dict = names
        else:
            raise ValueError(f"Model names must be a dict or list, got {type(names).__name__}")

        for cls_id, name in names_dict.items():
            norm_name = str(name).strip().lower()
            if norm_name in mapping:
                self._class_to_label[int(cls_id)] = mapping[norm_name]

        if not self._class_to_label:
            raise ValueError(
                f"The supplied detector does not expose any person or vehicle classes. "
                f"Available model classes: {list(names_dict.values())}"
            )

        self._target_classes: List[int] = sorted(self._class_to_label.keys())

    @property
    def mode(self) -> str:
        """Perception mode ('rgb')."""
        return self._mode

    @property
    def perception_mode(self) -> str:
        """Contract perception mode identifier ('rgb')."""
        return self._mode

    @property
    def model_mode(self) -> str:
        """Model mode ('rgb')."""
        return self._mode

    def detect(self, rgb_array: np.ndarray) -> List[Detection]:
        """Detect persons and vehicles in an HxWx3 RGB uint8 NumPy array.

        Parameters
        ----------
        rgb_array : np.ndarray
            HxWx3 RGB image array with uint8 dtype.

        Returns
        -------
        list[contracts.Detection]
            List of validated detections with bounding box, confidence, and canonical label.

        Raises
        ------
        TypeError
            If rgb_array is not a NumPy ndarray.
        ValueError
            If rgb_array has incorrect dimensions, shape, dtype, or invalid bounds.
        RuntimeError
            If model inference fails or returns invalid/missing output.
        """
        if not isinstance(rgb_array, np.ndarray):
            raise TypeError(f"rgb_array must be a numpy.ndarray, got {type(rgb_array).__name__}")
        if rgb_array.dtype != np.uint8:
            raise ValueError(f"rgb_array must have dtype uint8, got {rgb_array.dtype}")
        if rgb_array.ndim != 3:
            raise ValueError(f"rgb_array must be 3-dimensional (H, W, 3), got shape {rgb_array.shape}")
        height, width, channels = rgb_array.shape
        if channels != 3:
            raise ValueError(f"rgb_array must have 3 colour channels, got {channels} in shape {rgb_array.shape}")
        if height <= 0 or width <= 0:
            raise ValueError(f"rgb_array dimensions must be positive, got {rgb_array.shape}")

        # Ultralytics numpy input expects BGR image array (channel order reversed).
        # We explicitly convert RGB to BGR contiguous array before passing to predict.
        bgr_array = np.ascontiguousarray(rgb_array[:, :, ::-1])

        start_time = time.perf_counter()
        try:
            if hasattr(self.model, "predict"):
                results = self.model.predict(
                    source=bgr_array,
                    classes=self._target_classes,
                    conf=self.confidence,
                    device=self.device,
                    verbose=False,
                )
            else:
                results = self.model(
                    bgr_array,
                    classes=self._target_classes,
                    conf=self.confidence,
                    device=self.device,
                )
        finally:
            self.last_latency_s = time.perf_counter() - start_time

        if results is None:
            raise RuntimeError("Model inference returned None instead of results")

        detections: List[Detection] = []
        for result in results:
            if result is None:
                raise RuntimeError("Model returned None for an inference result")
            if not hasattr(result, "boxes"):
                raise ValueError(
                    f"Model result missing required 'boxes' attribute: {type(result).__name__}"
                )
            if result.boxes is None:
                continue

            boxes_obj = result.boxes
            if (
                not hasattr(boxes_obj, "xyxy")
                or not hasattr(boxes_obj, "conf")
                or not hasattr(boxes_obj, "cls")
            ):
                raise ValueError("Model result boxes missing required xyxy, conf, or cls attributes")

            xyxy_raw = _to_list(boxes_obj.xyxy)
            conf_raw = _to_list(boxes_obj.conf)
            cls_raw = _to_list(boxes_obj.cls)

            if not (len(xyxy_raw) == len(conf_raw) == len(cls_raw)):
                raise ValueError(
                    f"Mismatched box attribute lengths: xyxy={len(xyxy_raw)}, conf={len(conf_raw)}, cls={len(cls_raw)}"
                )

            for box, conf_val, cls_val in zip(xyxy_raw, conf_raw, cls_raw):
                # Validate confidence
                try:
                    conf_float = float(conf_val)
                except (TypeError, ValueError) as err:
                    raise ValueError(f"Invalid non-numeric confidence value: {conf_val!r}") from err

                if not math.isfinite(conf_float) or not 0.0 <= conf_float <= 1.0:
                    raise ValueError(f"Detection confidence must be finite and within [0, 1], got {conf_float}")

                # Confidence threshold filtering
                if conf_float < self.confidence:
                    continue

                # Class filtering and label mapping
                try:
                    cls_int = int(cls_val)
                except (TypeError, ValueError) as err:
                    raise ValueError(f"Invalid non-integer class id: {cls_val!r}") from err

                if cls_int not in self._class_to_label:
                    continue
                label = self._class_to_label[cls_int]

                # Validate bounding box coordinates
                if not isinstance(box, (list, tuple)) or len(box) != 4:
                    raise ValueError(f"Bounding box must contain 4 coordinates, got {box!r}")

                try:
                    x1, y1, x2, y2 = (float(coord) for coord in box)
                except (TypeError, ValueError) as err:
                    raise ValueError(f"Bounding box coordinates must be numeric, got {box!r}") from err

                if not all(math.isfinite(c) for c in (x1, y1, x2, y2)):
                    raise ValueError(f"Bounding box contains non-finite coordinates: {(x1, y1, x2, y2)}")

                # Check degenerate box geometry
                if x1 >= x2 or y1 >= y2:
                    raise ValueError(
                        f"Degenerate bounding box with non-positive dimensions: {(x1, y1, x2, y2)}"
                    )

                # Validate bounds against image dimensions
                # Tolerance of 1e-2 allows for minor floating-point rounding from coordinate transforms
                eps = 1e-2
                if x1 < -eps or y1 < -eps or x2 > width + eps or y2 > height + eps:
                    raise ValueError(
                        f"Bounding box {(x1, y1, x2, y2)} exceeds image bounds (width={width}, height={height})"
                    )

                # Clamp minor floating-point overruns to exact image limits
                x1_clamped = max(0.0, min(float(width), x1))
                y1_clamped = max(0.0, min(float(height), y1))
                x2_clamped = max(0.0, min(float(width), x2))
                y2_clamped = max(0.0, min(float(height), y2))

                if x1_clamped >= x2_clamped or y1_clamped >= y2_clamped:
                    raise ValueError(
                        f"Bounding box {(x1, y1, x2, y2)} collapsed after boundary clamping"
                    )

                detections.append(
                    Detection(
                        bbox=(x1_clamped, y1_clamped, x2_clamped, y2_clamped),
                        confidence=conf_float,
                        label=label,
                    )
                )

        return detections
