#!/usr/bin/env python3
"""Test the inventory's embedded Linux helper on hosted temporary fixtures."""
import os
from pathlib import Path
import pwd
import re
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

source=(Path(__file__).resolve().parents[1]/"windows/inventory.ps1").read_text(encoding="utf-8")
script=re.search(r"\$script:InventoryMirrorPython=@'\n(.*?)\n'@",source,re.S).group(1)
helper={"__name__":"inventory_mirror_tests"}
exec(compile(script,"InventoryMirrorPython","exec"),helper)
assert helper["BASE"]=="/home/agent/project-data"

class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)/"project-data"
        self.base.mkdir()
        helper["BASE"]=str(self.base)
        self.stage=".inventory-"+"a"*32+".tmp"
        self.identity=patch.object(pwd,"getpwnam",return_value=SimpleNamespace(pw_uid=os.getuid()))
        self.identity.start()

    def tearDown(self):
        self.identity.stop()
        self.temp.cleanup()

    def run_mode(self,mode,name=None):
        helper["main"](mode,self.stage if name is None else name)

    def test_private_atomic_replace_and_exact_bytes(self):
        old=os.umask(0o002)
        try:
            for data in (b'{"schemaVersion":2,"distros":[]}',b'{"schemaVersion":2,"distros":[{"Name":"AgentDev"}]}'):
                self.run_mode("prepare")
                directory=self.base/"inventory"
                stage=directory/self.stage
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)
                self.assertEqual(stat.S_IMODE(stage.stat().st_mode),0o600)
                self.assertEqual(stage.stat().st_uid,os.getuid())
                stage.write_bytes(data) # Models writing the pre-created inode via UNC.
                self.run_mode("publish")
                latest=directory/"latest.json"
                self.assertEqual(latest.read_bytes(),data)
                self.assertEqual(latest.stat().st_uid,os.getuid())
                self.assertEqual(stat.S_IMODE(latest.stat().st_mode),0o600)
                self.assertFalse(stage.exists())
        finally:
            os.umask(old)

    def test_cleanup_preserves_previous_snapshot(self):
        self.run_mode("prepare")
        latest=self.base/"inventory/latest.json"
        latest.write_bytes(b"previous")
        self.run_mode("cleanup")
        self.assertEqual(latest.read_bytes(),b"previous")
        self.assertFalse((latest.parent/self.stage).exists())

    def test_symlink_directory_and_base_refused(self):
        outside=Path(self.temp.name)/"outside"
        outside.mkdir()
        (self.base/"inventory").symlink_to(outside,target_is_directory=True)
        with self.assertRaises(OSError):
            self.run_mode("prepare")
        self.assertEqual(list(outside.iterdir()),[])
        alias=Path(self.temp.name)/"alias"
        alias.symlink_to(self.base,target_is_directory=True)
        helper["BASE"]=str(alias)
        with self.assertRaises(OSError):
            self.run_mode("prepare")

    def test_stage_symlink_hardlink_and_fifo_refused(self):
        self.run_mode("prepare")
        stage=self.base/"inventory"/self.stage
        outside=Path(self.temp.name)/"outside"
        outside.write_bytes(b"unchanged")
        stage.unlink()
        stage.symlink_to(outside)
        with self.assertRaises(OSError):
            self.run_mode("publish")
        stage.unlink()
        os.link(outside,stage)
        with self.assertRaises(PermissionError):
            self.run_mode("publish")
        stage.unlink()
        os.mkfifo(stage,0o600)
        with self.assertRaises(PermissionError):
            self.run_mode("publish")
        self.assertEqual(outside.read_bytes(),b"unchanged")

    def test_destination_symlink_replaced_without_following(self):
        self.run_mode("prepare")
        directory=self.base/"inventory"
        outside=Path(self.temp.name)/"outside"
        outside.write_bytes(b"unchanged")
        (directory/"latest.json").symlink_to(outside)
        (directory/self.stage).write_bytes(b"snapshot")
        self.run_mode("publish")
        self.assertEqual(outside.read_bytes(),b"unchanged")
        self.assertFalse((directory/"latest.json").is_symlink())
        self.assertEqual((directory/"latest.json").read_bytes(),b"snapshot")

    def test_non_agent_and_wrong_directory_owner_refused(self):
        with patch.object(pwd,"getpwnam",return_value=SimpleNamespace(pw_uid=os.getuid()+1)):
            with self.assertRaises(PermissionError):
                self.run_mode("prepare")
        self.run_mode("prepare")
        original=os.fstat
        def wrong_owner(fd):
            info=original(fd)
            if stat.S_ISDIR(info.st_mode):
                return SimpleNamespace(st_uid=os.getuid()+1)
            return info
        with patch.object(os,"fstat",side_effect=wrong_owner):
            with self.assertRaises(PermissionError):
                self.run_mode("publish")

    def test_invalid_operations_names_and_missing_stage_refused(self):
        for mode,name in (("invalid",self.stage),("prepare","../latest.json"),("publish","latest.json")):
            with self.assertRaises(ValueError):
                self.run_mode(mode,name)
        self.run_mode("prepare")
        (self.base/"inventory"/self.stage).unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_mode("publish")

if __name__=="__main__":
    unittest.main()
