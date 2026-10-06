"""单元测试：规范化、稳定标识、提交裁决、租约、阶段单调、发布唯一。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.config import Config  # noqa: E402
from app.storage import Storage  # noqa: E402


def make_config() -> Config:
    tmp = tempfile.mkdtemp()
    return Config(data_dir=Path(tmp))


def sample_records():
    return [
        {
            "ship_id": "HC-1", "track_id": "T1", "timestamp": "2026-10-06T00:00:00Z",
            "longitude": 121.5, "latitude": 31.2, "sog": 7.0, "depth": 20.0, "note": "a",
        }
    ]


class NormalizeTests(unittest.TestCase):
    def test_field_order_and_float_rounding(self):
        a = models.normalize_record({"note": "x", "depth": 28.6000001, "ship_id": "s"})
        b = models.normalize_record({"ship_id": "s", "depth": 28.6, "note": "x"})
        self.assertEqual(a, b)
        self.assertEqual(list(a.keys()), ["ship_id", "depth", "note"])

    def test_rules_normalized_dedup_and_sort(self):
        r1 = models.normalize_rules({"mask_fields": [" note ", "depth", "note", "sog"]})
        r2 = models.normalize_rules({"mask_fields": ["sog", "depth", "note"]})
        self.assertEqual(r1, r2)
        self.assertEqual(r1["mask_fields"], ["depth", "note", "sog"])

    def test_stable_identifiers(self):
        recs1 = models.normalize_payload(sample_records())
        recs2 = models.normalize_payload([dict(sample_records()[0])])
        rules = models.normalize_rules({"mask_fields": ["note"]})
        self.assertEqual(
            models.make_export_id(recs1, rules),
            models.make_export_id(recs2, rules),
        )
        # 规则不同 => 标识不同
        rules2 = models.normalize_rules({"mask_fields": ["depth"]})
        self.assertNotEqual(
            models.make_export_id(recs1, rules),
            models.make_export_id(recs1, rules2),
        )
        # 业务键只看记录，与规则无关
        self.assertEqual(
            models.make_business_key(recs1), models.make_business_key(recs1)
        )

    def test_payload_validation(self):
        with self.assertRaises(ValueError):
            models.normalize_payload([])
        with self.assertRaises(ValueError):
            models.normalize_payload("x")


class AdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.storage = Storage(self.cfg.connect())
        self.storage.init_db()
        self.recs = models.normalize_payload(sample_records())
        self.rules = models.normalize_rules({"mask_fields": ["note"]})

    def test_created_then_duplicate_returns_first_receipt(self):
        r1 = self.storage.adjudicate_submission(self.recs, self.rules)
        self.assertEqual(r1["outcome"], "created")
        r2 = self.storage.adjudicate_submission(self.recs, self.rules)
        self.assertEqual(r2["outcome"], "duplicate")
        self.assertEqual(r2["export_id"], r1["export_id"])
        self.assertEqual(r2["receipt_id"], r1["receipt_id"])
        count = self.storage.conn.execute("SELECT COUNT(*) c FROM exports").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_conflict_on_different_rules_preserves_original(self):
        r1 = self.storage.adjudicate_submission(self.recs, self.rules)
        other_rules = models.normalize_rules({"mask_fields": ["depth"]})
        r2 = self.storage.adjudicate_submission(self.recs, other_rules)
        self.assertEqual(r2["outcome"], "conflict")
        self.assertTrue(r2["conflict"]["rules_changed"])
        self.assertFalse(r2["conflict"]["input_changed"])
        self.assertEqual(r2["export_id"], r1["export_id"])
        row = self.storage.get(r1["export_id"])
        self.assertEqual(row["rules_hash"], models.stable_hash(self.rules))
        count = self.storage.conn.execute("SELECT COUNT(*) c FROM exports").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_conflict_on_different_records_same_business_key(self):
        # 同船同航迹时间但浮点表示不同 => 规范化后业务键相同；改 note 内容 => 输入哈希不同
        r1 = self.storage.adjudicate_submission(self.recs, self.rules)
        changed = [dict(self.recs[0], note="b")]
        r2 = self.storage.adjudicate_submission(changed, self.rules)
        self.assertEqual(r2["outcome"], "conflict")
        self.assertTrue(r2["conflict"]["input_changed"])
        self.assertEqual(r2["export_id"], r1["export_id"])


class LeaseAndStageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.storage = Storage(self.cfg.connect())
        self.storage.init_db()
        self.recs = models.normalize_payload(sample_records())
        self.rules = models.normalize_rules({"mask_fields": []})
        self.eid = self.storage.adjudicate_submission(self.recs, self.rules)["export_id"]

    def test_lease_mutual_exclusion_and_expiry(self):
        self.assertTrue(self.storage.acquire_lease(self.eid, "A", ttl_seconds=0.3))
        self.assertFalse(self.storage.acquire_lease(self.eid, "B", ttl_seconds=0.3))
        # 持有者可重入
        self.assertTrue(self.storage.acquire_lease(self.eid, "A", ttl_seconds=0.3))
        time.sleep(0.4)
        self.assertTrue(self.storage.acquire_lease(self.eid, "B", ttl_seconds=1.0))

    def test_stage_never_regresses(self):
        self.storage.acquire_lease(self.eid, "A", ttl_seconds=5)
        self.assertTrue(self.storage.advance(self.eid, "A", "TEMP_WRITTEN"))
        self.assertTrue(self.storage.advance(self.eid, "A", "VERIFIED"))
        self.assertFalse(self.storage.advance(self.eid, "A", "ACCEPTED"))
        self.assertFalse(self.storage.advance(self.eid, "A", "TEMP_WRITTEN"))
        row = self.storage.get(self.eid)
        self.assertEqual(row["stage"], "VERIFIED")
        # 非持有者不能推进
        self.assertFalse(self.storage.advance(self.eid, "X", "PUBLISHED"))

    def test_publish_once_and_absorbing(self):
        self.storage.acquire_lease(self.eid, "A", ttl_seconds=5)
        self.storage.advance(self.eid, "A", "TEMP_WRITTEN", content_sha256="h")
        self.storage.advance(self.eid, "A", "VERIFIED", content_sha256="h")
        self.assertEqual(self.storage.commit_publish(self.eid, "A", "f.json", "h"), "PUBLISHED")
        # 并发第二次发布：同摘要幂等，不产生第二个结果
        self.assertEqual(self.storage.commit_publish(self.eid, "A", "f.json", "h"), "PUBLISHED")
        # 不同摘要 => 明确不一致，保留原证据
        self.assertEqual(self.storage.commit_publish(self.eid, "A", "f.json", "other"), "DIGEST_MISMATCH")
        # 已发布后不能再被租约领取
        self.assertFalse(self.storage.acquire_lease(self.eid, "B", ttl_seconds=5))
        row = self.storage.get(self.eid)
        self.assertEqual(row["stage"], "PUBLISHED")
        self.assertIsNone(row["lease_owner"])

    def test_publish_requires_verified_and_lease(self):
        self.storage.acquire_lease(self.eid, "A", ttl_seconds=5)
        self.assertEqual(
            self.storage.commit_publish(self.eid, "A", "f.json", "h"), "BAD_STAGE:LEASED"
        )
        self.assertEqual(
            self.storage.commit_publish(self.eid, "B", "f.json", "h"), "LEASE_LOST"
        )


if __name__ == "__main__":
    unittest.main()
