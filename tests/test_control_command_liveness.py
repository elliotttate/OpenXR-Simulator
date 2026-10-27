"""Isolated runtimes may capture frames without writing a diagnostic log."""
import importlib.util
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'mcp-server'))
spec=importlib.util.spec_from_file_location('control_server',ROOT/'mcp-server/openxr_simulator_mcp.py')
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)


class ControlLiveness(unittest.TestCase):
    def check(self,capture,age=0,alive=True):
        with patch.object(server,'current_log_file',return_value=None), \
             patch.object(server,'status_age_seconds',return_value=age), \
             patch.object(server,'_pid_alive',return_value=alive) as pid, \
             patch.object(server.ui_automation,'captures',{'fresh':capture}):
            result=server.runtime_is_live()
            return result,pid

    def capture(self):
        return {'created_at':time.time(),'geometry':{'supported':True,'session_id':'8044-59164950-1'}}

    def test_fresh_capture_and_heartbeat_validate_its_own_pid_without_a_log(self):
        live,pid=self.check(self.capture())
        self.assertTrue(live);pid.assert_called_once_with(8044)

    def test_old_capture_cannot_authorize_future_session_commands(self):
        c=self.capture();c['created_at']-=31
        self.assertFalse(self.check(c)[0])

    def test_stale_or_missing_heartbeat_and_dead_owner_are_rejected(self):
        for age,alive in ((3,True),(None,True),(0,False)):
            self.assertFalse(self.check(self.capture(),age,alive)[0])

    def test_unrecognized_or_unsupported_capture_does_not_guess_an_owner(self):
        for geometry in ({'supported':False,'session_id':'8044-59164950-1'},
                         {'supported':True,'session_id':'unknown'},{}):
            c=self.capture();c['geometry']=geometry
            live,pid=self.check(c);self.assertFalse(live);pid.assert_not_called()


if __name__=='__main__':unittest.main()
