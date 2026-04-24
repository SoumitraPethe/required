"""Quiz session and fallback selection service."""

from datetime import datetime, timedelta, timezone
import random


DIFFICULTY_ORDER = ["easy", "medium", "hard"]


def _utc_now():
    return datetime.now(timezone.utc)


def _to_iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _clamp_questions(n):
    return max(10, min(100, int(n or 10)))


def get_recent_accuracy(conn, user_id, days=7):
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) AS correct_count
        FROM question_attempts
        WHERE user_id=? AND answered_at >= datetime('now', ?)
        """,
        (user_id, f"-{int(days)} day"),
    ).fetchone()
    total = int(row["total"] or 0)
    if total == 0:
        return 0.0
    return (int(row["correct_count"] or 0) / total) * 100.0


def adaptive_difficulty(base_difficulty, accuracy):
    base = (base_difficulty or "medium").lower()
    if base not in DIFFICULTY_ORDER:
        base = "medium"
    idx = DIFFICULTY_ORDER.index(base)
    if accuracy > 75:
        idx = min(idx + 1, len(DIFFICULTY_ORDER) - 1)
    elif accuracy < 50:
        idx = max(idx - 1, 0)
    return DIFFICULTY_ORDER[idx]


def build_fallback_questions(conn, user_id, all_questions, difficulty="all", topic="all", limit=10, wrong_first=True):
    """Select fallback questions using adaptive difficulty + weakness weighting."""
    limit = max(1, int(limit or 10))
    accuracy = get_recent_accuracy(conn, user_id, days=7)
    effective_difficulty = difficulty
    if (difficulty or "all").lower() != "all":
        effective_difficulty = adaptive_difficulty(difficulty, accuracy)

    pool = list(all_questions)
    if (effective_difficulty or "all").lower() != "all":
        pool = [q for q in pool if q.get("difficulty", "").lower() == effective_difficulty.lower()]
    if (topic or "all").lower() != "all":
        pool = [q for q in pool if q.get("topic", "").lower() == topic.lower()]
    if not pool:
        pool = list(all_questions)

    rows = conn.execute(
        """
        SELECT question_key,
               SUM(CASE WHEN is_correct=0 THEN 1 ELSE 0 END) AS wrong_count,
               SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) AS correct_count
        FROM question_attempts
        WHERE user_id=?
        GROUP BY question_key
        """,
        (user_id,),
    ).fetchall()
    stats = {
        r["question_key"]: (
            int(r["wrong_count"] or 0),
            int(r["correct_count"] or 0),
        )
        for r in rows
    }

    weighted = []
    for q in pool:
        wrong_count, correct_count = stats.get(q["id"], (0, 0))
        extra = max(0, wrong_count - correct_count) if wrong_first else 0
        weight = 1 + min(extra, 5)
        weighted.extend([q] * weight)

    random.shuffle(weighted)
    chosen = []
    seen = set()
    for q in weighted:
        if q["id"] in seen:
            continue
        chosen.append(q)
        seen.add(q["id"])
        if len(chosen) >= limit:
            break

    if len(chosen) < limit:
        remaining = [q for q in pool if q["id"] not in seen]
        random.shuffle(remaining)
        chosen.extend(remaining[: max(0, limit - len(chosen))])

    return chosen, effective_difficulty


def create_session(conn, user_id, source, mode, difficulty, topic, number_of_questions):
    """Create quiz/exam session with global timer metadata."""
    number_of_questions = _clamp_questions(number_of_questions)
    total_duration_seconds = number_of_questions * 50
    now = _utc_now()
    cur = conn.execute(
        """
        INSERT INTO quiz_sessions (
            user_id, source, mode, difficulty, topic, total_questions,
            number_of_questions, exam_start_time, total_duration_seconds, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            source,
            mode,
            (difficulty or "all").lower(),
            topic or "all",
            number_of_questions,
            number_of_questions,
            _to_iso(now),
            total_duration_seconds,
            "active",
        ),
    )
    return {
        "session_id": cur.lastrowid,
        "number_of_questions": number_of_questions,
        "total_duration_seconds": total_duration_seconds,
        "remaining_seconds": total_duration_seconds,
        "exam_start_time": _to_iso(now),
    }


def session_remaining_seconds(session_row):
    start = _parse_dt(session_row["exam_start_time"])
    total = int(session_row["total_duration_seconds"] or 0)
    if not start or total <= 0:
        return total
    elapsed = int((_utc_now() - start).total_seconds())
    return max(0, total - elapsed)


def get_session_status(conn, user_id, session_id):
    row = conn.execute(
        """
        SELECT *
        FROM quiz_sessions
        WHERE id=? AND user_id=?
        """,
        (session_id, user_id),
    ).fetchone()
    if row is None:
        return None

    remaining = session_remaining_seconds(row)
    status = row["status"] or "active"
    if status == "active" and remaining <= 0:
        conn.execute(
            """
            UPDATE quiz_sessions
            SET status='auto_submitted', ended_at=CURRENT_TIMESTAMP
            WHERE id=? AND user_id=?
            """,
            (session_id, user_id),
        )
        status = "auto_submitted"

    return {
        "session_id": row["id"],
        "mode": row["mode"],
        "status": status,
        "number_of_questions": int(row["number_of_questions"] or row["total_questions"] or 10),
        "total_duration_seconds": int(row["total_duration_seconds"] or 0),
        "remaining_seconds": remaining,
        "exam_start_time": row["exam_start_time"],
        "correct_count": int(row["correct_count"] or 0),
        "wrong_count": int(row["wrong_count"] or 0),
    }
