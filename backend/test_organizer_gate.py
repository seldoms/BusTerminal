"""P1-4 元数据归属收口：Organizer 默认不得再往媒体库里写东西。

方案 C 下 NFO/海报/改名/整理全部由 amane 执笔（计划 §2.4）。BusTerminal 若还写，
就会出现两个作者覆盖同一批文件。这里守住"默认不落盘"这条底线。
"""
import os
import tempfile
import unittest

from organizer import Organizer


class OrganizerGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.environ.pop("BUS_TERMINAL_LEGACY_ORGANIZER", None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_is_off(self):
        self.assertFalse(Organizer().allow_file_writes)

    def test_generate_nfo_does_not_touch_disk_by_default(self):
        organizer = Organizer()
        organizer.library_root = self.root
        nfo = organizer.generate_nfo({"id": "ABC-123", "title": "x", "detail_json": {}}, self.root)
        self.assertIsNone(nfo)
        self.assertEqual(os.listdir(self.root), [])

    def test_download_actor_thumbs_is_noop_by_default(self):
        organizer = Organizer()
        organizer.library_root = self.root
        self.assertIsNone(organizer.download_actor_thumbs([{"name": "A", "avatar": "x"}], self.root))
        self.assertEqual(os.listdir(self.root), [])

    def test_create_symlink_is_noop_by_default(self):
        organizer = Organizer()
        organizer.library_root = self.root
        self.assertFalse(organizer.create_symlink_for_movie("ABC-123", "/nowhere.mp4"))
        self.assertEqual(os.listdir(self.root), [])

    def test_explicit_optin_still_writes(self):
        organizer = Organizer(allow_file_writes=True)
        organizer.library_root = self.root
        nfo = organizer.generate_nfo(
            {"id": "ABC-123", "title": "演练片", "detail_json": {}}, self.root
        )
        self.assertTrue(nfo and os.path.isfile(nfo))

    def test_env_escape_hatch(self):
        os.environ["BUS_TERMINAL_LEGACY_ORGANIZER"] = "1"
        try:
            self.assertTrue(Organizer().allow_file_writes)
        finally:
            os.environ.pop("BUS_TERMINAL_LEGACY_ORGANIZER", None)


if __name__ == "__main__":
    unittest.main()
