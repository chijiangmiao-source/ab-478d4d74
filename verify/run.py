"""一次性验收服务（verify）。

依次执行：
  1) 构建检查：全部 Python 源码字节码编译
  2) 代码测试：单元 + 集成测试（unittest）
  3) API/HTTP 冒烟：以真实子进程启动 API 与后台工作进程，覆盖
     - 健康响应 / 可配置宿主端口
     - 规则编辑、提交、真实 API 轮询至发布、下载摘要核验
     - 提交后改动当前规则，仍按冻结快照导出
     - 临时工件写入后进程崩溃退出，重启恢复/收敛，未核验不可下载
     - 两个工作进程并行，同一导出只发布一次且阶段不倒退
     - 业务等价重传返回首次回执；快照冲突返回 409 并保留原证据

全部通过以退出码 0 报告，任一步失败以退出码 1 报告。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method: str, url: str, body=None, timeout=5):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait_health(base: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s, b = http("GET", f"{base}/health")
            if s == 200 and b.get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise AssertionError(f"health endpoint not ready at {base}")


def wait_stage(base: str, eid: str, stage: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        s, b = http("GET", f"{base}/api/exports/{eid}")
        if s == 200:
            last = b["stage"]
            if b["stage"] == stage:
                return b
            if b["stage"] == "FAILED":
                raise AssertionError(f"{eid} FAILED: {b.get('last_error')}")
        time.sleep(0.15)
    raise AssertionError(f"{eid} 未到达 {stage}（last={last}）")


class Proc:
    def __init__(self, name: str, *args: str, env: dict | None = None):
        self.name = name
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        self.p = subprocess.Popen(
            [sys.executable, *args],
            cwd=ROOT,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def terminate(self, timeout: float = 5.0) -> None:
        if self.p.poll() is not None:
            return
        self.p.send_signal(signal.SIGTERM)
        try:
            self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()

    def wait_exit(self, timeout: float = 15.0) -> int:
        return self.p.wait(timeout)

    def output(self) -> str:
        try:
            return self.p.stdout.read() if self.p.stdout else ""
        except Exception:
            return ""


def records(ship="HC-V1", track="TV1", note="验收航迹", **extra):
    row = {
        "ship_id": ship, "track_id": track, "timestamp": "2026-10-06T08:00:00Z",
        "longitude": 121.5042, "latitude": 31.2381, "sog": 7.4, "cog": 92.0,
        "heading": 91, "depth": 28.6, "note": note,
    }
    row.update(extra)
    return [row]


# ---------------------------------------------------------------------------
# 步骤 1+2：构建检查与代码测试
# ---------------------------------------------------------------------------

def step_build_and_tests() -> None:
    print("\n=== 1) 构建检查：字节码编译 ===")
    targets = list(ROOT.glob("app/**/*.py")) + list(ROOT.glob("verify/**/*.py"))
    r = subprocess.run([sys.executable, "-m", "py_compile", *map(str, targets)], cwd=ROOT)
    if r.returncode != 0:
        raise AssertionError("py_compile 失败")
    print(f"{PASS} 编译通过（{len(targets)} 个文件）")

    print("\n=== 2) 代码测试：unittest ===")
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT
    )
    if r.returncode != 0:
        raise AssertionError("unittest 失败")
    print(f"{PASS} 全部单元/集成测试通过")


# ---------------------------------------------------------------------------
# 步骤 3：API/HTTP 冒烟
# ---------------------------------------------------------------------------

def start_api(data_dir: Path, port: int) -> Proc:
    proc = Proc(
        "api", "-m", "app.main", "--no-worker",
        env={"DATA_DIR": str(data_dir), "WEB_HOST": "0.0.0.0", "WEB_PORT": str(port),
             "PYTHONUNBUFFERED": "1"},
    )
    base = f"http://127.0.0.1:{port}"
    wait_health(base)
    return proc


def start_worker(data_dir: Path, worker_id: str, lease: float = 2.0) -> Proc:
    return Proc(
        f"worker-{worker_id}", "-m", "app.worker_main",
        env={"DATA_DIR": str(data_dir), "WORKER_ID": worker_id,
             "LEASE_SECONDS": str(lease), "POLL_SECONDS": "0.05",
             "PYTHONUNBUFFERED": "1"},
    )


def step_frozen_snapshot_and_crash_recovery() -> None:
    print("\n=== 3a) 冻结快照 + 崩溃恢复/收敛 + 下载门控 ===")
    data_dir = Path(tempfile.mkdtemp(prefix="verify-a-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    api = start_api(data_dir, port)
    try:
        print(f"    API 监听可配置宿主端口 {port}，/health 正常")

        # --- 导出 A：将在临时件后崩溃 ---
        s, b = http("PUT", f"{base}/api/rules", {"rules": {"mask_fields": ["note"]}})
        assert s == 200, b
        s, a = http("POST", f"{base}/api/exports", {"records": records(ship="A", track="A1")})
        assert s == 201, a
        eid_a = a["export_id"]

        # --- 导出 B：提交后再改规则，必须仍按冻结快照（note）导出 ---
        s, b = http("POST", f"{base}/api/exports",
                    {"records": records(ship="B", track="B1"),
                     "rules": {"mask_fields": ["note"]}})
        assert s == 201, b
        eid_b = b["export_id"]
        # 提交后值班员改动当前规则（扩大遮蔽）
        s, _ = http("PUT", f"{base}/api/rules",
                    {"rules": {"mask_fields": ["note", "depth", "sog", "cog"]}})
        assert s == 200

        # 未发布前下载必须被拒绝（不暴露未核验内容）
        s, b = http("GET", f"{base}/api/exports/{eid_a}/artifact")
        assert s == 409 and b["error"] == "not_published", b

        # 设置崩溃注入：A 写临时工件后进程猝死
        (data_dir / "crash_after_temp").write_text(eid_a)

        w1 = start_worker(data_dir, "w1-crash", lease=2.0)
        rc = w1.wait_exit(timeout=20)
        out = w1.output()
        assert rc == 3, f"崩溃进程退出码应为 3，实际 {rc}\n{out}"
        print("    工作进程在临时工件写入后以退出码 3 猝死（模拟成功）")

        time.sleep(0.3)
        # 崩溃现场：阶段 TEMP_WRITTEN，有残留临时件，下载仍被拒绝
        s, st = http("GET", f"{base}/api/exports/{eid_a}")
        assert st["stage"] == "TEMP_WRITTEN", st["stage"]
        tmps = list((data_dir / "artifacts" / "tmp").iterdir())
        assert tmps, "崩溃后应残留临时工件"
        s, b = http("GET", f"{base}/api/exports/{eid_a}/artifact")
        assert s == 409, b
        print(f"    崩溃现场：阶段={st['stage']}，残留临时件 {len(tmps)} 个，下载拒绝 409")

        # 重启新工作进程，等租约过期后恢复/收敛
        w2 = start_worker(data_dir, "w2-restart", lease=2.0)
        try:
            st_a = wait_stage(base, eid_a, "PUBLISHED")
            st_b = wait_stage(base, eid_b, "PUBLISHED")

            # A：恢复后唯一完整工件，摘要与登记一致
            with urllib.request.urlopen(
                f"{base}/api/exports/{eid_a}/artifact", timeout=5
            ) as r:
                raw_a = r.read()
            import hashlib
            assert hashlib.sha256(raw_a).hexdigest() == st_a["content_sha256"]
            doc_a = json.loads(raw_a)
            assert doc_a["records"][0]["note"] == "***MASKED***"
            assert doc_a["records"][0]["depth"] == 28.6
            assert st_a["stages"][0]["stage"] == "ACCEPTED"
            assert st_a["stages"][-1]["stage"] == "PUBLISHED"

            # B：规则是提交后改的，仍按冻结快照只遮蔽 note
            with urllib.request.urlopen(
                f"{base}/api/exports/{eid_b}/artifact", timeout=5
            ) as r:
                doc_b = json.loads(r.read())
            assert doc_b["records"][0]["note"] == "***MASKED***"
            assert doc_b["records"][0]["depth"] == 28.6, "depth 不应被提交后新增的规则遮蔽"
            assert doc_b["rule_snapshot"]["mask_fields"] == ["note"]

            time.sleep(0.3)
            assert list((data_dir / "artifacts" / "tmp").iterdir()) == [], "临时件应被清理"
            published = sorted(p.name for p in (data_dir / "artifacts" / "published").iterdir())
            assert published == sorted([f"{eid_a}.json", f"{eid_b}.json"]), published

            # 阶段不得倒退：日志顺序必须严格沿阶段机前进
            rank = {"ACCEPTED": 0, "LEASED": 1, "TEMP_WRITTEN": 2, "VERIFIED": 3,
                    "PUBLISHED": 4, "FAILED": 5}
            for st_x in (st_a, st_b):
                rr = [rank[x["stage"]] for x in st_x["stages"]]
                assert rr == sorted(rr) and rr[-1] == 4, rr
        finally:
            w2.terminate()
        print(f"{PASS} 崩溃恢复收敛到同一完整工件；冻结快照不受后续规则改动影响；下载门控有效")
    finally:
        api.terminate()


def step_parallel_workers() -> None:
    print("\n=== 3b) 两个工作进程并行：只发布一次、阶段不倒退 ===")
    data_dir = Path(tempfile.mkdtemp(prefix="verify-b-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    api = start_api(data_dir, port)
    w1 = start_worker(data_dir, "p1", lease=3.0)
    w2 = start_worker(data_dir, "p2", lease=3.0)
    try:
        ids = []
        for i in range(6):
            s, b = http(
                "POST", f"{base}/api/exports",
                {"records": records(ship=f"PAR-{i}", track=f"PT-{i}", depth=20.0 + i)},
            )
            assert s == 201, b
            ids.append(b["export_id"])
        rank = {"ACCEPTED": 0, "LEASED": 1, "TEMP_WRITTEN": 2, "VERIFIED": 3,
                "PUBLISHED": 4, "FAILED": 5}
        for eid in ids:
            st = wait_stage(base, eid, "PUBLISHED", timeout=40)
            rr = [rank[x["stage"]] for x in st["stages"]]
            assert rr == sorted(rr) and rr[-1] == 4, (eid, rr)
        files = sorted(p.name for p in (data_dir / "artifacts" / "published").iterdir())
        assert len(files) == 6 and len(set(files)) == 6, files
        time.sleep(0.3)
        assert list((data_dir / "artifacts" / "tmp").iterdir()) == []
        print(f"{PASS} 6 个导出在双进程竞争下各发布一次，共 6 个唯一工件，无阶段倒退")
    finally:
        w1.terminate()
        w2.terminate()
        api.terminate()


def step_retransmit_and_conflict() -> None:
    print("\n=== 3c) 等价重传返回首次回执；快照冲突 409 并保留证据 ===")
    data_dir = Path(tempfile.mkdtemp(prefix="verify-c-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    api = start_api(data_dir, port)
    w = start_worker(data_dir, "rt1", lease=3.0)
    try:
        s, _ = http("PUT", f"{base}/api/rules", {"rules": {"mask_fields": ["note"]}})
        assert s == 200
        s, first = http("POST", f"{base}/api/exports",
                        {"records": records(ship="RT", track="RT1", note="first")})
        assert s == 201, first
        eid, receipt = first["export_id"], first["receipt_id"]
        wait_stage(base, eid, "PUBLISHED")

        # 业务等价重传（键序打乱、不附 rules 而当前规则相同）
        same = [dict(reversed(list(records(ship="RT", track="RT1", note="first")[0].items())))]
        s, dup = http("POST", f"{base}/api/exports", {"records": same})
        assert s == 200 and dup["outcome"] == "duplicate", dup
        assert dup["export_id"] == eid and dup["receipt_id"] == receipt, dup

        # 规则快照不同 => 409 明确冲突
        s, conf = http("POST", f"{base}/api/exports",
                       {"records": records(ship="RT", track="RT1", note="first"),
                        "rules": {"mask_fields": ["depth"]}})
        assert s == 409 and conf["outcome"] == "conflict", conf
        assert conf["rules_changed"] and conf["existing_export_id"] == eid, conf

        # 记录内容不同但业务身份相同 => 同样冲突
        s, conf2 = http("POST", f"{base}/api/exports",
                        {"records": records(ship="RT", track="RT1", note="tampered"),
                         "rules": {"mask_fields": ["note"]}})
        assert s == 409 and conf2["input_changed"], conf2

        # 原有证据保留：仍只有一个工件且仍可下载
        files = list((data_dir / "artifacts" / "published").iterdir())
        assert len(files) == 1, files
        with urllib.request.urlopen(f"{base}/api/exports/{eid}/artifact", timeout=5) as r:
            doc = json.loads(r.read())
        assert doc["records"][0]["note"] == "***MASKED***"
        assert doc["export_id"] == eid
        print(f"{PASS} 重传返回首次回执 {receipt[:16]}… 且无第二工件；两类冲突均 409，原证据完好")
    finally:
        w.terminate()
        api.terminate()


def main() -> int:
    print("海洋测绘航迹导出 —— 一次性验收（verify）")
    failures: list[str] = []
    for name, fn in [
        ("build_and_tests", step_build_and_tests),
        ("frozen_snapshot_and_crash_recovery", step_frozen_snapshot_and_crash_recovery),
        ("parallel_workers", step_parallel_workers),
        ("retransmit_and_conflict", step_retransmit_and_conflict),
    ]:
        try:
            fn()
        except Exception as exc:
            failures.append(name)
            print(f"{FAIL} 场景 [{name}] 失败：{type(exc).__name__}: {exc}")

    print("\n================ 验收结论 ================")
    if failures:
        for name in failures:
            print(f"  {FAIL} {name}")
        print(f"结果：{len(failures)} 个场景失败")
        return 1
    print(f"  {PASS} 构建检查 / 代码测试 / 全部 API/HTTP 冒烟场景通过")
    print("结果：验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
