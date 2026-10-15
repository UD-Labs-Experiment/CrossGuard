import unittest
from dataclasses import replace
from crossguard.contracts import Message, Observation
from crossguard.communication.channels import ChannelNetwork,ChannelConfig,OpticalGeometry
from crossguard.communication.braking import BrakingEvent,relay_braking,PhoneAdvisoryModel


def message(event_id='hazard',expiry=3.):
    return Message(event_id,'rsu','B',0.,expiry,Observation('ped','rsu',0.,(10.,0.)))


class ChannelTests(unittest.TestCase):
    def test_each_channel_outage_and_all_outage(self):
        for failed in (['radio'],['cellular'],['optical'],['radio','cellular','optical']):
            n=ChannelNetwork('hybrid',configs=self.configs(),geometry=self.geometry,
                outages=[{'channel':key,'start_s':0.,'end_s':1.} for key in failed])
            n.send(message(),0.,(0,0),(10,0))
            delivered=n.receive('B',.5)
            self.assertEqual(bool(delivered),len(failed)<3)
            if delivered:self.assertNotIn(delivered[0].channel,failed)
            n.send(message('recovered'),1.,(0,0),(10,0))
            self.assertTrue(n.receive('B',1.5))

    def test_inflight_packet_cannot_bypass_outage(self):
        n=ChannelNetwork('radio',configs=self.configs(),outages=[{'channel':'radio','start_s':.02,'end_s':1.}])
        n.send(message(),0.,(0,0),(10,0))
        self.assertEqual(n.receive('B',.1),[])
        self.assertEqual(n.events[-1]['reason'],'scheduled_outage_in_flight')
    def configs(self):
        return {k:ChannelConfig(delay,0.,0.,500.) for k,delay in
                [('radio',.05),('cellular',.3),('optical',.01)]}

    def geometry(self,*args):
        return OpticalGeometry((1.,0.,0.),(-1.,0.,0.))

    def test_optical_blocked_and_radio_fallback(self):
        n=ChannelNetwork('hybrid',configs=self.configs(),
            geometry=lambda *args: OpticalGeometry((1.,0.,0.),(-1.,0.,0.),True))
        n.send(message(),0.,(0.,0.),(10.,0.))
        self.assertEqual([m.channel for m in n.receive('B',.1)],['radio'])
        self.assertEqual(n.events[2]['reason'],'line_of_sight_blocked')
        self.assertEqual(n.receive('B',.4),[])
        self.assertEqual(n.events[-1]['event'],'duplicate')

    def test_optical_fov_and_missing_geometry_fail_closed(self):
        for geometry,reason in [(None,'optical_geometry_unavailable'),
            (lambda *a:OpticalGeometry((-1.,0.,0.),(-1.,0.,0.)),'outside_emitter_fov'),
            (lambda *a:OpticalGeometry((1.,0.,0.),(1.,0.,0.)),'outside_receiver_fov')]:
            n=ChannelNetwork('optical',geometry=geometry)
            self.assertEqual(n.send(message(),0.,(0.,0.),(10.,0.))['reason'],reason)
            self.assertEqual(n.receive('B',1.),[])

    def test_optical_3d_range(self):
        n=ChannelNetwork('optical',configs={'optical':ChannelConfig(0,0,0,5)},
            geometry=lambda *a:OpticalGeometry((0,0,-1),(0,0,1),
                emitter_position=(0,0,10),receiver_position=(0,0,0)))
        self.assertEqual(n.send(message(),0,(0,0),(0,0))['reason'],'optical_out_of_range')

    def test_hybrid_fastest_actual_jitter_delivery_wins(self):
        configs=self.configs()
        configs['radio']=ChannelConfig(.1,.09,0.,500.)
        configs['optical']=ChannelConfig(.08,0.,0.,500.)
        n=ChannelNetwork('hybrid',seed=1,configs=configs,geometry=self.geometry)
        n.send(message(),0.,(0.,0.),(10.,0.))
        # Radio seed1 gives jitter -.0658 and wins despite nominal .1 > .08.
        self.assertEqual([m.channel for m in n.receive('B',1.)],['radio'])
        self.assertEqual(sum(e['event']=='received' for e in n.events),1)

    def test_hop_and_expiry_before_optical_geometry(self):
        n=ChannelNetwork('optical',geometry=lambda *a:self.fail('geometry should not run'))
        self.assertEqual(n.send(replace(message(),hop_count=3),0.,(0,0),(10,0))['reason'],'hop_limit_exceeded')
        self.assertEqual(n.send(message(expiry=.1),.2,(0,0),(10,0))['event'],'expired')

    def test_expired_in_transit_and_reset(self):
        n=ChannelNetwork('cellular',configs=self.configs())
        n.send(message(expiry=.1),0.,(0,0),(10,0))
        self.assertEqual(n.receive('B',.4),[])
        self.assertEqual(n.events[-1]['event'],'expired')
        n.reset(); self.assertEqual(n.events,[])
        n.send(message(),0.,(0,0),(10,0))
        self.assertEqual(len(n.receive('B',.4)),1)
        self.assertEqual(n.summary()['channels'][1]['delivered'],1)

    def test_original_time_and_per_recipient_identity(self):
        n=ChannelNetwork('hybrid',configs=self.configs(),geometry=self.geometry)
        for receiver in ('B','C'):
            n.send(replace(message(),receiver_id=receiver),.2,(0,0),(10,0))
            delivered=n.receive(receiver,1.)
            self.assertEqual(len(delivered),1)
            self.assertEqual(delivered[0].observation.observed_at_s,0.)


class BrakingPhoneTests(unittest.TestCase):
    def braking(self):
        return BrakingEvent('A:brake:1','A',1.,3.,6.,(10.,0.),(5.,0.))

    def test_braking_adapter_and_bounded_relay(self):
        m=self.braking().to_message('B',1.1)
        event=BrakingEvent.from_message(m,1.2)
        self.assertEqual(event,self.braking())
        relay=relay_braking(m,'C','B',1.2)
        self.assertEqual(relay.observation,m.observation)
        self.assertEqual(relay.event_id,m.event_id)
        self.assertEqual(relay.hop_count,1)
        self.assertIsNone(relay_braking(relay,'D','C',1.3))
        self.assertIsNone(BrakingEvent.from_message(m,2.5))
        self.assertIsNone(BrakingEvent.from_message(m,.5))
        self.assertIsNone(BrakingEvent.from_message(m,3.1,max_age_s=5))

    def test_braking_duplicate_and_distinct_refresh(self):
        n=ChannelNetwork('radio',configs={'radio':ChannelConfig(0,0,0,500)})
        m=self.braking().to_message('B',1.)
        n.send(m,1,(0,0),(10,0)); n.send(m,1,(0,0),(10,0))
        self.assertEqual(len(n.receive('B',1)),1)
        m2=replace(self.braking(),event_id='A:brake:1.2',observed_at_s=1.2).to_message('B',1.2)
        n.send(m2,1.2,(0,0),(10,0))
        self.assertEqual(len(n.receive('B',1.2)),1)

    def test_phone_unavailable_and_reaction(self):
        phone=PhoneAdvisoryModel(reaction_delay_s=.8)
        phone.configure(available=False,receiver_id='C')
        self.assertEqual(phone.offer(replace(message(),receiver_id='C'),.3)['event'],'unavailable')
        self.assertEqual(phone.offer(message(),.3)['event'],'delivered')
        self.assertEqual(phone.offer(message(),.4)['event'],'duplicate')
        self.assertEqual(phone.poll(1.),[])
        self.assertEqual(len(phone.poll(1.1)),1)

    def test_phone_stale_or_unavailable_at_reaction_never_controls(self):
        phone=PhoneAdvisoryModel(reaction_delay_s=1.)
        phone.offer(message(expiry=.8),.3)
        self.assertEqual(phone.poll(1.3),[])
        phone.offer(message('next'),.4)
        phone.configure(available=False)
        self.assertEqual(phone.poll(1.4),[])
        self.assertEqual(phone.events[-1]['event'],'expired_before_reaction')

    def test_invalid_braking_and_phone_configuration(self):
        with self.assertRaises(ValueError):
            replace(self.braking(),deceleration_mps2=float('nan'))
        with self.assertRaises(ValueError):
            PhoneAdvisoryModel(reaction_delay_s=-1)


if __name__=='__main__': unittest.main()
