"""amane 番号修复脚本的判号规则测试（scripts/fix_amane_numbers.py）。

这些规则决定「会不会把番号写到错误的媒体记录上」，写错比不写更糟，必须锁死。
测试数据全部来自生产实测（项目内部问题登记 KI-030）。
"""
import importlib.util
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fix_amane_numbers.py"
_SPEC = importlib.util.spec_from_file_location("fix_amane_numbers", _SCRIPT)
fix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fix)


class ResolveNumberTest(unittest.TestCase):
    def test_organized_path_dir_and_file_agree(self):
        """organize 后的标准布局 /hp/<studio>/<番号>/<番号>.mp4。"""
        code, source = fix.resolve_number("/hp/MBM/MBMH-097/MBMH-097.mp4")
        self.assertEqual(code, "MBMH-097")
        self.assertEqual(source, "both")

    def test_part_suffix_is_stripped(self):
        code, _ = fix.resolve_number("/hp/IP/ROE-355-C/ROE-355-C.mp4", frozenset({"ROE-355"}))
        self.assertEqual(code, "ROE-355")

    def test_conflict_picks_dir_when_both_known(self):
        """目录名与文件名都说得通时，以目录为准（目录是 amane/MoviePilot 自己的归档结果）。"""
        path = "/hp/all/FSDSS-528 体液交感不停息官能性爱/DASS-528-C,池田マリナ,FSDSS-528 体液交感不停息官能性爱.mp4"
        code, source = fix.resolve_number(path, frozenset({"FSDSS-528", "DASS-528"}))
        self.assertEqual(code, "FSDSS-528")
        self.assertEqual(source, "dir+known")

    def test_conflict_picks_file_when_dir_is_not_anchored(self):
        """`SNAP×SNAP model.021_七叶` 能解出 SNAP-021，但它不在目录名开头 —— 是标题中段抠出来的。"""
        path = "/hp/all/SNAP×SNAP model.021_七叶/786SNA-021,七原さゆ,SNAP×SNAP model.021_七叶.mp4"
        code, source = fix.resolve_number(path, frozenset({"786SNA-021", "SNAP-021"}))
        self.assertEqual(code, "786SNA-021")
        self.assertEqual(source, "file+known")

    def test_conflict_without_library_evidence_is_skipped(self):
        """两边都拿不出片库佐证就放弃 —— 宁可不填，也不填错。"""
        path = "/hp/all/SNAP×SNAP model.021_七叶/786SNA-021,七原さゆ,SNAP×SNAP model.021_七叶.mp4"
        code, source = fix.resolve_number(path)
        self.assertIsNone(code)
        self.assertEqual(source, "conflict")

    def test_watermark_filename_does_not_win_over_dir(self):
        """`4k2.mestart-647.mp4` 会解出 MESTART-647，但目录 start-647 锚定且真实存在。"""
        path = "/hp/_incoming/start-647/4k2.mestart-647.mp4"
        code, source = fix.resolve_number(path, frozenset({"START-647"}))
        self.assertEqual(code, "START-647")
        self.assertEqual(source, "dir+known")

    def test_empty_path(self):
        self.assertEqual(fix.resolve_number(""), (None, "none"))


class BuildPlanTest(unittest.TestCase):
    def test_classifies_fix_and_fill(self):
        records = [
            {"id": 1188, "path": "/hp/MBM/MBMH-097/MBMH-097.mp4", "number": "COM-956998"},
            {"id": 1181, "path": "/hp/SOD Create/START-646/START-646.mp4", "number": "MESTART-646"},
            {"id": 900, "path": "/all1/禁断NTR ... 纱罗 加贺美由奈/NXG-498.mp4", "number": ""},
            {"id": 7, "path": "/hp/A/ABC-123/ABC-123.mp4", "number": "ABC-123"},
        ]
        known = frozenset({"MBMH-097", "START-646", "NXG-498", "ABC-123"})
        plan, skipped = fix.build_plan(records, known)

        self.assertEqual([item["id"] for item in plan], [1188, 1181, 900])
        self.assertEqual([item["kind"] for item in plan], ["fix", "fix", "fill"])
        self.assertEqual(plan[0]["stored"], "COM-956998")
        self.assertEqual(plan[0]["number"], "MBMH-097")
        self.assertEqual({item["reason"] for item in skipped}, {"already_correct"})

    def test_candidate_missing_from_library_is_skipped(self):
        records = [{"id": 1, "path": "/hp/UNKNOWN-001/UNKNOWN-001.mp4", "number": ""}]
        plan, skipped = fix.build_plan(records, frozenset())
        self.assertEqual(plan, [])
        self.assertEqual(skipped[0]["reason"], "not_in_library")


class CandidateCodesTest(unittest.TestCase):
    def test_collects_dir_file_and_stored(self):
        codes = fix.candidate_codes([
            {"path": "/hp/MBM/MBMH-097/MBMH-097.mp4", "number": "COM-956998"},
        ])
        self.assertEqual(set(codes), {"MBMH-097", "COM-956998"})


if __name__ == "__main__":
    unittest.main()
