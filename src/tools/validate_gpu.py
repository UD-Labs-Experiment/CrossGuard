import json,time,statistics
from pathlib import Path
import numpy as np
from PIL import Image
from crossguard.perception.onnx_detection import OnnxYoloDetector
out=Path('src/results/gpu_validation');out.mkdir(parents=True,exist_ok=True)
image=np.array(Image.open('src/results/rgb_live_validation/bus_yolo.png').convert('RGB'))
results={}
for device in ['cpu','gpu']:
 detector=OnnxYoloDetector('src/models/yolo11n.onnx',device=device,profile_prefix=out/device)
 detector.detect(image);timings=[]
 for _ in range(10):
  t=time.perf_counter();detections=detector.detect(image);timings.append((time.perf_counter()-t)*1000)
 profile=Path(detector.session.end_profiling());events=json.loads(profile.read_text())
 providers={}
 for e in events:
  p=e.get('args',{}).get('provider')
  if p:providers[p]=providers.get(p,0)+1
 results[device]={'backend':detector.backend,'adapter':detector.adapter,'median_ms':statistics.median(timings),'detections':len(detections),'providers_executed':providers,'profile':str(profile)}
 print(json.dumps(results[device]),flush=True)
 if device=='gpu':assert providers.get('DmlExecutionProvider',0)>0
(out/'benchmark.json').write_text(json.dumps(results,indent=2))
