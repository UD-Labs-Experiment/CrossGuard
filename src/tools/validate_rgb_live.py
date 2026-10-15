import json,time
from pathlib import Path
from dataclasses import asdict
import cv2
from crossguard.simulation.city_world import CityWorld

root=Path(__file__).resolve().parents[1]/"results"/"rgb_live_validation"
root.mkdir(parents=True,exist_ok=True)
city=None
try:
 city=CityWorld(vehicles=12,walkers=20,resolution=(960,540),perception_mode="rgb",progress=lambda m:print(m,flush=True))
 start=time.monotonic(); count=0; saved=set(); next_status=0
 while not city.finished:
  frames=city.tick();count+=1
  if city.time>=next_status:
   state=city.metrics();print(json.dumps({k:state.get(k) for k in ["sim_time_s","frame_id","speed_kmh","decision_state","detector_count","inference_latency_ms","source_detection_counts","pipeline_completed_batches"]}),flush=True)
   next_status+=3
  pipeline=city.pipeline.snapshot()
  if pipeline["completed_batches"]>=4:
   streams={"driver":city.driver_seg,"bus":city.bus_seg,"rsu":city.rsu_seg}
   for source,stream in streams.items():
    if source in saved or stream is None:continue
    # Save the exact source image only when its detection frame matches.
    info=pipeline["source_frames"].get(source,{})
    if source not in pipeline.get("preview_images",{}):continue
    image=pipeline["preview_images"][source].copy()
    for detection in pipeline["detections"].get(source,[]):
     x1,y1,x2,y2=map(int,detection["bbox"])
     cv2.rectangle(image,(x1,y1),(x2,y2),(255,255,255),1)
     cv2.putText(image,detection["label"],(x1,max(14,y1)),cv2.FONT_HERSHEY_SIMPLEX,.4,(255,255,255),1)
    cv2.imwrite(str(root/f"{source}_yolo.png"),cv2.cvtColor(image,cv2.COLOR_RGB2BGR));saved.add(source)
 state=city.metrics();state["wall_camera_fps"]=round(count/(time.monotonic()-start),2)
 state["run_directory"]=str(city.output)
 state["rgb_observations"]={k:[asdict(o) for o in v] for k,v in city.pipeline.snapshot()["observations"].items()}
 (root/"summary.json").write_text(json.dumps(state,indent=2))
 print('COMPLETE',json.dumps(state),flush=True)
finally:
 if city:city.close()
