"""Spaced repetition service (simplified SM-2 style)."""

from datetime import datetime, timedelta, timezone


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


def update_srs_item(conn, user_id, question_key, question_text, topic, difficulty, is_correct):
    """Upsert spaced repetition schedule for a question."""
    row = conn.execute(
        """
        SELECT id, repetition_count, ease_factor
        FROM spaced_repetition
        WHERE user_id=? AND question_key=?
        """,
        (user_id, question_key),
    ).fetchone()

    now = _utc_now()
    if row is None:
        repetition_count = 0
        ease_factor = 2.5
    else:
        repetition_count = int(row["repetition_count"] or 0)
        ease_factor = float(row["ease_factor"] or 2.5)

    if not is_correct:
        repetition_count = 0
        interval_days = 1
        last_result = "wrong"
    else:
        repetition_count += 1
        if repetition_count == 1:
            interval_days = 1
        elif repetition_count == 2:
            interval_days = 3
        else:
            interval_days = 7
        last_result = "correct"

    next_review_at = now + timedelta(days=interval_days)

    if row is None:
        conn.execute(
            """
            INSERT INTO spaced_repetition (
                user_id, question_key, question_text, topic, difficulty,
                next_review_at, repetition_count, ease_factor, last_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                question_key,
                question_text,
                topic,
                difficulty,
                _to_iso(next_review_at),
                repetition_count,
                ease_factor,
                last_result,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE spaced_repetition
            SET question_text=?, topic=?, difficulty=?, next_review_at=?,
                repetition_count=?, ease_factor=?, last_result=?, updated_at=CURRENT_TIMESTAMP
            WHERE user_id=? AND question_key=?
            """,
            (
                question_text,
                topic,
                difficulty,
                _to_iso(next_review_at),
                repetition_count,
                ease_factor,
                last_result,
                user_id,
                question_key,
            ),
        )


def get_due_review_items(conn, user_id, limit=50):
    """Return due SRS items (next_review_at <= now)."""
    now = _utc_now()
    rows = conn.execute(
        """
        SELECT question_key, question_text, topic, difficulty, next_review_at,
               repetition_count, ease_factor, last_result
        FROM spaced_repetition
        WHERE user_id=?
        ORDER BY next_review_at ASC
        """,
        (user_id,),
    ).fetchall()

    due = []
    for row in rows:
        due_at = _parse_dt(row["next_review_at"])
        if due_at is None or due_at <= now:
            due.append(
                {
                    "question_key": row["question_key"],
                    "question_text": row["question_text"],
                    "topic": row["topic"],
                    "difficulty": row["difficulty"],
                    "next_review_at": row["next_review_at"],
                    "repetition_count": int(row["repetition_count"] or 0),
                    "ease_factor": float(row["ease_factor"] or 2.5),
                    "last_result": row["last_result"] or "",
                }
            )
        if len(due) >= limit:
            break
    return due
