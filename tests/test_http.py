"""HTTP 端到端 + 双工作进程并行：真实 socket、真实 API 轮询。"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.config import Config  # noqa: E402
from app.server import build_server  # noqa: E402
from app.storage import Storage  # noqa: E402
from app.worker import Worker  # noqa: E402


def http(method: str, url: str, body=None, expect=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return resp.status, payload
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read().decode("utf-8"))
        if expect is not None:
            assert e.code == expect, f"expected {expect}, got {e.code}: {payload}"
        return e.code, payload


class Harness:
    def __init__(self, num_workers=1):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(data_dir=self.tmp, host="127.0.0.1", port=0,
                          lease_seconds=2, heartbeat_seconds=1, poll_seconds=0.02)
        self.server: ThreadingHTTPServer = build_server(self.cfg, quiet=True)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.t = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        self.t.start()
        self.workers = [Worker(Config(data_dir=self.tmp, host="127.0.0.1", port=0,
                                      worker_id=f"w{i}", lease_seconds=2,
                                      heartbeat_seconds=1, poll_seconds=0.02))
                        for i in range(num_workers)]
        self.wthreads = [threading.Thread(target=w.run_forever, daemon=True) for w in self.workers]
        for wt in self.wthreads:
            wt.start()

    def stop(self):
        for w in self.workers:
            w.stop()
        self.server.shutdown()

    def wait_stage(self, eid: str, stage: str, timeout=10.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            status, body = http("GET", f"{self.base}/api/exports/{eid}")
            last = body["stage"]
            if body["stage"] == stage:
                return body
            if body["stage"] == "FAILED":
                raise AssertionError(f"export failed: {body.get('last_error')}")
            time.sleep(0.05)
        raise AssertionError(f"stage never reached {stage}; last={last}")


def sample_records(note="例行"):
    return [{"ship_id": "HC-9", "track_id": "T9", "timestamp": "2026-10-06T08:00:00Z",
             "longitude": 121.504, "latitude": 31.238, "sog": 7.4, "cog": 92.0,
             "heading": 91, "depth": 28.6, "note": note}]


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(num_workers=1)

    def tearDown(self):
        self.h.stop()

    def test_health_and_page(self):
        s, b = http("GET", f"{self.h.base}/health")
        self.assertEqual(s, 200)
        self.assertEqual(b["status"], "ok")
        with urllib.request.urlopen(f"{self.h.base}/", timeout=5) as r:
            self.assertIn("航迹导出值班台", r.read().decode("utf-8"))

    def test_full_flow_frozen_rules_download_gated(self):
        # 设定当前规则
        s, b = http("PUT", f"{self.h.base}/api/rules", {"rules": {"mask_fields": ["note"]}})
        self.assertEqual(s, 200)
        s, b = http("GET", f"{self.h.base}/api/rules")
        self.assertEqual(b["rules"]["mask_fields"], ["note"])

        # 提交
        s, b = http("POST", f"{self.h.base}/api/exports", {"records": sample_records()})
        self.assertEqual(s, 201)
        self.assertEqual(b["outcome"], "created")
        eid = b["export_id"]
        receipt = b["receipt_id"]
        self.assertTrue(eid.startswith("exp_"))

        # 轮询直到发布（页面同款真实 API 轮询）
        st = self.h.wait_stage(eid, "PUBLISHED")
        self.assertTrue(st["download_available"])

        # 下载并核验内容与摘要
        with urllib.request.urlopen(f"{self.h.base}/api/exports/{eid}/artifact", timeout=5) as r:
            raw = r.read()
        doc = json.loads(raw)
        self.assertEqual(doc["records"][0]["note"], "***MASKED***")
        self.assertEqual(doc["records"][0]["depth"], 28.6)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), st["content_sha256"])

    def test_rule_change_after_submit_uses_frozen_snapshot(self):
        http("PUT", f"{self.h.base}/api/rules", {"rules": {"mask_fields": ["note"]}})
        s, b = http("POST", f"{self.h.base}/api/exports",
                    {"records": sample_records(), "rules": {"mask_fields": ["note"]}})
        eid = b["export_id"]
        # 提交之后再改当前规则：扩大遮蔽
        http("PUT", f"{self.h.base}/api/rules",
             {"rules": {"mask_fields": ["note", "depth", "sog"]}})
        self.h.wait_stage(eid, "PUBLISHED")
        with urllib.request.urlopen(f"{self.h.base}/api/exports/{eid}/artifact", timeout=5) as r:
            doc = json.loads(r.read())
        self.assertEqual(doc["records"][0]["note"], "***MASKED***")
        self.assertEqual(doc["records"][0]["depth"], 28.6)
        self.assertEqual(doc["records"][0]["sog"], 7.4)
        self.assertEqual(doc["rule_snapshot"]["mask_fields"], ["note"])

    def test_duplicate_returns_first_receipt_single_artifact(self):
        http("PUT", f"{self.h.base}/api/rules", {"rules": {"mask_fields": []}})
        s, b1 = http("POST", f"{self.h.base}/api/exports", {"records": sample_records()})
        eid = b1["export_id"]
        self.h.wait_stage(eid, "PUBLISHED")
        # 业务等价重传（JSON 键序不同也必须等价）
        reordered = [dict(reversed(list(sample_records()[0].items())))]
        s, b2 = http("POST", f"{self.h.base}/api/exports", {"records": reordered})
        self.assertEqual(s, 200)
        self.assertEqual(b2["outcome"], "duplicate")
        self.assertEqual(b2["receipt_id"], b1["receipt_id"])
        self.assertEqual(b2["export_id"], eid)
        # 只产生一个工件
        files = list((self.h.tmp / "artifacts" / "published").iterdir())
        self.assertEqual(len(files), 1)

    def test_conflict_rejected_and_original_evidence_kept(self):
        http("PUT", f"{self.h.base}/api/rules", {"rules": {"mask_fields": ["note"]}})
        s, b1 = http("POST", f"{self.h.base}/api/exports", {"records": sample_records()})
        eid = b1["export_id"]
        self.h.wait_stage(eid, "PUBLISHED")
        # 同船同航迹（业务等价）但规则快照不同
        s, b2 = http("POST", f"{self.h.base}/api/exports",
                     {"records": sample_records(), "rules": {"mask_fields": ["depth"]}})
        self.assertEqual(s, 409)
        self.assertEqual(b2["outcome"], "conflict")
        self.assertTrue(b2["rules_changed"])
        # 原有证据保留：仍可下载且仍按原快照遮蔽 note
        with urllib.request.urlopen(f"{self.h.base}/api/exports/{eid}/artifact", timeout=5) as r:
            doc = json.loads(r.read())
        self.assertEqual(doc["records"][0]["note"], "***MASKED***")
        self.assertEqual(doc["records"][0]["depth"], 28.6)
        files = list((self.h.tmp / "artifacts" / "published").iterdir())
        self.assertEqual(len(files), 1)


class DownloadGatingTests(unittest.TestCase):
    """无工作进程时：未核验内容绝不可下载。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(data_dir=self.tmp, host="127.0.0.1", port=0)
        self.server = build_server(self.cfg, quiet=True)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()

    def test_download_before_publish_refused(self):
        s, b = http("POST", f"{self.base}/api/exports", {"records": sample_records()})
        self.assertEqual(s, 201)
        eid = b["export_id"]
        s, b = http("GET", f"{self.base}/api/exports/{eid}/artifact", expect=409)
        self.assertEqual(b["error"], "not_published")
        # 不存在的导出 404
        http("GET", f"{self.base}/api/exports/exp_doesnotexist/artifact", expect=404)
        # 磁盘上即使有临时文件，published 目录也为空
        self.assertEqual(list((self.tmp / "artifacts" / "published").iterdir()), [])


class ParallelWorkersTests(unittest.TestCase):
    def test_two_workers_publish_each_export_once(self):
        h = Harness(num_workers=2)
        try:
            http("PUT", f"{h.base}/api/rules", {"rules": {"mask_fields": ["note"]}})
            ids = []
            for i in range(8):
                recs = [{"ship_id": f"HC-P{i}", "track_id": f"TP{i}",
                         "timestamp": "2026-10-06T08:00:00Z",
                         "longitude": 121.5 + i, "latitude": 31.2, "sog": 7.0,
                         "depth": 20.0 + i, "note": "p"}]
                s, b = http("POST", f"{h.base}/api/exports", {"records": recs})
                self.assertEqual(s, 201)
                ids.append(b["export_id"])
            for eid in ids:
                st = h.wait_stage(eid, "PUBLISHED", timeout=15)
                # 阶段日志必须单调，不得倒退
                ranks = {s: i for i, s in enumerate(
                    ["ACCEPTED", "LEASED", "TEMP_WRITTEN", "VERIFIED", "PUBLISHED", "FAILED"])}
                rr = [ranks[x["stage"]] for x in st["stages"]]
                self.assertEqual(rr, sorted(rr))
            files = sorted(p.name for p in (h.tmp / "artifacts" / "published").iterdir())
            self.assertEqual(len(files), 8)
            self.assertEqual(len(set(files)), 8)
            # 临时目录最终清空
            time.sleep(0.2)
            self.assertEqual(list((h.tmp / "artifacts" / "tmp").iterdir()), [])
        finally:
            h.stop()


if __name__ == "__main__":
    unittest.main()
