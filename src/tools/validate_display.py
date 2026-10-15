import asyncio,json,base64,io
from pathlib import Path
import httpx,websockets
from PIL import Image
async def main():
 out=Path('src/results/display_refinement');out.mkdir(parents=True,exist_ok=True)
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8000',timeout=120) as http:
  async def command(a,**p):
   r=await http.post('/api/command',json={'action':a,'params':p});r.raise_for_status();return r.json()
  await command('start',enabled=True)
  try:
   await command('display',mode='yolo');await command('view',name='opposite')
   async with websockets.connect('ws://127.0.0.1:8000/ws',max_size=5000000) as ws:
    for _ in range(150):
     p=json.loads(await asyncio.wait_for(ws.recv(),20))
     if p.get('type')!='frame' or p.get('image_mode')!='yolo' or not p.get('image'):continue
     m=p['metrics'];src=m['source_frames'].get('view_opposite')
     if not src:continue
     assert p['image_frame_id']==src['frame_id']
     assert p['image_capture_time_s']==src['captured_at_s']
     assert p['image_frame_id']<=p['frame_id']
     assert m['device_mount']['type']=='traffic_light'
     data=base64.b64decode(p['image'].split(',',1)[1]);size=Image.open(io.BytesIO(data)).size;assert size==(1920,1080),size
     (out/'opposite_yolo.jpg').write_bytes(data)
     (out/'evidence.json').write_text(json.dumps({'image_frame_id':p['image_frame_id'],'telemetry_frame_id':p['frame_id'],'capture_time_s':p['image_capture_time_s'],'resolution':size,'mount':m['device_mount'],'detections':m['rgb_detections']['view_opposite'],'gpu':m['inference_gpu']},indent=2))
     print('MATCHED YOLO PREVIEW',size,m['device_mount'],len(m['rgb_detections']['view_opposite']),flush=True);break
    else:raise AssertionError('No YOLO preview delivered')
   r=await command('display',mode='rgb');assert r['acknowledged_state']['display_mode']=='rgb'
  finally:await command('stop')
asyncio.run(main())
