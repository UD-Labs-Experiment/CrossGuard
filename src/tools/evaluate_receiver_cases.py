"""Live four-case smoke evaluation; results are not a statistical safety study."""
import json
from pathlib import Path
import time
from PIL import Image
from crossguard.simulation.city_world import CityWorld

output = Path(__file__).resolve().parents[1] / "results" / "receiver_case_evaluation"
output.mkdir(parents=True, exist_ok=True)
results = []
for mode in ("autonomous", "retrofit"):
    for enabled in (True, False):
        print(f"START {mode} enabled={enabled}", flush=True)
        city = None
        try:
            city = CityWorld(vehicles=12, walkers=20, receiver_mode=mode,
                             enabled=enabled, resolution=(1280,720), seed=7,
                             progress=lambda message: print(message, flush=True))
            start = time.monotonic()
            frames = 0
            captured = set()
            while not city.finished:
                view = ("top", "driver", "person", "opposite")[min(3, int(city.time / 3))]
                city.set_view(view)
                images = city.tick()
                frames += 1
                if view not in captured and images.get(view) is not None:
                    Image.fromarray(images[view]).save(output / f"{mode}_{'with' if enabled else 'without'}_{view}.png")
                    captured.add(view)
            notifications = [e for e in city.messages if e.get("channel") == "simulated_audio"]
            record = {**city.metrics(), "receiver_mode":mode,
                      "wall_camera_fps":round(frames/(time.monotonic()-start),2),
                      "notifications":notifications,
                      "run_directory":str(city.output)}
            results.append(record)
            (output / "summary.json").write_text(json.dumps(results, indent=2))
            print(f"DONE {mode} enabled={enabled} pedestrian_collision={record['pedestrian_collision']} other_collisions={len(record['other_collisions'])} FPS={record['wall_camera_fps']}",flush=True)
        finally:
            if city is not None:
                city.close()
