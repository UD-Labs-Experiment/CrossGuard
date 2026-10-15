"""Offline actual-RGB evidence inspection and fully decoded comparison videos."""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from town10_continuation import ROOT, MODES, STATES, read, save, verified_catalogue

VIEWS=('top','driver','person','opposite','driver_b','driver_c')


def complete_sets(run):
    manifest=read(run/'camera_manifest.json')
    complete=[m for m in manifest if not m.get('missing') and all(v in m['views'] for v in VIEWS)]
    if not complete:
        raise RuntimeError('No complete synchronized six-view set: '+str(run))
    return complete


def panel(run,entry,cfg,mode,state):
    from PIL import Image, ImageDraw, ImageFont
    font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',20)
    small=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',16)
    canvas=Image.new('RGB',(1280,1210),(15,18,22));draw=ImageDraw.Draw(canvas)
    draw.text((16,12),f'{cfg["readable_name"]} | {mode} | Assistance {state} | seed {cfg["seed"]}',font=font,fill='white')
    draw.text((16,42),f'frame {entry["frame"]} | trial {entry["time_s"]:.3f}s | simulation {entry["simulation_timestamp_s"]:.3f}s',font=small,fill='white')
    for i,view in enumerate(VIEWS):
        path=run/'assets/pictures'/entry['views'][view]
        with Image.open(path) as picture:
            picture.load();picture=picture.convert('RGB').resize((640,360))
        x=(i%2)*640;y=80+(i//2)*370
        canvas.paste(picture,(x,y));draw.rectangle((x,y,x+640,y+28),fill='black')
        title={'top':'Overhead','person':'Pedestrian','driver':'Driver A'}.get(view,view.replace('_',' ').title())
        draw.text((x+8,y+3),title,font=font,fill='white')
    draw.text((16,1190),'Actual recorded RGB. Encounter qualification is reported separately.',font=small,fill='#ffcb80')
    return canvas


def pictures(root,test):
    from PIL import Image
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    cfg=next(c for c in verified_catalogue(root)['tests'] if c['test']==test)
    folder=root/cfg['relative_path'];checks=[]
    for mode in MODES:
        for state in STATES:
            run=folder/mode/f'Assistance_{state}'
            if not (run/'results.json').exists():continue
            rows=read(run/'physical_timeline.json');result=read(run/'results.json')
            frames={r['frame'] for r in rows};sets=complete_sets(run);checked=0;dimensions={}
            for entry in sets:
                if entry['frame'] not in frames:
                    # Terminal capture may correspond to the following world frame.
                    if abs(entry['time_s']-rows[-1]['time_s'])>1/60+.00001:
                        raise RuntimeError('RGB/physics frame alignment missing')
                for name in VIEWS:
                    with Image.open(run/'assets/pictures'/entry['views'][name]) as image:
                        image.load()
                        if min(image.size)<=0:raise RuntimeError('Invalid RGB dimensions')
                        if name in dimensions and dimensions[name]!=image.size:
                            raise RuntimeError('RGB dimensions changed within recorded view')
                        dimensions[name]=image.size
                    checked+=1
            times={r['frame']:r['time_s'] for r in rows}
            contacts=result.get('pedestrian_contacts',[])+[e for p in result['secondary'].values() for e in p['events']]
            contact=min((times[e['frame']] for e in contacts if e['frame'] in times),default=None)
            trigger=(result.get('encounter_timing') or {}).get('trigger')
            movement=trigger['at_s'] if trigger else None
            brake=result['timeline']['hero_car']['first_brake_command_s']
            markers={'initial_scene':0.,'before_emergence':max(0.,(movement or 1.)-.2),
                     'during_braking':(brake or movement or 1.)+.2,
                     'outcome':contact if contact is not None else rows[-1]['time_s'],
                     'terminal':rows[-1]['time_s']}
            if cfg['pedestrian_behavior']=='wait':markers['approach_opportunity']=5.
            if cfg['pedestrian_behavior']=='retreat' and movement is not None:
                markers['forward_walk']=movement+.8
                markers['after_reversal']=movement+2.
            selected=[]
            for name,t in markers.items():
                entry=min(sets,key=lambda m:abs(m['time_s']-t))
                destination=run/'assets'/f'{name}_six_views.jpg'
                panel(run,entry,cfg,mode,state).save(destination,quality=95)
                selected.append({'marker':name,'requested_trial_s':t,'source':entry,'image':str(destination)})
            save(run/'assets/picture_verification.json',{'decoded_images':checked,'source_dimensions':dimensions,
                'complete_synchronized_sets':len(sets),'first_trial_s':sets[0]['time_s'],
                'last_trial_s':sets[-1]['time_s'],'selected_markers':selected,
                'actual_rgb_inspected_by_codex':False})
            figure,axes=plt.subplots(4,1,figsize=(12,10),sharex=True)
            ts=[r['time_s'] for r in rows]
            for name,label in (('hero_car','A'),('follower_b','B'),('follower_c','C')):
                for axis,key in zip(axes[:3],('speed_mps','acceleration_mps2','brake')):
                    axis.plot(ts,[r['vehicles'][name][key] for r in rows],label=label)
            for name,label in (('follower_b','A/B'),('follower_c','B/C')):
                axes[3].plot(ts,[r['vehicles'][name]['gap_m'] for r in rows],label=label)
            for axis,label in zip(axes,('Speed m/s','Acceleration m/s²','Brake command','Physical bumper gap m')):
                axis.set_ylabel(label);axis.grid(alpha=.3);axis.legend(ncol=3)
            axes[-1].set_xlabel('Simulation time since launch (s)')
            figure.suptitle(f'{cfg["readable_name"]} | {mode} | Assistance {state} | seed {cfg["seed"]}')
            figure.tight_layout();figure.savefig(run/'assets/braking_timeline.png',dpi=140);plt.close(figure)
            checks.append({'mode':mode,'assistance':state,'decoded_images':checked,'sets':len(sets)})
    save(folder/'picture_checks.json',checks)
    print(json.dumps(checks,indent=2))


def videos(root,test,selected_mode=None,allow_unreviewed_export=False):
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    cfg=next(c for c in verified_catalogue(root)['tests'] if c['test']==test)
    folder=root/cfg['relative_path']
    for mode in (selected_mode,) if selected_mode else MODES:
        review_path=folder/mode/'scene_review.json'
        review=read(review_path) if review_path.exists() else {}
        reviewed=review.get('actual_rgb_inspected') is True
        if not reviewed and not allow_unreviewed_export:
            raise RuntimeError('Inspect actual pictures before generating videos')
        runs={s:folder/mode/f'Assistance_{s}' for s in STATES}
        manifests={s:complete_sets(runs[s]) for s in STATES}
        # Use recorded relative timestamps, never global frame numbers across reloads.
        coverage=min(manifests[s][-1]['time_s'] for s in STATES)
        if coverage<20.-1/60-.00001:raise RuntimeError('Incomplete source coverage')
        target=folder/mode/'Comparison'/f'{mode}_Assistance_OFF_vs_ON.mp4'
        outputs={s:runs[s]/'assets/videos'/f'{mode}_Assistance_{s}.mp4' for s in STATES}
        outputs['Comparison']=target
        temporary={key:path.with_name(path.stem+'.encoding.mp4') for key,path in outputs.items()}
        for path in outputs.values():path.parent.mkdir(parents=True,exist_ok=True)
        writers={key:cv2.VideoWriter(str(temporary[key]),cv2.VideoWriter_fourcc(*'mp4v'),10,
            (2560 if key=='Comparison' else 1280,1210)) for key,path in outputs.items()}
        if not all(w.isOpened() for w in writers.values()):
            for w in writers.values():w.release()
            raise RuntimeError('Video writer failed')
        sources=[]
        try:
            for index in range(201):
                t=index/10.;entries={s:min(manifests[s],key=lambda e:abs(e['time_s']-t)) for s in STATES}
                if any(abs(e['time_s']-t)>.051 for e in entries.values()):
                    raise RuntimeError('Recorded source alignment exceeds half output frame')
                panels={s:np.asarray(panel(runs[s],entries[s],cfg,mode,s)) for s in STATES}
                frame=np.concatenate([panels[s] for s in STATES],axis=1)
                writers['Comparison'].write(cv2.cvtColor(frame,cv2.COLOR_RGB2BGR))
                for s in STATES:writers[s].write(cv2.cvtColor(panels[s],cv2.COLOR_RGB2BGR))
                sources.append({'video_s':t,'sources':entries})
        finally:
            for w in writers.values():w.release()
        for key,path in outputs.items():
            width=2560 if key=='Comparison' else 1280
            decoder=cv2.VideoCapture(str(temporary[key]));count=0;dimensions=True
            while True:
                ok,frame=decoder.read()
                if not ok:break
                count+=1;dimensions=dimensions and frame.shape[:2]==(1210,width)
            fps=decoder.get(cv2.CAP_PROP_FPS);decoder.release()
            verification={'passed':count==201 and dimensions and abs(fps-10)<.01,
                          'decoded_frames':count,'dimensions':[width,1210],'fps':fps,
                          'source_alignment_limit_s':.051,'source':'actual recorded RGB',
                          'independent_inspection_status':'REVIEWED' if reviewed else 'PENDING'}
            selected=sources if key=='Comparison' else [
                {'video_s':r['video_s'],'sources':{key:r['sources'][key]}} for r in sources]
            if not verification['passed']:raise RuntimeError('Video failed full decode')
            temporary[key].replace(path)
            save(path.with_suffix('.source_frames.json'),selected)
            save(path.with_suffix('.verification.json'),verification)
            print(str(path))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('pictures','videos'))
    parser.add_argument('--root',type=Path,default=ROOT);parser.add_argument('--test',required=True)
    parser.add_argument('--mode',choices=MODES)
    parser.add_argument('--allow-unreviewed-export',action='store_true',
                        help='Export recorded media before independent review; inspection remains pending')
    args=parser.parse_args()
    if args.action=='pictures':pictures(args.root,args.test)
    else:videos(args.root,args.test,args.mode,args.allow_unreviewed_export)
