"""Actual synchronized RGB evidence; labels never substitute missing images."""
import json
from pathlib import Path
from PIL import Image, ImageDraw

class SixViewRecorder:
    def __init__(self, output, start_time, labels=None, assets_output=None):
        self.run_output=Path(output)
        self.output=Path(assets_output or output)/'pictures'
        self.output.mkdir(parents=True,exist_ok=True)
        self.start_time=start_time
        self.labels=labels or {}
        self.last=-1.
        self.manifest=[]

    def capture(self,time_s,frame,views,force=False,marker=None):
        if not force and time_s-self.last < .1-1e-6:
            return
        self.last=time_s
        entry={'frame':frame,'time_s':time_s,'simulation_timestamp_s':self.start_time+time_s,
               'marker':marker,'views':{},'missing':[]}
        for name,stream in views.items():
            rgb=stream.rgb()
            if rgb is None or stream.last_frame != frame:
                entry['missing'].append(name)
                continue
            image=Image.fromarray(rgb)
            draw=ImageDraw.Draw(image)
            label=' | '.join(str(self.labels.get(k,'unlabelled')) for k in ('test','mode','assistance','seed'))
            draw.rectangle((0,0,image.width,45),fill=(12,15,19))
            draw.text((8,4),label,fill='white')
            draw.text((8,24),f'{name} | frame {frame} | sim {stream.last_timestamp:.4f}s | trial {time_s:.4f}s',fill='white')
            filename=f'{frame:07d}_{name}.jpg'
            image.save(self.output/filename,quality=92)
            entry['views'][name]=filename
        self.manifest.append(entry)
        (self.run_output/'camera_manifest.json').write_text(json.dumps(self.manifest,indent=2))

class SourceCameraRecorder:
    def __init__(self,output,start_time):
        self.output=Path(output)
        self.rows=[]
    def capture(self,time_s,sources,pedestrian_id,location,crossing_at):
        self.rows.append({'time_s':time_s,'pedestrian_id':pedestrian_id,'evaluator_location':location,
            'crossing_at':crossing_at,'frames':{k:{'frame':s.last_frame,'timestamp_s':s.last_timestamp}
                                             for k,s in sources.items()}})
        (self.output/'source_camera_frames.json').write_text(json.dumps(self.rows,indent=2))
