"""Focus simulation must be explicit, typed and scoped to a live runtime."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mcp-server'))
import openxr_simulator_mcp as server


class SessionFocus(unittest.IsolatedAsyncioTestCase):
    async def test_registered_as_explicit_input_focus_simulation(self):
        specs={s.name:s for s in await server.list_tools()}
        self.assertIn('set_session_focus',specs)
        self.assertEqual(specs['set_session_focus'].inputSchema['properties']['focused']['type'],'boolean')

    async def test_only_boolean_values_can_queue_a_focus_change(self):
        for value in (False,True):
            with patch.object(server,'_write_control_command',return_value=None) as write:
                result=await server.call_tool('set_session_focus',{'focused':value})
                write.assert_called_once_with(server.SIMULATOR_DIR/'session_focus_command.json',{'focused':value})
                self.assertTrue(json.loads(result[0].text)['queued'])
        for arguments in ({},{'focused':0},{'focused':'false'},{'focused':None}):
            with patch.object(server,'_write_control_command') as write:
                with self.assertRaises(ValueError):await server.call_tool('set_session_focus',arguments)
                write.assert_not_called()

    async def test_inactive_runtime_does_not_report_a_queued_command(self):
        with patch.object(server,'_write_control_command',return_value='No live runtime'):
            result=await server.call_tool('set_session_focus',{'focused':False})
            self.assertEqual(result[0].text,'No live runtime')


if __name__=='__main__':unittest.main()
