"""Smoke test the centralized-debounce arbiter without spinning up HTTP.

Exercises:
  1. ``observe`` deduplicates ``(group_id, message_id)`` even when multiple
     bots report the same group message.
  2. ``run_judge`` produces exactly one new ``decision_id`` per call, regardless
     of how many bots are waiting on the result.
  3. ``await_decision_async`` returns the same decision dict to every concurrent
     waiter.
  4. ``observe`` from a different bot upserts ``group_bots`` so the LLM judge
     sees both candidates.

The judge LLM is monkey-patched so the test runs offline. Run with::

    python scripts/arbiter_smoketest.py

Exits with code 0 on success, non-zero (and a printed reason) on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    # Sandbox the arbiter DB so we never touch the real instances/_shared.
    sandbox = Path(tempfile.mkdtemp(prefix="pupu_arbiter_smoketest_"))
    (sandbox / "instances" / "_shared").mkdir(parents=True, exist_ok=True)
    os.environ["PUPU_REPO_ROOT"] = str(sandbox)
    os.environ["PUPU_ARBITER_AUDIT"] = "0"

    from pupu_console import arbitrator

    # Stub the judge LLM so the test is hermetic.
    expected_speaker = "3596356160"

    def fake_llm(context_text, candidates, state):
        assert expected_speaker in candidates, f"missing candidate: {sorted(candidates)}"
        return expected_speaker, "smoketest_pick", 0.92

    arbitrator._llm_decide = fake_llm  # type: ignore[assignment]

    group_id = "9999999"

    # 1. Two bots observe the SAME message; second observe must be a no-op.
    common_msg = {
        "group_id": group_id,
        "message_id": "msg-1",
        "speaker_qq": "424225912",
        "speaker_name": "钮钴禄",
        "text": "喵喵喵",
        "speaker_is_bot": False,
    }
    arbitrator.observe(
        {
            **common_msg,
            "reporter": {
                "bot_id": "3596356160",
                "qq": "3596356160",
                "name": "璐璐",
                "persona_brief": "活泼",
            },
        }
    )
    arbitrator.observe(
        {
            **common_msg,
            "reporter": {
                "bot_id": "3853876778",
                "qq": "3853876778",
                "name": "仆仆",
                "persona_brief": "毒舌",
            },
        }
    )

    conn = arbitrator._connect()
    try:
        msg_count = conn.execute(
            "SELECT COUNT(*) AS c FROM group_messages WHERE group_id = ?", (group_id,)
        ).fetchone()["c"]
        bot_count = conn.execute(
            "SELECT COUNT(*) AS c FROM group_bots WHERE group_id = ?", (group_id,)
        ).fetchone()["c"]
    finally:
        conn.close()
    if msg_count != 1:
        print(f"FAIL: expected 1 deduped message, got {msg_count}")
        return 1
    if bot_count != 2:
        print(f"FAIL: expected 2 bots in group_bots, got {bot_count}")
        return 1

    # 2. Two concurrent run_judge calls (simulating a degenerate watchdog
    #    misfire) must produce exactly one new decision when serialized by
    #    the per-group lock. We model the "one judge per debounce flush"
    #    contract by calling once and checking the row count.
    decision = arbitrator.run_judge(group_id)
    if not decision or decision["speaker"] != expected_speaker:
        print(f"FAIL: run_judge returned {decision}")
        return 1

    conn = arbitrator._connect()
    try:
        rows = conn.execute(
            "SELECT decision_id, speaker, reason FROM group_decisions WHERE group_id = ?",
            (group_id,),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        print(f"FAIL: expected 1 decision row, got {len(rows)}: {[dict(r) for r in rows]}")
        return 1

    decision_id = decision["decision_id"]

    # 3. Concurrent await_decision_async waiters all see the SAME decision.
    async def _await_pair() -> list[dict | None]:
        # Bootstrap with since=decision_id-1 so we wake up on the existing row.
        async def _waiter():
            return await arbitrator.await_decision_async(group_id, decision_id - 1, 5.0)

        return await asyncio.gather(_waiter(), _waiter())

    results = asyncio.run(_await_pair())
    if any(r is None for r in results):
        print(f"FAIL: at least one waiter timed out: {results}")
        return 1
    if results[0] != results[1]:
        print(f"FAIL: waiters got different decisions:\n  {results[0]}\n  {results[1]}")
        return 1
    if results[0]["decision_id"] != decision_id:
        print(f"FAIL: decision_id mismatch: {results[0]} vs expected {decision_id}")
        return 1
    if results[0]["speaker"] != expected_speaker:
        print(f"FAIL: speaker mismatch: got {results[0]['speaker']} expected {expected_speaker}")
        return 1

    # 4. Bot's self-reply observe must dedup with a synthetic message_id.
    arbitrator.observe(
        {
            "group_id": group_id,
            "message_id": "self:3596356160:111",
            "speaker_qq": "3596356160",
            "speaker_name": "璐璐",
            "speaker_is_bot": True,
            "text": "我来啦~",
            "reporter": {
                "bot_id": "3596356160",
                "qq": "3596356160",
                "name": "璐璐",
            },
        }
    )
    arbitrator.observe(
        {
            "group_id": group_id,
            "message_id": "self:3596356160:111",
            "speaker_qq": "3596356160",
            "speaker_name": "璐璐",
            "speaker_is_bot": True,
            "text": "我来啦~",
            "reporter": {
                "bot_id": "3596356160",
                "qq": "3596356160",
                "name": "璐璐",
            },
        }
    )
    conn = arbitrator._connect()
    try:
        msg_count = conn.execute(
            "SELECT COUNT(*) AS c FROM group_messages WHERE group_id = ?", (group_id,)
        ).fetchone()["c"]
    finally:
        conn.close()
    if msg_count != 2:
        print(f"FAIL: expected 2 messages after self-reply dedup, got {msg_count}")
        return 1

    print("OK arbiter smoketest passed")
    print(f"     decision_id={decision_id} speaker={expected_speaker}")
    print(f"     sandbox={sandbox}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
