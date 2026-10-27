"""Command-file publication contracts, including a real Windows sharing lock."""
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"mcp-server"))
import ui_targeting
import atomic_json_file


def windows_denial(code=5):
    error=PermissionError(13,"Runtime reader temporarily denies replacement")
    error.winerror=code
    return error


class AtomicJsonContracts(unittest.TestCase):
    def test_replacement_retries_windows_denials_and_keeps_existing_command_until_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"command.json";path.write_text('{"previous":1}')
            unrelated=Path(tmp)/"another-writer.tmp";unrelated.write_text("owned elsewhere")
            replace=os.replace;sources=[]
            def denied_then_ready(source,target):
                sources.append(Path(source))
                self.assertEqual(json.loads(path.read_text()),{"previous":1})
                self.assertEqual(json.loads(Path(source).read_text()),{"next":2})
                if len(sources)<3:raise windows_denial(32)
                replace(source,target)
            with mock.patch.object(atomic_json_file.os,"replace",side_effect=denied_then_ready):
                atomic_json_file.atomic_json(path,{"next":2},timeout=.2,retry_interval=.001)
            self.assertEqual(len(sources),3);self.assertEqual(len(set(sources)),1)
            self.assertNotEqual(sources[0],path.with_suffix(".json.tmp"))
            self.assertEqual(json.loads(path.read_text()),{"next":2})
            self.assertFalse(sources[0].exists());self.assertEqual(unrelated.read_text(),"owned elsewhere")

    def test_permanent_windows_denial_has_a_deadline_preserves_target_and_cleans_only_owned_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"command.json";path.write_text("old command")
            other=Path(tmp)/"command.json.other-owner.tmp";other.write_text("do not touch")
            denial=windows_denial();started=time.monotonic()
            with mock.patch.object(atomic_json_file.os,"replace",side_effect=denial) as replacement:
                with self.assertRaises(PermissionError) as raised:
                    atomic_json_file.atomic_json(path,{"new":True},timeout=.03,retry_interval=.005)
            self.assertIs(raised.exception,denial)
            self.assertGreaterEqual(time.monotonic()-started,.03)
            self.assertLess(time.monotonic()-started,.5)
            self.assertGreater(replacement.call_count,1)
            self.assertEqual(path.read_text(),"old command")
            self.assertEqual(list(Path(tmp).glob("*.tmp")),[other])
            self.assertEqual(other.read_text(),"do not touch")

    def test_non_windows_permission_error_and_hard_replace_errors_are_never_retried(self):
        for error in (PermissionError(13,"POSIX denial"),FileNotFoundError("Missing directory"),OSError(28,"Disk full")):
            with self.subTest(error=error),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"command.json";path.write_text("original")
                with mock.patch.object(atomic_json_file.os,"replace",side_effect=error) as replacement:
                    with self.assertRaises(type(error)) as raised:atomic_json_file.atomic_json(path,{"test":True})
                self.assertIs(raised.exception,error);self.assertEqual(replacement.call_count,1)
                self.assertEqual(path.read_text(),"original");self.assertEqual(list(Path(tmp).glob("*.tmp")),[])

    def test_bad_payload_and_deadline_configuration_fail_before_creating_a_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"command.json";path.write_text("original")
            with mock.patch.object(atomic_json_file.tempfile,"mkstemp") as create:
                for payload in ({"bad":float("nan")},{"bad":float("inf")},{"bad":object()}):
                    with self.assertRaises((TypeError,ValueError)):atomic_json_file.atomic_json(path,payload)
                for kwargs in ({"timeout":-1},{"timeout":float("inf")},{"retry_interval":0}):
                    with self.assertRaises(ValueError):atomic_json_file.atomic_json(path,{"valid":True},**kwargs)
            create.assert_not_called();self.assertEqual(path.read_text(),"original")

    def test_temp_creation_access_error_is_not_retried_as_a_replace_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"command.json";path.write_text("original")
            denial=windows_denial()
            with mock.patch.object(atomic_json_file.tempfile,"mkstemp",side_effect=denial) as create:
                with self.assertRaises(PermissionError):atomic_json_file.atomic_json(path,{"valid":True})
            create.assert_called_once();self.assertEqual(path.read_text(),"original")

    def test_concurrent_writers_use_distinct_owned_temps_and_publish_complete_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"command.json";barrier=threading.Barrier(6);gate=threading.Lock()
            sources={};replace=os.replace;mkstemp=tempfile.mkstemp
            def acquire_unique_temp(*args,**kwargs):
                owned=mkstemp(*args,**kwargs)
                # Ensure all six calls own their temp before writing. Do not
                # spend their replacement deadline waiting on a test barrier.
                barrier.wait(3)
                return owned
            def coordinated(source,target):
                source=Path(source)
                with gate:
                    first=source not in sources
                    if first:sources[source]=json.loads(source.read_text())
                replace(source,target)
            with mock.patch.object(atomic_json_file.os,"replace",side_effect=coordinated),mock.patch.object(atomic_json_file.tempfile,"mkstemp",side_effect=acquire_unique_temp):
                with ThreadPoolExecutor(max_workers=6) as pool:
                    results=[pool.submit(atomic_json_file.atomic_json,path,{"writer":i,"content":"x"*2000},timeout=2) for i in range(6)]
                    for result in results:result.result(timeout=4)
            self.assertEqual(len(sources),6)
            self.assertEqual({p["writer"] for p in sources.values()},set(range(6)))
            self.assertIn(json.loads(path.read_text())["writer"],range(6))
            self.assertEqual(list(Path(tmp).glob("*.tmp")),[])

    def test_both_existing_command_paths_use_the_shared_writer(self):
        self.assertIs(ui_targeting.atomic_json,atomic_json_file.atomic_json)
        spec=importlib.util.spec_from_file_location("atomic_json_server_contract",Path(__file__).resolve().parents[1]/"mcp-server/openxr_simulator_mcp.py")
        server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
        self.assertIs(server.atomic_json,atomic_json_file.atomic_json)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"head_pose_command.json"
            with mock.patch.object(server,"ensure_simulator_dir") as ensure:
                server._write_json_command(path,{"yaw":.25})
            ensure.assert_called_once()
            self.assertEqual(json.loads(path.read_text()),{"yaw":.25})


@unittest.skipUnless(os.name=="nt","Windows file-sharing semantics")
class WindowsReplacementLock(unittest.TestCase):
    def test_actual_reader_lock_is_retried_until_the_reader_closes(self):
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.CreateFileW.argtypes=(wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                     wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=(wintypes.HANDLE,);kernel.CloseHandle.restype=wintypes.BOOL
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"head_pose_command.json";path.write_text('{"before":true}')
            # A short-lived runtime-style reader allows reads/writes but denies
            # FILE_SHARE_DELETE, so atomic replacement gets the observed WinError5.
            handle=kernel.CreateFileW(str(path),0x80000000,0x1|0x2,None,3,0,None)
            if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
            closed=threading.Event()
            def unlock():
                time.sleep(.04);kernel.CloseHandle(handle);closed.set()
            reader=threading.Thread(target=unlock);reader.start();started=time.monotonic()
            try:
                ui_targeting.atomic_json(path,{"after":True})
            finally:
                reader.join(1)
            self.assertTrue(closed.is_set())
            self.assertGreaterEqual(time.monotonic()-started,.02)
            self.assertEqual(json.loads(path.read_text()),{"after":True})
            self.assertEqual(list(Path(tmp).glob("*.tmp")),[])


if __name__=="__main__":unittest.main()
