import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class OceanSyncFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.voyage = seed_demo(self.db)["voyage"]

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, kind, uuid, revision, data, device="tablet-A"):
        return {"type": kind, "local_uuid": uuid, "revision": revision, "data": data}

    def test_full_offline_sync_conflict_confirm_and_file_dedupe(self):
        station = {"voyage_id": self.voyage, "station_code": "S-01", "latitude": 30.1, "longitude": 122.0, "sampled_at": "2026-09-05T08:30:00+08:00", "owner": "member-a"}
        batch = {"device_id": "tablet-A", "records": [self.record("station", "st-001", 1, station)]}
        first = self.db.sync("member-a", "member", batch)
        self.assertEqual(first["created"], 1)
        duplicate = self.db.sync("member-a", "member", batch)
        self.assertEqual(duplicate["duplicates"], 1)
        station_id = self.db.list_stations()[0]["id"]

        sample = {"station_id": station_id, "sample_code": "W-001", "sample_type": "water", "depth_m": 5, "storage_condition": "4C", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 1, sample)]})
        updated = dict(sample, storage_condition="negative-20C")
        result = self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 2, updated)]})
        self.assertEqual(result["updated"], 1)
        stale = self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 1, sample)]})
        self.assertEqual(len(stale["conflicts"]), 1)
        sample_id = self.db.list_samples()[0]["id"]
        self.db.confirm("sample", sample_id, "lead-01", "lead")
        locked = self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 3, dict(updated, depth_m=6))]})
        self.assertIn("不能覆盖", locked["conflicts"][0]["reason"])

        custody = {"sample_id": sample_id, "event_type": "handover", "from_party": "member-a", "to_party": "shore-lab", "occurred_at": "2026-09-26T09:00:00+08:00"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("custody", "custody-001", 1, custody)]})
        self.assertEqual(len(self.db.list_custody()), 1)

        digest = "a" * 64
        file_record = {"voyage_id": self.voyage, "station_id": station_id, "file_name": "ctd.csv", "sha256": digest, "size_bytes": 120, "captured_at": "2026-09-05T08:20:00+08:00"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("instrument_file", "file-001", 1, file_record)]})
        duplicate_file = self.db.sync("member-b", "member", {"device_id": "tablet-B", "records": [self.record("instrument_file", "file-002", 1, file_record, "tablet-B")]})
        self.assertEqual(duplicate_file["duplicates"], 1)
        self.assertEqual(len(self.db.list_files()), 1)

    def test_duplicate_code_and_member_update_are_isolated(self):
        station = {"voyage_id": self.voyage, "station_code": "S-01", "latitude": 30.1, "longitude": 122.0, "sampled_at": "2026-09-05T08:30:00+08:00", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record("station", "st-a", 1, station, "A")]})
        station_id = self.db.list_stations()[0]["id"]
        sample = {"station_id": station_id, "sample_code": "W-001", "sample_type": "water", "depth_m": 5, "storage_condition": "4C", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record("sample", "sample-a", 1, sample, "A")]})
        conflict = self.db.sync("member-b", "member", {"device_id": "B", "records": [self.record("sample", "sample-b", 1, sample, "B")]})
        self.assertEqual(conflict["created"], 1)
        self.assertIn("已分配", conflict["conflicts"][0]["reason"])
        samples = self.db.list_samples()
        sample_b = next(item for item in samples if item["id"] != samples[0]["id"])
        unauthorized = self.db.sync("member-c", "member", {"device_id": "B", "records": [self.record("sample", "sample-b", 2, dict(sample, sample_code=sample_b["sample_code"], depth_m=9), "B")]})
        self.assertIn("只有记录人", unauthorized["conflicts"][0]["reason"])


class SplitSampleFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.voyage = seed_demo(self.db)["voyage"]

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, kind, uuid, revision, data, device="tablet-A"):
        return {"type": kind, "local_uuid": uuid, "revision": revision, "data": data}

    def make_parent(self, code="W-001"):
        station = {"voyage_id": self.voyage, "station_code": "S-01", "latitude": 30.1, "longitude": 122.0, "sampled_at": "2026-09-05T08:30:00+08:00", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record("station", "st-1", 1, station, "A")]})
        station_id = self.db.list_stations()[0]["id"]
        sample = {"station_id": station_id, "sample_code": code, "sample_type": "water", "depth_m": 5, "storage_condition": "4C", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record("sample", "s-parent", 1, sample, "A")]})
        rows = self.db.list_samples()
        return station_id, next(s["id"] for s in rows if s["sample_code"] == code)
    def custody_record(self, sample_id, to_party, uuid):
        return {"device_id": "A", "records": [self.record(
            "custody", uuid, 1,
            {"sample_id": sample_id, "event_type": "handover", "from_party": "member-a",
             "to_party": to_party, "occurred_at": "2026-09-26T09:00:00+08:00"}, "A")]}

    def test_split_numbers_inherit_and_continues(self):
        station_id, parent_id = self.make_parent()
        first = self.db.split_sample(parent_id, "member-a", {})
        self.assertEqual(first["sample_code"], "W-001-1")
        self.assertEqual(first["station_id"], station_id)
        self.assertEqual(first["voyage_id"], self.voyage)
        self.assertEqual(first["parent_sample_id"], parent_id)
        # 类型、深度、保存条件、负责人继承母样。
        self.assertEqual((first["sample_type"], first["depth_m"], first["storage_condition"], first["owner"]),
                         ("water", 5.0, "4C", "member-a"))
        second = self.db.split_sample(parent_id, "member-a", {"storage_condition": "-20C", "depth_m": 12})
        self.assertEqual(second["sample_code"], "W-001-2")
        self.assertEqual((second["storage_condition"], second["depth_m"]), ("-20C", 12.0))
        # 同步通道也能按母样续号，缺省编号时服务端自动分配。
        result = self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record(
            "sample", "s-child-sync", 1,
            {"station_id": station_id, "parent_sample_id": parent_id, "sample_type": "water",
             "depth_m": 1, "storage_condition": "4C", "owner": "member-a"}, "A")]})
        self.assertEqual(result["created"], 1)
        codes = {s["sample_code"] for s in self.db.list_samples()}
        self.assertEqual(codes, {"W-001", "W-001-1", "W-001-2", "W-001-3"})

    def test_split_collision_gets_dup_suffix_without_overwrite(self):
        _, parent_id = self.make_parent()
        child = self.db.split_sample(parent_id, "member-a", {})
        self.assertEqual(child["sample_code"], "W-001-1")
        # 另一设备先占用 W-001-2，再分样时编号撞号：-DUP 后缀进隔离清单，原记录不被覆盖。
        existing = {"station_id": child["station_id"], "sample_code": "W-001-2", "sample_type": "water",
                    "depth_m": 5, "storage_condition": "4C", "owner": "member-b"}
        self.db.sync("member-b", "member", {"device_id": "B", "records": [self.record("sample", "s-other", 1, existing, "B")]})
        clash = self.db.split_sample(parent_id, "member-a", {})
        self.assertTrue(clash["sample_code"].startswith("W-001-2-DUP-"))
        self.assertEqual(clash["requested_code"], "W-001-2")
        self.assertIn("已分配", clash["conflict"]["reason"])
        conflicts = self.db.list_conflicts()
        self.assertTrue(any("W-001-2" in c["reason"] and "DUP" in c["reason"] for c in conflicts))
        # 原记录 W-001-2 保持不变，且分样树指向关系正确。
        rows = {s["sample_code"]: s for s in self.db.list_samples()}
        self.assertEqual(rows["W-001-2"]["owner"], "member-b")
        self.assertEqual(rows[clash["sample_code"]]["parent_sample_id"], parent_id)

    def test_locked_parent_blocks_new_splits_but_children_still_handover(self):
        _, parent_id = self.make_parent()
        child = self.db.split_sample(parent_id, "member-a", {})
        # 子样不能再作为母样分样。
        with self.assertRaisesRegex(DomainError, "只能在母样上分样"):
            self.db.split_sample(child["id"], "member-a", {})
        self.db.confirm("sample", parent_id, "lead-01", "lead")
        with self.assertRaisesRegex(DomainError, "确认锁定"):
            self.db.split_sample(parent_id, "member-a", {})
        # 同步通道同样不能对锁定母样新增子样。
        with self.assertRaisesRegex(DomainError, "确认锁定"):
            self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record(
                "sample", "s-child-late", 1,
                {"station_id": child["station_id"], "parent_sample_id": parent_id, "sample_code": "W-001-9",
                 "sample_type": "water", "depth_m": 1, "storage_condition": "4C", "owner": "member-a"}, "A")]})
        # 已分出的子样仍可单独交接。
        self.db.sync("member-a", "member", self.custody_record(child["id"], "lab-1", "cu-1"))
        self.db.sync("member-a", "member", self.custody_record(child["id"], "lab-2", "cu-2"))
        child_custody = [c for c in self.db.list_custody() if c["sample_id"] == child["id"]]
        self.assertEqual([c["to_party"] for c in child_custody], ["lab-2", "lab-1"])

    def test_child_must_inherit_parent_voyage_and_station(self):
        station_id, parent_id = self.make_parent()
        # 直接分样接口拒绝改挂别的站位。
        with self.assertRaisesRegex(DomainError, "不能改挂别的站位"):
            self.db.split_sample(parent_id, "member-a", {"station_id": station_id + 999})
        # 同步通道同样拒绝站位不一致的子样。
        bad = {"station_id": station_id + 999, "parent_sample_id": parent_id, "sample_code": "W-001-1",
               "sample_type": "water", "depth_m": 1, "storage_condition": "4C", "owner": "member-a"}
        with self.assertRaises(DomainError):
            self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record("sample", "s-bad", 1, bad, "A")]})
        # 继承母样站位与航次的子样正常建立。
        ok = self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record(
            "sample", "s-good", 1,
            {"station_id": station_id, "parent_sample_id": parent_id, "sample_code": "W-001-1",
             "sample_type": "water", "depth_m": 1, "storage_condition": "4C", "owner": "member-a"}, "A")]})
        self.assertEqual(ok["created"], 1)


if __name__ == "__main__":
    unittest.main()
