"""Driver response waits once, but can retain newly delivered target evidence."""
import unittest
from dataclasses import replace
from crossguard.communication.braking import PhoneAdvisoryModel, BrakingEvent
from crossguard.contracts import Message, Observation


def sample(event, observed, receiver='A', source='rsu', object_id='person', expiry=None):
    observation=Observation(object_id,source,observed,(0.,0.))
    return Message(event,source,receiver,observed,
                   observed+2.5 if expiry is None else expiry,observation)


class PhoneRefreshTests(unittest.TestCase):
    def test_long_driver_uses_new_evidence_at_original_deadline(self):
        model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
        model.offer(sample('first',0.),0.)
        new=sample('fresh',1.5)
        event=model.offer(new,1.5)
        self.assertEqual(event['event'],'refreshed')
        self.assertEqual(event['react_at_s'],2.4)
        self.assertEqual(model.poll(2.399),[])
        self.assertEqual(model.poll(2.4),[new])
        self.assertEqual(model.events[-1]['initial_event_id'],'first')
        self.assertEqual(model.events[-1]['event_id'],'fresh')

    def test_default_retains_per_packet_expiry_behavior(self):
        model=PhoneAdvisoryModel(reaction_delay_s=2.4)
        model.offer(sample('first',0.),0.)
        model.offer(sample('fresh',1.5),1.5)
        self.assertEqual(model.poll(2.4),[])
        self.assertEqual(model.events[-1]['event'],'expired_before_reaction')
        self.assertEqual(model.summary()['pending'],1)

    def test_duplicate_cannot_refresh_or_reset_deadline(self):
        model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
        first=sample('first',0.)
        model.offer(first,0.)
        self.assertEqual(model.offer(replace(first,observation=replace(first.observation,observed_at_s=1.5)),1.5)['event'],'duplicate')
        self.assertEqual(model.poll(2.4),[])

    def test_stale_future_or_expired_evidence_cannot_revive_pending(self):
        for offered in (sample('new',-.6),sample('new',2.),sample('new',1.5,expiry=1.49)):
            with self.subTest(message=offered):
                model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
                model.offer(sample('first',0.),0.)
                self.assertEqual(model.offer(offered,1.5)['event'],'expired')
                self.assertEqual(model.poll(2.4),[])

    def test_distinct_target_or_receiver_never_shares_timer(self):
        for new in (sample('new',1.5,receiver='B'),sample('new',1.5,source='bus'),sample('new',1.5,object_id='other')):
            with self.subTest(message=new):
                model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
                model.offer(sample('first',0.),0.)
                self.assertEqual(model.offer(new,1.5)['event'],'delivered')
                self.assertEqual(model.poll(2.4),[])
                self.assertEqual(model.summary()['pending'],1)

    def test_out_of_order_evidence_does_not_replace_newer_measurement(self):
        model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
        model.offer(sample('first',0.),0.)
        new=sample('new',1.5)
        model.offer(new,1.5)
        self.assertEqual(model.offer(sample('older',1.2),1.6)['event'],'older_evidence')
        self.assertEqual(model.poll(2.4),[new])

    def test_refreshed_packet_must_still_be_fresh_and_available_at_response(self):
        for unavailable in (False,True):
            model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
            model.offer(sample('first',0.),0.)
            model.offer(sample('new',1.5,expiry=2.3 if not unavailable else 4.),1.5)
            if unavailable: model.configure(available=False)
            self.assertEqual(model.poll(2.4),[])
            self.assertEqual(model.events[-1]['event'],'expired_before_reaction')

    def test_refresh_after_deadline_requires_new_reaction_interval(self):
        model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
        model.offer(sample('first',0.),0.)
        event=model.offer(sample('new',2.5),2.5)
        self.assertEqual(event['event'],'delivered')
        self.assertEqual(event['react_at_s'],4.9)
        self.assertEqual(model.poll(2.5),[])

    def test_phone_refresh_does_not_relax_braking_age_validation(self):
        model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
        first=BrakingEvent('first','lead',0.,1.2,6.,(0.,0.)).to_message('B',0.)
        new=BrakingEvent('new','lead',1.3,2.5,6.,(0.,0.)).to_message('B',1.3)
        model.offer(first,0.);model.offer(new,1.3)
        self.assertEqual(model.poll(2.4),[new])
        self.assertIsNone(BrakingEvent.from_message(new,2.4))

    def test_reset_discards_previous_target_and_deadline(self):
        model=PhoneAdvisoryModel(reaction_delay_s=2.4,refresh_pending_by_target=True)
        model.offer(sample('first',0.),0.);model.reset()
        event=model.offer(sample('new',1.5),1.5)
        self.assertEqual(event['event'],'delivered')
        self.assertEqual(event['react_at_s'],3.9)
        with self.assertRaises(ValueError): PhoneAdvisoryModel(refresh_pending_by_target='yes')


if __name__=='__main__': unittest.main()
