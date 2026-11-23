"""Update acknowledgement belongs to one operation and one supervisor lifetime."""
from pathlib import Path
import sys
from unittest.mock import Mock
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
import test_collector_handoff as fixture
from self_hosted_collection import CollectionSupervisor
from collection_schedule import CollectionSchedules

OP='a'*32


class UpdateHandoffTests(unittest.TestCase):
    def setUp(self):
        fixture.SupervisorTests.setUp(self)
        self.hold={'state':'absent','operation_id':None}
        self.supervisor.update_hold_guard=Mock(check=lambda:dict(self.hold))

    def hold_operation(self, op=OP):
        self.hold.update(state='held',operation_id=op)

    def test_readonly_status_does_not_acknowledge_or_send_stop_before_tick(self):
        self.hold_operation()
        self.assertEqual(self.supervisor.update_status(),
            {'observed_operation_id':None,'active':True,'outcome':'unobserved'})
        self.assertEqual(self.process.stdin.commands,[])
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status(),
            {'observed_operation_id':OP,'active':True,'outcome':'draining'})
        self.assertEqual(self.process.stdin.commands,[b'stop\n'])

    def test_success_means_reaped_normal_exit_and_no_new_launch(self):
        self.hold_operation()
        self.supervisor.tick()
        self.process.code=0
        self.assertEqual(self.supervisor.update_status()['outcome'],'draining')
        self.supervisor.tick()
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status(),
            {'observed_operation_id':OP,'active':False,'outcome':'normal'})
        self.assertEqual(len(self.starts),1)
        self.assertTrue(self.value['settings']['automatic_collection'])
        self.hold.update(state='absent',operation_id=None)
        self.supervisor.tick()
        self.assertEqual(len(self.starts),2)

    def test_timeout_is_sticky_even_if_process_returns_zero(self):
        self.hold_operation()
        self.supervisor.tick()
        self.clock.now+=120
        self.supervisor.tick()
        self.assertEqual(self.process.signals,['terminate'])
        self.process.code=0
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status()['outcome'],'forced')
        self.assertEqual(len(self.starts),1)

    def test_failed_pipe_is_never_reclassified_as_success(self):
        self.process.stdin.write=Mock(side_effect=BrokenPipeError)
        self.hold_operation()
        self.supervisor.tick()
        self.process.code=0
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status()['outcome'],'unconfirmed')

    def test_prior_scheduled_pipe_failure_is_not_forgotten_by_new_hold(self):
        self.process.stdin.write=Mock(side_effect=BrokenPipeError)
        self.clock.now=self.supervisor.deadline
        self.supervisor.tick()
        self.assertEqual(self.supervisor.info['state'],'handoff_unconfirmed')
        self.hold_operation()
        self.process.code=0
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status()['outcome'],'unconfirmed')

    def test_prior_scheduled_termination_is_not_forgotten_by_new_hold(self):
        self.clock.now=self.supervisor.deadline
        self.supervisor.tick()
        self.clock.now+=120
        self.supervisor.tick()
        self.hold_operation()
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status()['outcome'],'forced')
        self.process.code=0
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status()['outcome'],'forced')

    def test_nonzero_exit_is_not_normal(self):
        self.hold_operation()
        self.supervisor.tick()
        self.process.code=1
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status()['outcome'],'unconfirmed')

    def test_hold_before_start_is_acknowledged_idle_without_using_old_last_exit(self):
        self.process.code=0
        self.value['settings']['automatic_collection']=False
        self.supervisor.tick()
        self.supervisor.info['last_exit']=-9
        self.hold_operation()
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status(),
            {'observed_operation_id':OP,'active':False,'outcome':'idle'})
        self.assertEqual(len(self.starts),1)

    def test_restarted_supervisor_does_not_inherit_old_ack(self):
        self.hold_operation()
        restarted=CollectionSupervisor(self.supervisor.manager, ready=lambda:None,
            schedules=CollectionSchedules(),spawn=self.supervisor.spawn,clock=self.clock,
            update_hold_guard=self.supervisor.update_hold_guard)
        self.assertEqual(restarted.update_status()['outcome'],'unobserved')
        restarted.tick()
        self.assertEqual(restarted.update_status()['outcome'],'idle')
        self.assertEqual(len(self.starts),1)

    def test_malformed_hold_stops_child_without_forging_operation_ack(self):
        self.hold.update(state='invalid',operation_id=None)
        self.supervisor.tick()
        self.assertEqual(self.process.stdin.commands,[b'stop\n'])
        self.process.code=0
        self.supervisor.tick()
        self.assertEqual(self.supervisor.update_status()['observed_operation_id'],None)
        self.assertEqual(len(self.starts),1)


if __name__=='__main__': unittest.main()
