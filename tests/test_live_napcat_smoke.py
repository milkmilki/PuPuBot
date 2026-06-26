r"""Optional real NapCat/QQ smoke test.

This module is intentionally skipped by default. Enable it only when the local
PuPu Console and the two NapCat accounts are already available, because it sends
a real QQ group message through the localhost smoke endpoint.

Example:
    $env:PUPU_RUN_LIVE_NAPCAT_SMOKE = "1"
    .\ForFun\Scripts\python.exe -m unittest tests.test_live_napcat_smoke
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pupu_console.arbitrator import (
    is_group_arbitration_silenced,
    set_group_arbitration_silence,
)


RUN_ENV = "PUPU_RUN_LIVE_NAPCAT_SMOKE"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or not str(value).strip() else str(value).strip()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@unittest.skipUnless(
    os.environ.get(RUN_ENV) == "1",
    f"set {RUN_ENV}=1 to run the real NapCat/QQ smoke test",
)
class LiveNapCatSmokeTests(unittest.TestCase):
    """Exercise one deterministic open-group arbitration round on real QQ."""

    maxDiff = None

    def setUp(self) -> None:
        self.console_url = _env("PUPU_LIVE_CONSOLE_URL", "http://127.0.0.1:8770").rstrip("/")
        self.group_id = _env("PUPU_LIVE_GROUP_ID", "1103489921")
        self.sender_instance = _env("PUPU_LIVE_SENDER_INSTANCE", "cc3120f8")
        self.target_instance = _env("PUPU_LIVE_TARGET_INSTANCE", "bd7dae8d")
        self.target_qq = _env("PUPU_LIVE_TARGET_QQ", "3596356160")
        self.expected_speaker = _env("PUPU_LIVE_EXPECTED_SPEAKER", self.target_qq)
        self.expected_reason = _env("PUPU_LIVE_EXPECTED_REASON", "explicit_at")
        self.timeout_seconds = float(_env("PUPU_LIVE_TIMEOUT_SECONDS", "120"))
        self.poll_interval = float(_env("PUPU_LIVE_POLL_INTERVAL_SECONDS", "1.5"))
        self.start_instances = _env_bool("PUPU_LIVE_START_INSTANCES", True)
        self.require_reply = _env_bool("PUPU_LIVE_REQUIRE_REPLY", True)
        self.restore_silence_mode = _env("PUPU_LIVE_RESTORE_SILENCE", "on").lower()
        self.db_path = Path(_env("PUPU_LIVE_ARBITER_DB", str(Path("instances") / "_shared" / "arbiter.db")))

    def test_group_explicit_at_round_trip(self) -> None:
        self._ensure_instance_running(self.sender_instance)
        if self.target_instance:
            self._ensure_instance_running(self.target_instance)

        if not self.target_qq.isdigit():
            self.fail(f"PUPU_LIVE_TARGET_QQ must be numeric, got {self.target_qq!r}")
        if not self.group_id.isdigit():
            self.fail(f"PUPU_LIVE_GROUP_ID must be numeric, got {self.group_id!r}")

        baseline_decision_id = self._max_decision_id()
        previous_silence = is_group_arbitration_silenced(self.group_id)
        marker = f"SMOKE-LIVE-{int(time.time() * 1000)}"
        text = (
            f"[CQ:at,qq={self.target_qq}] {marker} optional live NapCat smoke test, "
            "please reply only once."
        )

        try:
            set_group_arbitration_silence(self.group_id, False)
            send_result = self._request_json(
                "POST",
                "/api/debug/smoke/send_text",
                {
                    "instance_id": self.sender_instance,
                    "target": "group",
                    "group_id": self.group_id,
                    "text": text,
                },
                timeout=35,
            )
            self.assertTrue(send_result.get("ok"), send_result)

            evidence = self._wait_for_round_evidence(marker, baseline_decision_id)
        finally:
            self._restore_silence(previous_silence)

        self.assertEqual(evidence["decision"]["speaker"], self.expected_speaker, evidence)
        if self.expected_reason:
            self.assertEqual(evidence["decision"]["reason"], self.expected_reason, evidence)
        if self.require_reply:
            self.assertIsNotNone(evidence.get("reply"), evidence)

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 10,
    ) -> Any:
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.console_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            self.fail(f"{method} {path} failed with HTTP {e.code}: {detail}")
        except OSError as e:
            self.fail(
                f"{method} {path} failed: {e}. "
                f"Start PuPu Console first or set PUPU_LIVE_CONSOLE_URL. url={self.console_url}"
            )
        return json.loads(payload) if payload else None

    def _ensure_instance_running(self, instance_id: str) -> None:
        instances = self._instances_by_id()
        info = instances.get(instance_id)
        if info is None:
            self.fail(f"unknown instance {instance_id!r}; available={sorted(instances)}")
        if info.get("running"):
            return
        if not self.start_instances:
            self.fail(f"instance {instance_id!r} is not running")

        self._request_json(
            "POST",
            f"/api/instances/{instance_id}/start",
            {"qq_mode": "napcat"},
            timeout=45,
        )
        deadline = time.monotonic() + 45
        last_info = info
        while time.monotonic() < deadline:
            last_info = self._instances_by_id().get(instance_id, {})
            if last_info.get("running"):
                return
            time.sleep(self.poll_interval)
        self.fail(f"instance {instance_id!r} did not become running; last_info={last_info}")

    def _instances_by_id(self) -> dict[str, dict[str, Any]]:
        items = self._request_json("GET", "/api/instances", timeout=8)
        if not isinstance(items, list):
            self.fail(f"/api/instances returned unexpected payload: {items!r}")
        return {str(item.get("id")): item for item in items if isinstance(item, dict)}

    def _connect_db(self) -> sqlite3.Connection:
        if not self.db_path.is_absolute():
            self.db_path = Path(__file__).resolve().parents[1] / self.db_path
        if not self.db_path.is_file():
            self.fail(f"arbiter db not found: {self.db_path}")
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _max_decision_id(self) -> int:
        conn = self._connect_db()
        try:
            row = conn.execute(
                "SELECT MAX(decision_id) AS max_id FROM group_decisions WHERE group_id = ?",
                (self.group_id,),
            ).fetchone()
            return int(row["max_id"] or 0)
        finally:
            conn.close()

    def _wait_for_round_evidence(self, marker: str, baseline_decision_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        last_evidence: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_evidence = self._read_round_evidence(marker, baseline_decision_id)
            decision = last_evidence.get("decision")
            if decision:
                if decision.get("speaker") != self.expected_speaker:
                    self.fail(
                        "arbiter chose an unexpected speaker:\n"
                        + json.dumps(last_evidence, ensure_ascii=False, indent=2)
                    )
                if not self.require_reply or last_evidence.get("reply"):
                    return last_evidence
            time.sleep(self.poll_interval)

        self.fail(
            f"timed out waiting for live NapCat smoke evidence; marker={marker}, "
            f"baseline_decision_id={baseline_decision_id}\n"
            + json.dumps(last_evidence, ensure_ascii=False, indent=2)
        )

    def _read_round_evidence(self, marker: str, baseline_decision_id: int) -> dict[str, Any]:
        conn = self._connect_db()
        try:
            marker_row = conn.execute(
                """
                SELECT message_id, speaker_qq, speaker_name, text, observed_at
                FROM group_messages
                WHERE group_id = ? AND text LIKE ?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (self.group_id, f"%{marker}%"),
            ).fetchone()
            decisions = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT decision_id, speaker, reason, confidence, since_message_id, decided_at
                    FROM group_decisions
                    WHERE group_id = ? AND decision_id > ?
                    ORDER BY decision_id ASC
                    """,
                    (self.group_id, baseline_decision_id),
                ).fetchall()
            ]
            evidence: dict[str, Any] = {
                "marker": marker,
                "baseline_decision_id": baseline_decision_id,
                "marker_message": dict(marker_row) if marker_row else None,
                "decisions_after_baseline": decisions,
            }
            if not marker_row:
                return evidence

            marker_message_id = str(marker_row["message_id"])
            decision = next(
                (row for row in decisions if str(row.get("since_message_id")) == marker_message_id),
                None,
            )
            evidence["decision"] = decision
            if not decision or not self.require_reply:
                return evidence

            reply_row = conn.execute(
                """
                SELECT message_id, speaker_qq, speaker_name, text, observed_at
                FROM group_messages
                WHERE group_id = ?
                  AND speaker_qq = ?
                  AND message_id != ?
                  AND observed_at > ?
                ORDER BY observed_at ASC
                LIMIT 1
                """,
                (
                    self.group_id,
                    self.expected_speaker,
                    marker_message_id,
                    marker_row["observed_at"],
                ),
            ).fetchone()
            evidence["reply"] = dict(reply_row) if reply_row else None
            return evidence
        finally:
            conn.close()

    def _restore_silence(self, previous_silence: bool) -> None:
        if self.restore_silence_mode in {"previous", "restore"}:
            set_group_arbitration_silence(self.group_id, previous_silence)
            return
        if self.restore_silence_mode in {"0", "false", "off", "no"}:
            return
        set_group_arbitration_silence(self.group_id, True)


if __name__ == "__main__":
    unittest.main()
