"""Six independent offline theme workers; immutable simulation inputs, resumable media."""
import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from town10_continuation import MODES, STATES, digest, read, verified_catalogue
from town10_unattended_robustness import atomic_json, same_process


def utc():
    return datetime.now(timezone.utc).isoformat()


def output_paths(folder, mode):
    return [folder/mode/f'Assistance_{state}'/'assets/videos'/f'{mode}_Assistance_{state}.mp4'
            for state in STATES] + [folder/mode/'Comparison'/f'{mode}_Assistance_OFF_vs_ON.mp4']


def validate_outputs(folder, mode, checkpoint):
    """Resume requires all three hashes, source manifests and full video decoding."""
    import cv2
    cv2.setNumThreads(1)
    paths = output_paths(folder, mode)
    if not checkpoint or len(checkpoint.get('videos', [])) != 3:
        return False
    for path, saved in zip(paths, checkpoint['videos']):
        if not path.exists() or digest(path) != saved['sha256']:
            return False
        try:
            proof = read(path.with_suffix('.verification.json'))
            sources = read(path.with_suffix('.source_frames.json'))
        except (OSError, ValueError):
            return False
        if proof.get('passed') is not True or len(sources) != 201:
            return False
        decoder = cv2.VideoCapture(str(path)); count = 0
        width = 2560 if path == paths[-1] else 1280
        try:
            while True:
                ok, frame = decoder.read()
                if not ok: break
                if frame.shape[:2] != (1210, width): return False
                count += 1
            if count != 201 or abs(decoder.get(cv2.CAP_PROP_FPS)-10) > .01:
                return False
        finally:
            decoder.release()
    return True


def source_identity(folder, mode):
    return {str(path.relative_to(folder)): digest(path)
            for state in STATES for name in ('results.json', 'camera_manifest.json', 'physical_timeline.json', 'placement_audit.json')
            for path in [folder/mode/f'Assistance_{state}'/name]}


def theme_worker(args):
    import cv2
    cv2.setNumThreads(1)
    root=args.root; report=root/'Reports/Video_Export'
    report.mkdir(parents=True, exist_ok=True)
    progress_path=report/f'progress_{args.theme}.json'
    lock_path=report/f'owner_{args.theme}.json'
    import psutil
    if lock_path.exists():
        old=read(lock_path)
        if same_process(old.get('pid'), old.get('created')):
            raise RuntimeError('Theme already has a live export owner')
    owner={'pid':os.getpid(),'created':psutil.Process().create_time()}
    atomic_json(lock_path,owner)
    prior=read(progress_path) if progress_path.exists() else {}
    state={'status':'RUNNING','planned_videos':60,'completed_videos':0,'failed_videos':0,
           'elapsed_s':prior.get('elapsed_s',0),'eta_s':None,'current':None,
           'worker_pid':os.getpid(),'error':None,'theme':args.theme,'total_modes':20,
           'completed_modes':dict(prior.get('completed_modes',{})),'failed_modes':{},'independent_review_status':'PENDING',
           'catalogue_sha256':args.catalogue_sha256,'model_calls':0}
    if prior and prior.get('catalogue_sha256')!=args.catalogue_sha256:
        lock_path.unlink(); raise RuntimeError('Saved export catalogue identity differs')
    started=time.monotonic(); previous_elapsed=state['elapsed_s']
    def checkpoint():
        state['elapsed_s']=previous_elapsed+time.monotonic()-started
        state['updated_utc']=utc()
        state['completed_videos']=len(state['completed_modes'])*3
        state['failed_videos']=len(state['failed_modes'])*3
        state['eta_s']=(state['elapsed_s']/state['completed_videos']*(60-state['completed_videos'])
                        if state['completed_videos'] else None)
        atomic_json(progress_path,state)
    try:
        if digest(root/'frozen_catalogue.json')!=args.catalogue_sha256:
            raise RuntimeError('Frozen catalogue checksum mismatch')
        configurations=[c for c in verified_catalogue(root)['tests']
                        if c['test'].startswith('Robustness_') and c['theme']==args.theme]
        if len(configurations)!=10: raise RuntimeError('Expected exactly ten configurations per theme')
        checkpoint()
        for cfg in configurations:
            folder=root/cfg['relative_path']
            for mode in MODES:
                key=f'{cfg["test"]}/{mode}'
                state['current']=key; checkpoint()
                try:
                    identity=source_identity(folder,mode)
                    saved=prior.get('completed_modes',{}).get(key)
                    if saved and saved.get('source_identity')==identity and validate_outputs(folder,mode,saved):
                        state['completed_modes'][key]=saved; checkpoint(); continue
                    state['completed_modes'].pop(key,None)
                    checkpoint()
                    # Preserve all existing media before replacing incomplete or previously unverified files.
                    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
                    for path in output_paths(folder,mode):
                        for existing in (path,path.with_suffix('.verification.json'),path.with_suffix('.source_frames.json')):
                            if existing.exists():
                                backup=report/'preserved_existing'/stamp/existing.relative_to(root)
                                backup.parent.mkdir(parents=True,exist_ok=True)
                                import shutil
                                shutil.copy2(existing,backup)
                    log_path=report/f'{cfg["test"]}_{mode}_{stamp}.log'
                    with log_path.open('w',encoding='utf-8') as log:
                        child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('town10_continuation_media.py')),
                            'videos','--root',str(root),'--test',cfg['test'],'--mode',mode,
                            '--allow-unreviewed-export'],stdout=log,stderr=subprocess.STDOUT)
                        deadline=time.monotonic()+1800
                        while child.poll() is None:
                            checkpoint()
                            if time.monotonic()>deadline:
                                child.kill();child.wait();raise TimeoutError('Export exceeded 30 minute mode limit')
                            time.sleep(5)
                        if child.returncode:raise RuntimeError(f'Encoder exited {child.returncode}; see {log_path}')
                    if source_identity(folder,mode)!=identity: raise RuntimeError('Simulation input changed during export')
                    saved={'completed_utc':utc(),'source_identity':identity,'videos':[
                        {'path':str(path.relative_to(root)),'sha256':digest(path)} for path in output_paths(folder,mode)]}
                    if not validate_outputs(folder,mode,saved):raise RuntimeError('Independent full decode failed')
                    state['completed_modes'][key]=saved
                except Exception:
                    state['failed_modes'][key]={'error':traceback.format_exc(),'updated_utc':utc()}
                checkpoint()
        state['status']='COMPLETE_PENDING_INDEPENDENT_REVIEW' if not state['failed_modes'] else 'COMPLETE_WITH_ERRORS'
        state['current']=None;state['eta_s']=0;checkpoint()
        return 1 if state['failed_modes'] else 0
    except BaseException:
        state['status']='FAILED';state['error']=traceback.format_exc();checkpoint();raise
    finally:
        if lock_path.exists() and read(lock_path)==owner:lock_path.unlink()


def controller(args):
    """Windows scheduled launcher owns this process and its six independent children."""
    report=args.root/'Reports/Video_Export'; report.mkdir(parents=True,exist_ok=True)
    import psutil
    owner_path=report/'controller_owner.json'
    if owner_path.exists():
        old=read(owner_path)
        if same_process(old.get('pid'),old.get('created')):raise RuntimeError('Export controller already alive')
    for number in range(1,7):
        path=report/f'owner_T{number:02}.json'
        if path.exists():
            old=read(path)
            if same_process(old.get('pid'),old.get('created')):raise RuntimeError('Existing theme worker still alive')
    if digest(args.root/'frozen_catalogue.json')!=args.catalogue_sha256:raise RuntimeError('Catalogue mismatch')
    snapshot_path=report/'simulation_input_hashes.json'
    if snapshot_path.exists():
        snapshot=read(snapshot_path)
        if any(not (args.root/name).exists() or digest(args.root/name)!=sha for name,sha in snapshot.items()):
            raise RuntimeError('Existing immutable simulation input snapshot differs')
    else:
        snapshot={'frozen_catalogue.json':digest(args.root/'frozen_catalogue.json')}
        for cfg in verified_catalogue(args.root)['tests']:
            if not cfg['test'].startswith('Robustness_'):continue
            folder=args.root/cfg['relative_path']
            for mode in MODES:
                for state in STATES:
                    for path in (folder/mode/f'Assistance_{state}').glob('*.json'):
                        snapshot[str(path.relative_to(args.root))]=digest(path)
        atomic_json(snapshot_path,snapshot)
    atomic_json(owner_path,{'pid':os.getpid(),'created':psutil.Process().create_time()})
    children=[]
    for number in range(1,7):
        theme=f'T{number:02}'
        log=(report/f'{theme}_worker_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")}.log').open('w')
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'theme','--root',str(args.root),
            '--catalogue-sha256',args.catalogue_sha256,'--theme',theme],stdout=log,stderr=subprocess.STDOUT)
        children.append((theme,child,log))
    try:
        while True:
            states=[read(report/f'progress_T{number:02}.json') for number in range(1,7)
                    if (report/f'progress_T{number:02}.json').exists()]
            finished=all(child.poll() is not None for _,child,_ in children)
            completed=sum(s.get('completed_videos',0) for s in states)
            failures=sum(s.get('failed_videos',0) for s in states)
            status=('COMPLETE_PENDING_INDEPENDENT_REVIEW' if completed==360 and failures==0
                    else 'COMPLETE_WITH_ERRORS') if finished else 'RUNNING'
            atomic_json(report/'progress.json',{'status':status,'planned_videos':360,'completed_videos':completed,
                'failed_videos':failures,'updated_utc':utc(),'worker_pid':os.getpid(),
                'independent_review_status':'PENDING','themes':states,'children':[
                    {'theme':theme,'pid':child.pid,'exit_code':child.poll()} for theme,child,_ in children]})
            if finished:
                changed=[name for name,sha in snapshot.items()
                         if not (args.root/name).exists() or digest(args.root/name)!=sha]
                atomic_json(report/'simulation_input_preservation.json',{'passed':not changed,
                    'checked_files':len(snapshot),'changed_files':changed,'updated_utc':utc()})
                if changed:raise RuntimeError('Simulation input preservation failed')
                return 0 if completed==360 and failures==0 else 1
            time.sleep(5)
    finally:
        for _,_,log in children:log.close()
        if owner_path.exists() and read(owner_path).get('pid')==os.getpid():owner_path.unlink()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('run','theme'))
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--catalogue-sha256',required=True)
    parser.add_argument('--theme',choices=[f'T{n:02}' for n in range(1,7)])
    args=parser.parse_args()
    if args.action=='theme' and not args.theme:parser.error('theme worker requires --theme')
    return theme_worker(args) if args.action=='theme' else controller(args)


if __name__=='__main__':sys.exit(main())
