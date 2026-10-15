"""Live bridge protocol validation; does not automate or render a browser."""
import asyncio,json,time,sys,base64
from pathlib import Path
import httpx,websockets
ROOT=Path(__file__).resolve().parents[1]/'results'/'web_live_validation'
ROOT.mkdir(parents=True,exist_ok=True)
async def main():
 reports=[]
 cases=[('retrofit',True)] if '--retrofit-gpu' in sys.argv else ([('autonomous',False)] if '--autonomous-baseline' in sys.argv else [('autonomous',True),('retrofit',True),('retrofit',False)])
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8000',timeout=120) as http:
  async def command(action,**params):
   r=await http.post('/api/command',json={'action':action,'params':params});r.raise_for_status();return r.json()
  await command('stop')
  async with websockets.connect('ws://127.0.0.1:8000/ws',max_size=4000000) as ws:
   for mode,enabled in cases:
    if not reports:
     ack=await command('start',receiver_mode=mode,enabled=enabled)
    else:
     await command('pause')
     await command('receiver',mode=mode)
     ack=await command('system',enabled=enabled)
     await command('resume')
    trial=ack['acknowledged_state']['trial_id']
    if '--autonomous-baseline' in sys.argv:
     cameras=[]
     for view in ['top','driver','person','opposite']:
      await command('view',name=view)
      while True:
       msg=json.loads(await asyncio.wait_for(ws.recv(),20))
       if msg.get('type')=='frame' and msg.get('view')==view and msg.get('image'):
        (ROOT/f'{view}.jpg').write_bytes(base64.b64decode(msg['image'].split(',',1)[1]))
        cameras.append({'view':view,'frame_id':msg['frame_id'],'sim_time_s':msg['sim_time_s']});break
     await command('pause')
     before=(await http.get('/api/status')).json()['sim_time_s']
     await asyncio.sleep(.3)
     after=(await http.get('/api/status')).json()['sim_time_s']
     assert before==after
     (ROOT/'camera_pause.json').write_text(json.dumps({'cameras':cameras,'pause_before':before,'pause_after':after},indent=2))
     await command('resume')
    start=time.monotonic();last=None;events={};frames=0
    while time.monotonic()-start<180:
     msg=json.loads(await asyncio.wait_for(ws.recv(),20))
     status=msg.get('status',{})
     if status.get('trial_id')!=trial:
      if status.get('trial_id',0)>trial:raise RuntimeError('Manual case/replay change interrupted validation')
      continue
     if status.get('state')=='error':raise RuntimeError(status)
     if msg['type']=='frame':
      last=msg;frames+=1;m=msg['metrics']
      assert m['receiver_mode']==mode
      assert status['system_enabled']==enabled
      assert m['perception_mode']=='rgb'
      for source in m.get('source_frames',{}).values():
       assert source['frame_id']<=m['frame_id']
       assert source['captured_at_s']<=msg['sim_time_s']+.001
      for e in msg.get('events',[]):
       if e.get('event')=='notification_delivered':events[str(e.get('event_id'))]=e
     if status.get('state')=='finished':
      if status.get('terminal_metrics') and last:last['metrics']=status['terminal_metrics']
      break
    assert last and last['metrics'].get('end_reason')=='route_exit',last and last['metrics']
    report={'receiver_mode':mode,'enabled':enabled,'trial_id':trial,'frames_received':frames,'wall_s':time.monotonic()-start,'metrics':last['metrics'],'notification_events':list(events.values())}
    reports.append(report)
    (ROOT/('retrofit_gpu.json' if '--retrofit-gpu' in sys.argv else 'autonomous_baseline.json' if '--autonomous-baseline' in sys.argv else 'summary.json')).write_text(json.dumps(reports,indent=2))
    print(json.dumps({k:report[k] for k in ['receiver_mode','enabled','trial_id','frames_received','wall_s','notification_events']}),flush=True)
   await command('stop')
asyncio.run(main())
