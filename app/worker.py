"""后台工作进程：持租约处理，临时工件 -> 摘要核验 -> 原子发布，支持崩溃恢复。"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Optional

from . import models
from .config import Config
from .storage import Storage


class Worker:
    def __init__(self, config: Config):
        self.config = config
        # 连接惰性建立并绑定到实际运行的线程（SQLite 连接有线程亲和性）
        self.conn = None
        self.storage: Optional[Storage] = None
        self._bound_thread: Optional[int] = None
        self.owner = f"{config.worker_id}-{uuid.uuid4().hex[:8]}"
        self.tmp_dir = config.artifact_dir / "tmp"
        self.pub_dir = config.artifact_dir / "published"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.pub_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._bind()

    def _bind(self) -> None:
        tid = threading.get_ident()
        if self.storage is not None and self._bound_thread == tid:
            return
        # 不同线程复用同一 Worker（如测试/重启）时重新建连
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = self.config.connect()
        self.storage = Storage(self.conn)
        self.storage.init_db()
        self._bound_thread = tid

    # ---- 工件路径与摘要 -----------------------------------------------

    def _tmp_path(self, export_id: str) -> Path:
        return self.tmp_dir / f"{export_id}.tmp.{self.owner}.{uuid.uuid4().hex[:8]}"

    def _final_path(self, export_id: str) -> Path:
        return self.pub_dir / f"{export_id}.json"

    def _temps_for(self, export_id: str) -> list[Path]:
        return sorted(self.tmp_dir.glob(f"{export_id}.tmp.*"))

    @staticmethod
    def _digest(path: Path) -> Optional[str]:
        try:
            h = models.sha256_bytes(path.read_bytes())
            return h
        except FileNotFoundError:
            return None

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_temp(self, path: Path, content: bytes) -> None:
        """先写临时工件并 fsync，崩溃也不会留下半截最终文件。"""
        with open(path, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        self._fsync_dir(self.tmp_dir)

    # ---- 恢复/收敛 ----------------------------------------------------

    def _reconcile(
        self, export_id: str, expected: bytes, expected_sha: str
    ) -> tuple[Optional[Path], list[str]]:
        """根据阶段日志与摘要清理残缺工件，或收敛到摘要匹配的完整工件。

        返回 (可直接用于发布的临时工件路径或 None, 恢复说明列表)。
        """
        notes: list[str] = []
        # 1) 最终工件若已存在且摘要匹配，直接收敛，绝不产生第二个工件
        final = self._final_path(export_id)
        if final.exists():
            if self._digest(final) == expected_sha:
                for t in self._temps_for(export_id):
                    t.unlink(missing_ok=True)
                notes.append("final artifact already complete and digest-matched; converged")
                return None, notes
            notes.append("final artifact corrupt (digest mismatch); removed")
            final.unlink(missing_ok=True)

        # 2) 在残留临时工件中找摘要匹配者（崩溃后重启的收敛路径）
        good: Optional[Path] = None
        for tmp in self._temps_for(export_id):
            if self._digest(tmp) == expected_sha:
                good = tmp
                notes.append(f"recovered complete temp artifact {tmp.name} by digest")
            else:
                tmp.unlink(missing_ok=True)
                notes.append(f"removed partial temp artifact {tmp.name}")
        if good is not None:
            # 收敛到他人（或自己上一世）留下的同一完整工件，删除其余临时件
            for tmp in self._temps_for(export_id):
                if tmp != good:
                    tmp.unlink(missing_ok=True)
            return good, notes

        # 3) 无可用工件，按冻结快照重新生成
        fresh = self._tmp_path(export_id)
        self._write_temp(fresh, expected)
        if not notes:
            notes.append("no prior artifact; wrote fresh temp from frozen snapshot")
        return fresh, notes

    # ---- 崩溃注入 -----------------------------------------------------

    def _maybe_crash(self, export_id: str) -> None:
        marker = self.config.crash_marker
        try:
            target = marker.read_text().strip()
        except FileNotFoundError:
            return
        if target == export_id or target == "*":
            # 模拟进程在临时工件写入后、发布前猝死：删除标记避免崩溃循环后硬退出
            marker.unlink(missing_ok=True)
            self._fsync_dir(self.config.data_dir)
            os._exit(3)

    # ---- 单次处理 -----------------------------------------------------

    def process_once(self, export_id: str) -> None:
        self._bind()
        # 可重入租约：主循环已 claim 时是续租；直接调用时确保持有有效租约
        if not self.storage.acquire_lease(export_id, self.owner, self.config.lease_seconds):
            return
        frozen = self.storage.get_frozen(export_id)
        if frozen is None:
            return
        records_norm, rules_norm = frozen

        # 关键：始终按冻结快照渲染，随后改动当前规则不影响本标识
        content = models.render_artifact(export_id, records_norm, rules_norm)
        expected_sha = models.sha256_bytes(content)

        # 恢复：清理残缺件 / 收敛到摘要匹配件 / 否则重写
        temp_path, notes = self._reconcile(export_id, content, expected_sha)
        if temp_path is None:
            # 最终工件已完整（发布前崩溃于 rename 之后），直接补登记
            disk_sha = self._digest(self._final_path(export_id))
            if disk_sha != expected_sha:
                self.storage.mark_failed(export_id, self.owner, "final digest mismatch on recovery")
                return
        else:
            disk_sha = self._digest(temp_path)
            if disk_sha != expected_sha:
                self.storage.mark_failed(export_id, self.owner, "temp digest mismatch after write")
                return

        stage_row = self.storage.get(export_id)
        cur_stage = stage_row["stage"] if stage_row else "ACCEPTED"

        if cur_stage in ("ACCEPTED", "LEASED"):
            self.storage.advance(
                export_id, self.owner, "TEMP_WRITTEN",
                "; ".join(notes), content_sha256=expected_sha,
            )

        # 临时工件写入后模拟进程退出（测试用）
        self._maybe_crash(export_id)

        # 核验：磁盘摘要 == 冻结快照重算摘要 == 登记摘要
        row = self.storage.get(export_id)
        if row is None:
            return
        if disk_sha != expected_sha or (row["content_sha256"] and row["content_sha256"] != expected_sha):
            self.storage.mark_failed(export_id, self.owner, "digest verification failed")
            return

        if self.storage._STAGE_RANK[row["stage"]] < self.storage._STAGE_RANK["VERIFIED"]:
            ok = self.storage.advance(
                export_id, self.owner, "VERIFIED",
                f"sha256 verified {expected_sha[:16]}", content_sha256=expected_sha,
            )
            if not ok:
                return  # 租约丢失等，交由持有者继续；下载仍未开放
        else:
            # 恢复路径：内容核验通过但阶段已在 VERIFIED，幂等补写摘要
            self.storage.advance(
                export_id, self.owner, "VERIFIED",
                f"re-verified {expected_sha[:16]}", content_sha256=expected_sha,
            )

        # 原子发布：rename 落盘后再以事务登记 PUBLISHED（唯一一次）
        final = self._final_path(export_id)
        if not final.exists():
            assert temp_path is not None
            os.replace(temp_path, final)
            self._fsync_dir(self.pub_dir)

        result = self.storage.commit_publish(export_id, self.owner, final.name, expected_sha)
        if result == "PUBLISHED":
            for t in self._temps_for(export_id):
                t.unlink(missing_ok=True)
        elif result == "DIGEST_MISMATCH":
            # 已有不同摘要工件被发布：保留原有证据（确定性渲染下不应发生）
            self.storage.mark_failed(export_id, self.owner, "conflicting published digest")
        elif result.startswith("LEASE_LOST") or result.startswith("BAD_STAGE"):
            # 并发下败者：若已发布且摘要相同则收敛清理，否则等租约过期再恢复
            row = self.storage.get(export_id)
            if row and row["stage"] == "PUBLISHED" and row["content_sha256"] == expected_sha:
                for t in self._temps_for(export_id):
                    t.unlink(missing_ok=True)

    # ---- 主循环 -------------------------------------------------------

    def run_forever(self) -> None:
        self._bind()
        while not self._stop.is_set():
            export_id = self.storage.claim_next_pending(
                self.owner, self.config.lease_seconds
            )
            if export_id is None:
                self._stop.wait(self.config.poll_seconds)
                continue
            try:
                self.process_once(export_id)
            except Exception as exc:  # 单条失败不拖垮进程
                try:
                    self.storage.mark_failed(export_id, self.owner, f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass
            finally:
                self.storage.release_lease(export_id, self.owner)

    def stop(self) -> None:
        self._stop.set()
