import unittest
from crossguard.webserver import SimulationWorker,WorkerCommand

class LifecycleWorld:
 def __init__(self):
  self.receiver_mode='autonomous';self.receiver='autonomous';self.enabled=True;self.time=8;self.finished=True;self.view='driver';self.messages=[{'event':'old'}];self.resets=0
 def reset(self,enabled,reload_world=False):
  self.enabled=enabled;self.receiver=self.receiver_mode;self.time=0;self.finished=False;self.messages=[];self.view='top';self.resets+=1
 def set_view(self,view):self.view=view
 def metrics(self):return {"end_reason":"route_exit" if self.finished else None}
 def close(self):pass

class WebLifecycleTests(unittest.TestCase):
 def worker(self):
  w=SimulationWorker({'enabled':True},lambda p:None);w.world=LifecycleWorld();w.state='finished';w.current_view='opposite';return w
 def test_receiver_change_rebuilds_receiver_and_preserves_selected_camera(self):
  w=self.worker();w._execute_command(WorkerCommand('receiver',{'mode':'retrofit'}))
  self.assertEqual(w.world.receiver,'retrofit');self.assertEqual(w.world.view,'opposite');self.assertFalse(w.world.finished);self.assertEqual(w.trial_id,2)
 def test_system_change_restarts_sensors_and_clears_prior_trial_messages(self):
  w=self.worker();w._execute_command(WorkerCommand('system',{'enabled':False}))
  self.assertFalse(w.world.enabled);self.assertEqual(w.world.resets,1);self.assertEqual(w.world.messages,[]);self.assertEqual(w.world.view,'opposite')
 def test_finished_resume_does_not_misrepresent_completed_trial_as_running(self):
  w=self.worker();w._execute_command(WorkerCommand('resume'));self.assertEqual(w.state,'finished')
 def test_unavailable_selected_frame_is_not_relabelled_as_another_camera(self):
  import numpy as np
  packets=[];w=self.worker();w.broadcast_callback=packets.append
  w._package_and_broadcast({'driver':np.zeros((2,2,3),dtype=np.uint8)},{})
  self.assertIsNone(packets[-1]['image'])
 def test_reset_preserves_camera(self):
  w=self.worker();w._execute_command(WorkerCommand('reset'));self.assertEqual(w.world.view,'opposite');self.assertEqual(w.state,'running')
 def test_group_retrofit_restarts_modeled_human_trial(self):
  w=self.worker();w.config['group_mode']='cooperative';trial=w.trial_id
  w._execute_command(WorkerCommand('receiver',{'mode':'retrofit'}))
  self.assertEqual(w.receiver_mode,'retrofit');self.assertEqual(w.world.receiver_mode,'retrofit');self.assertEqual(w.trial_id,trial+1)
 def test_invalid_channel_fault_does_not_partially_change_profile(self):
  w=self.worker();w.config['communication_profile']='radio'
  with self.assertRaises(ValueError):w._execute_command(WorkerCommand('communication',{'profile':'optical','phone_available':'false'}))
  self.assertEqual(w.config['communication_profile'],'radio')
 def test_follower_camera_rejected_before_changing_single_view(self):
  w=self.worker()
  with self.assertRaises(ValueError):w._execute_command(WorkerCommand('view',{'name':'driver_b'}))
  self.assertEqual(w.current_view,'opposite')
 def test_configured_group_exposes_all_six_views(self):
  w=self.worker();w.config['group_mode']='cooperative'
  self.assertIn('driver_c',w.get_status_dict()['available_views'])
 def test_gap_change_restarts_trial_and_preserves_pause_and_configuration(self):
  from unittest.mock import Mock
  w=self.worker();w.state='paused';w.config['group_mode']='cooperative';w.system_enabled=False
  w._setup_world=Mock();trial=w.trial_id
  w._execute_command(WorkerCommand('following_gap',{'gap_m':5.5}))
  w._setup_world.assert_called_once();self.assertEqual(w.state,'paused');self.assertEqual(w.trial_id,trial+1)
  self.assertEqual(w.get_status_dict()['following_gap_m'],5.5)
  self.assertEqual(w.config['group_mode'],'cooperative');self.assertFalse(w.system_enabled)
 def test_invalid_gap_does_not_change_config_or_restart_trial(self):
  from unittest.mock import Mock
  w=self.worker();w.config['following_gap_m']=8.;w._setup_world=Mock();trial=w.trial_id
  with self.assertRaises(ValueError):w._execute_command(WorkerCommand('following_gap',{'gap_m':float('nan')}))
  w._setup_world.assert_not_called();self.assertEqual(w.config['following_gap_m'],8.);self.assertEqual(w.trial_id,trial)
 def test_gap_command_dispatches_through_public_command_queue(self):
  w=SimulationWorker({'enabled':True},lambda p:None);w.start()
  try:
   result=w.dispatch_command('following_gap',{'gap_m':5.},timeout=2.)
   self.assertEqual(result['state']['following_gap_m'],5.)
   with self.assertRaises(ValueError):w.dispatch_command('following_gap',{'gap_m':2.},timeout=2.)
  finally:w.stop_worker();w.join(timeout=2.)
 def test_reconnect_retains_frame_with_current_pause_state(self):
  w=self.worker();w.state='paused';w.latest_frame_packet={'status':{'trial_id':w.trial_id},'view':'opposite','image_mode':'rgb','image':'camera image'}
  packet=w.retained_frame()
  self.assertEqual(packet['image'],'camera image');self.assertEqual(packet['status']['state'],'paused')
 def test_reconnect_does_not_relabel_old_camera_or_old_trial_image(self):
  w=self.worker();w.latest_frame_packet={'status':{'trial_id':w.trial_id},'view':'driver','image_mode':'rgb','image':'driver image'}
  packet=w.retained_frame();self.assertIsNone(packet['image']);self.assertEqual(packet['view'],'opposite')
  w.trial_id+=1;self.assertIsNone(w.retained_frame())
if __name__=='__main__':unittest.main()
