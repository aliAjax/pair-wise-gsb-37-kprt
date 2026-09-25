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


class SampleSplitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.voyage = seed_demo(self.db)["voyage"]
        station = {"voyage_id": self.voyage, "station_code": "S-01", "latitude": 30.1, "longitude": 122.0, "sampled_at": "2026-09-05T08:30:00+08:00", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [{"type": "station", "local_uuid": "st-001", "revision": 1, "data": station}]})
        self.station_id = self.db.list_stations()[0]["id"]
        sample = {"station_id": self.station_id, "sample_code": "W-001", "sample_type": "water", "depth_m": 5, "storage_condition": "4C", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [{"type": "sample", "local_uuid": "sample-001", "revision": 1, "data": sample}]})
        self.parent = self.db.list_samples()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_split_assigns_sequential_codes_and_inherits_voyage_station(self):
        first = self.db.split_sample(self.parent["id"], "member-a", {})
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["children"][0]["sample_code"], "W-001-1")
        second = self.db.split_sample(self.parent["id"], "member-a", {"count": 2})
        self.assertEqual([c["sample_code"] for c in second["children"]], ["W-001-2", "W-001-3"])
        for child in first["children"] + second["children"]:
            self.assertEqual(child["parent_sample_id"], self.parent["id"])
            self.assertEqual(child["voyage_id"], self.parent["voyage_id"])
            self.assertEqual(child["station_id"], self.parent["station_id"])
            self.assertEqual(child["confirmed"], 0)

    def test_split_code_collision_is_quarantined_without_overwrite(self):
        other = {"station_id": self.station_id, "sample_code": "W-001-1", "sample_type": "water", "depth_m": 5, "storage_condition": "4C", "owner": "member-b"}
        self.db.sync("member-b", "member", {"device_id": "tablet-B", "records": [{"type": "sample", "local_uuid": "sample-b", "revision": 1, "data": other}]})
        result = self.db.split_sample(self.parent["id"], "member-a", {})
        child = result["children"][0]
        self.assertTrue(child["sample_code"].startswith("W-001-1-DUP-"))
        self.assertEqual(child["parent_sample_id"], self.parent["id"])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertIn("已分配", result["conflicts"][0]["reason"])
        self.assertEqual(len(self.db.list_conflicts()), 1)
        original = next(s for s in self.db.list_samples() if s["sample_code"] == "W-001-1")
        self.assertEqual(original["owner"], "member-b")
        self.assertIsNone(original["parent_sample_id"])
        again = self.db.split_sample(self.parent["id"], "member-a", {})
        self.assertEqual(again["children"][0]["sample_code"], "W-001-2")

    def test_confirmed_parent_cannot_split_but_child_handover_still_works(self):
        child = self.db.split_sample(self.parent["id"], "member-a", {})["children"][0]
        self.db.confirm("sample", self.parent["id"], "lead-01", "lead")
        with self.assertRaises(DomainError) as ctx:
            self.db.split_sample(self.parent["id"], "member-a", {})
        self.assertEqual(ctx.exception.status, 409)
        custody = {"sample_id": child["id"], "event_type": "handover", "from_party": "member-a", "to_party": "shore-lab", "occurred_at": "2026-09-26T09:00:00+08:00"}
        result = self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [{"type": "custody", "local_uuid": "custody-001", "revision": 1, "data": custody}]})
        self.assertEqual(result["created"], 1)
        self.assertEqual(self.db.list_custody()[0]["sample_id"], child["id"])

    def test_split_rejects_different_station_or_voyage(self):
        station2 = {"voyage_id": self.voyage, "station_code": "S-02", "latitude": 31.0, "longitude": 123.0, "sampled_at": "2026-09-06T08:30:00+08:00", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [{"type": "station", "local_uuid": "st-002", "revision": 1, "data": station2}]})
        station2_id = next(s["id"] for s in self.db.list_stations() if s["station_code"] == "S-02")
        with self.assertRaises(DomainError):
            self.db.split_sample(self.parent["id"], "member-a", {"station_id": station2_id})
        with self.assertRaises(DomainError):
            self.db.split_sample(self.parent["id"], "member-a", {"voyage_id": self.voyage + 1})
        ok = self.db.split_sample(self.parent["id"], "member-a", {"station_id": self.station_id, "voyage_id": self.voyage})
        self.assertEqual(ok["created"], 1)
        self.assertEqual(len(self.db.list_samples()), 2)


if __name__ == "__main__":
    unittest.main()
