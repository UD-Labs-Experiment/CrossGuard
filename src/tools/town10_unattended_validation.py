"""Detached, checkpointed validation only. Never launches robustness or CARLA."""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime,timezone
from pathlib import Path

import psutil
import requests

sys.path.insert(0,str(Path(__file__).resolve().parent))
from town10_continuation import MODES,STATES,digest,read,verified_catalogue


def utc():return datetime.now(timezone.utc).isoformat()


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2),encoding='utf-8')
    temporary.replace(path)


def pending_runs(root,cfg):
    """An interrupted partial attempt is evidence, never permission to overwrite."""
    pending=[]
    for mode in MODES:
        for state in STATES:
            run=root/cfg['relative_path']/mode/f'Assistance_{state}'
            if (run/'technical_failure.json').exists():
                raise RuntimeError(f'Retained failed attempt needs independent diagnosis: {run}')
            if (run/'results.json').exists():continue
            if any(run.glob('*.json')) or any((run/'assets/pictures').glob('*.jpg')):
                raise RuntimeError(f'Retained partial attempt needs independent diagnosis: {run}')
            pending.append((mode,state))
    return pending


def same_process(pid,created):
    try:return abs(psutil.Process(pid).create_time()-created)<.01
    except (psutil.NoSuchProcess,psutil.AccessDenied):return False


class Supervisor:
    def __init__(self,args):
        self.args=args;self.root=args.root.resolve();self.workspace=Path(__file__).resolve().parents[2]
        self.directory=self.root/'Reports/Unattended';self.directory.mkdir(parents=True,exist_ok=True)
        self.checkpoint=self.directory/'checkpoint.json';self.lock=self.directory/'supervisor.lock.json'
        self.state={'updated_utc':utc(),'supervisor_pid':os.getpid(),
            'supervisor_created':psutil.Process().create_time(),'phase':'Validation',
            'root':str(self.root),'test':args.test,'model':'gemini-3.8-flash-medium',
            'bridge_pid':args.bridge_pid,'bridge_created':args.bridge_created,
            'robustness_authorized':False,'auto_retry_failed_attempts':False}
        self.worker=None;self.owned={};self.env=os.environ.copy()
        self.env.update(PYTHONPATH=str(self.workspace/'src'),CROSSGUARD_STUDY_URL=args.bridge_url)

    def log(self,message):
        line=f'{utc()} {message}'
        with (self.directory/'supervisor.log').open('a',encoding='utf-8') as f:f.write(line+'\n')
        print(line,flush=True)

    def save(self,status,**updates):
        self.state.update(status=status,updated_utc=utc(),**updates);atomic_json(self.checkpoint,self.state)

    def acquire(self):
        if self.lock.exists():
            old=read(self.lock)
            if same_process(old['pid'],old['created']):raise RuntimeError('Existing supervisor is preserved; duplicate refused')
            if not self.args.resume:raise RuntimeError('Stale supervisor checkpoint: explicit --resume required')
            self.lock.unlink()
        fd=os.open(self.lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump({'pid':os.getpid(),'created':psutil.Process().create_time()},f)

    def status(self):
        if not same_process(self.args.bridge_pid,self.args.bridge_created):
            raise RuntimeError('Original sole bridge identity changed or exited; no replacement started')
        response=requests.get(self.args.bridge_url+'/api/status',timeout=10);response.raise_for_status()
        return response.json()

    def safe_stop(self):
        if self.worker is None:
            self.log('No owned validation worker; existing bridge and workers left unchanged');return
        if not same_process(self.args.bridge_pid,self.args.bridge_created):
            self.log('Original bridge unavailable; no replacement process touched');return
        try:
            response=requests.post(self.args.bridge_url+'/api/command',
                json={'action':'stop','params':{}},timeout=30);response.raise_for_status()
            self.state['safe_stop_response']=response.json()
            until=time.monotonic()+30
            while time.monotonic()<until:
                status=self.status()
                if status.get('state')!='running':
                    self.state['safe_stop_confirmed']=True;break
                time.sleep(1)
            self.log('Failure stop submitted through original bridge command queue')
        except Exception as exc:
            self.state['safe_stop_error']=str(exc);self.log('Stop queue unavailable: '+str(exc))

    def collect_children(self):
        if self.worker and self.worker.poll() is None:
            try:
                for p in psutil.Process(self.worker.pid).children(recursive=True):
                    self.owned[p.pid]=p.create_time()
            except (psutil.NoSuchProcess,psutil.AccessDenied):pass

    def terminate_owned(self):
        self.collect_children()
        if self.worker and self.worker.poll() is None:self.worker.terminate()
        # Identity checked: never terminate bridge, CARLA or an unrelated process.
        for pid,created in self.owned.items():
            if pid!=self.args.bridge_pid and same_process(pid,created):
                try:psutil.Process(pid).terminate()
                except (psutil.NoSuchProcess,psutil.AccessDenied):pass

    def offline(self,*arguments):
        command=[sys.executable,*map(str,arguments)]
        self.log('Offline command: '+subprocess.list2cmdline(command))
        with (self.directory/'offline.log').open('a',encoding='utf-8') as log:
            result=subprocess.run(command,cwd=self.workspace,env=self.env,stdout=log,stderr=subprocess.STDOUT,timeout=600)
        if result.returncode:raise RuntimeError('Offline assessment failed; see offline.log')

    def execute(self,cfg,mode,state):
        status=self.status()
        if status.get('state')=='running':raise RuntimeError('Existing live simulation preserved; refusing competing worker')
        self.owned={};name=f'{cfg["test"]}_{mode}_{state}'
        response_path=self.directory/f'{name}_agy_response.json';error_path=self.directory/f'{name}_agy_stderr.log'
        if response_path.exists():raise RuntimeError('Existing worker response retained; independent resume diagnosis required')
        command=[sys.executable,str(self.workspace/'src/tools/town10_continuation.py'),'run',
            '--root',str(self.root),'--test',cfg['test'],'--mode',mode,'--state',state]
        prompt=('You are the authorized simulation execution worker. Use the existing sole bridge at '+self.args.bridge_url+
            '. Execute exactly this one validation run command from '+str(self.workspace)+': '+subprocess.list2cmdline(command)+
            '. Set PYTHONPATH=src and CROSSGUARD_STUDY_URL='+self.args.bridge_url+
            '. Wait for process completion and report its exit code. Record all stdout/stderr in '+str(self.directory/f'{name}_simulation.log')+
            '. Do not modify source, configs, seeds, safeguards, snapshots or evidence. Do not start a bridge or CARLA, '
            'tick independently, retry, run other configurations, inspect/approve scenes, or start robustness. '
            'Any failure must be preserved and reported. The existing run tool records physical evidence through the bridge command queue.')
        atomic_json(self.directory/f'{name}_invocation.json',{'prompt':prompt,'command':command,'model':self.state['model'],'created_utc':utc()})
        argv=[self.args.agy,'-p',prompt,'--model',self.state['model'],'--effort','medium',
            '--mode','accept-edits','--dangerously-skip-permissions','--output-format','json','--print-timeout','0s']
        self.log('Launching approved agy validation worker: '+name)
        with response_path.open('w',encoding='utf-8') as out,error_path.open('w',encoding='utf-8') as err:
            self.worker=subprocess.Popen(argv,cwd=self.workspace,env=self.env,stdout=out,stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            begun=time.monotonic();last_progress=begun;last_time=None;errors=0
            self.save('RUNNING_VALIDATION',mode=mode,assistance=state,worker_pid=self.worker.pid,
                worker_created=psutil.Process(self.worker.pid).create_time(),response_file=str(response_path))
            while self.worker.poll() is None:
                self.collect_children()
                if time.monotonic()-begun>1500:raise RuntimeError('Worker wall-time bound exceeded')
                try:
                    status=self.status();errors=0;now=status.get('sim_time_s')
                    if now!=last_time:last_progress=time.monotonic();last_time=now
                    if status.get('state')=='running' and time.monotonic()-last_progress>180:
                        raise RuntimeError('Simulation progress stalled for 180 seconds')
                    self.save('RUNNING_VALIDATION',bridge_state=status.get('state'),simulation_s=now,
                        owned_children=[{'pid':p,'created':c} for p,c in self.owned.items()])
                except requests.RequestException:
                    errors+=1
                    if errors>=3:raise RuntimeError('Bridge status unavailable for three consecutive reads')
                time.sleep(5)
        response=read(response_path)
        if self.worker.returncode or response.get('status')!='SUCCESS':
            raise RuntimeError('agy process/transport failure; preserved response: '+str(response_path))
        run=self.root/cfg['relative_path']/mode/f'Assistance_{state}'
        if not (run/'results.json').exists() or (run/'technical_failure.json').exists():
            raise RuntimeError('Worker returned without successful physical evidence')
        leftovers=[pid for pid,created in self.owned.items() if same_process(pid,created)]
        if leftovers:raise RuntimeError('Worker exited with active owned subprocesses; stop before another run')
        self.log('Completed physical window: '+name)
        self.save('CHECKPOINT_RUN_COMPLETE',completed_mode=mode,completed_assistance=state)

    def run(self):
        self.acquire()
        try:
            catalogue=verified_catalogue(self.root)
            if digest(self.root/'frozen_catalogue.json')!=self.args.catalogue_sha256:
                raise RuntimeError('Pinned catalogue hash mismatch')
            for source,expected in catalogue.get('evaluation_source_hashes',{}).items():
                if digest(self.workspace/source)!=expected:raise RuntimeError('Frozen evaluation source changed: '+source)
            cfg=next(c for c in catalogue['tests'] if c['test']==self.args.test)
            if cfg['test']!='Validation_T02_01':raise RuntimeError('Unattended allowlist permits only approved T02 rain correction')
            pending=pending_runs(self.root,cfg);self.status()
            self.save('READY',pending_runs=pending,catalogue_sha256=self.args.catalogue_sha256)
            if self.args.dry_run:
                self.save('DRY_RUN_VERIFIED',simulation_commands_submitted=0)
                self.log('Dry run passed; no agent or simulation commands submitted');return
            for mode,state in pending:self.execute(cfg,mode,state)
            self.save('ASSESSING_RECORDED_VALIDATION')
            self.offline(self.workspace/'src/tools/town10_continuation_package.py','assess','--root',self.root,'--test',cfg['test'])
            self.offline(self.workspace/'src/tools/town10_continuation_media.py','pictures','--root',self.root,'--test',cfg['test'])
            self.offline(self.workspace/'src/tools/town10_continuation.py','check','--root',self.root)
            self.offline(self.workspace/'src/tools/town10_continuation_results.py','--root',self.root)
            self.save('AWAITING_CODEX_INDEPENDENT_REVIEW',physical_validation_recording_complete=True,
                videos_pending_after_picture_review=True,robustness_started=False)
            self.log('Authorized validation recording finished. Independent rain scene review/videos/gate remain pending. Robustness was not launched.')
        except Exception as exc:
            self.log('FAILED: '+str(exc));self.safe_stop();self.terminate_owned()
            self.save('FAILED_SAFE_STOP',error=str(exc),robustness_started=False)
            raise
        finally:
            if self.lock.exists() and read(self.lock).get('pid')==os.getpid():self.lock.unlink()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--test',default='Validation_T02_01');p.add_argument('--catalogue-sha256',required=True)
    p.add_argument('--bridge-url',default='http://127.0.0.1:8007');p.add_argument('--bridge-pid',type=int,required=True)
    p.add_argument('--bridge-created',type=float,required=True);p.add_argument('--agy',required=True)
    p.add_argument('--resume',action='store_true');p.add_argument('--dry-run',action='store_true')
    Supervisor(p.parse_args()).run()
