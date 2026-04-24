"""Analytics and practice planning service."""

import random
from quiz_service import adaptive_difficulty, get_recent_accuracy
from srs_service import get_due_review_items


def upsert_word_stats(conn, user_id, word_key, word_text, is_correct):
    """Track frequency seen/wrong/correct for priority-words engine."""
    row = conn.execute(
        """
        SELECT id, frequency_seen, frequency_wrong, frequency_correct
        FROM word_stats
        WHERE user_id=? AND word_key=?
        """,
        (user_id, word_key),
    ).fetchone()
    seen = 1
    wrong = 0 if is_correct else 1
    correct = 1 if is_correct else 0
    if row is None:
        conn.execute(
            """
            INSERT INTO word_stats (user_id, word_key, word_text, frequency_seen, frequency_wrong, frequency_correct)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, word_key, word_text, seen, wrong, correct),
        )
    else:
        conn.execute(
            """
            UPDATE word_stats
            SET word_text=?, frequency_seen=?, frequency_wrong=?, frequency_correct=?, updated_at=CURRENT_TIMESTAMP
            WHERE user_id=? AND word_key=?
            """,
            (
                word_text,
                int(row["frequency_seen"] or 0) + seen,
                int(row["frequency_wrong"] or 0) + wrong,
                int(row["frequency_correct"] or 0) + correct,
                user_id,
                word_key,
            ),
        )


def log_performance(conn, user_id, session_id, correct_count, wrong_count, difficulty, topic):
    total = max(1, int(correct_count or 0) + int(wrong_count or 0))
    accuracy = (int(correct_count or 0) / total) * 100.0
    conn.execute(
        """
        INSERT INTO performance_logs (user_id, session_id, accuracy, correct_count, wrong_count, difficulty, topic)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, session_id, accuracy, correct_count, wrong_count, (difficulty or "all"), (topic or "all")),
    )


def get_performance_trends(conn, user_id):
    daily_rows = conn.execute(
        """
        SELECT date(answered_at) as day,
               COUNT(*) as total,
               SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) as correct_count
        FROM question_attempts
        WHERE user_id=? AND answered_at >= datetime('now', '-30 day')
        GROUP BY date(answered_at)
        ORDER BY day ASC
        """,
        (user_id,),
    ).fetchall()
    daily_accuracy = []
    for row in daily_rows:
        total = int(row["total"] or 0)
        acc = round((int(row["correct_count"] or 0) / total) * 100, 2) if total else 0.0
        daily_accuracy.append({"date": row["day"], "accuracy": acc, "total": total})

    topic_rows = conn.execute(
        """
        SELECT topic,
               SUM(CASE WHEN answered_at >= datetime('now', '-30 day') AND is_correct=1 THEN 1 ELSE 0 END) as correct_30,
               SUM(CASE WHEN answered_at >= datetime('now', '-30 day') THEN 1 ELSE 0 END) as total_30,
               SUM(CASE WHEN answered_at >= datetime('now', '-7 day') AND is_correct=1 THEN 1 ELSE 0 END) as correct_7,
               SUM(CASE WHEN answered_at >= datetime('now', '-7 day') THEN 1 ELSE 0 END) as total_7
        FROM question_attempts
        WHERE user_id=?
        GROUP BY topic
        ORDER BY topic ASC
        """,
        (user_id,),
    ).fetchall()
    topic_trends = []
    for row in topic_rows:
        acc_30 = (int(row["correct_30"] or 0) / int(row["total_30"] or 1)) * 100 if int(row["total_30"] or 0) else 0.0
        acc_7 = (int(row["correct_7"] or 0) / int(row["total_7"] or 1)) * 100 if int(row["total_7"] or 0) else 0.0
        topic_trends.append(
            {
                "topic": row["topic"] or "unknown",
                "accuracy_30_days": round(acc_30, 2),
                "accuracy_7_days": round(acc_7, 2),
                "improvement": round(acc_7 - acc_30, 2),
            }
        )
    return {"daily_accuracy": daily_accuracy, "topic_trends": topic_trends}


def get_priority_words(conn, user_id, limit=20):
    rows = conn.execute(
        """
        SELECT word_key, word_text, frequency_seen, frequency_wrong, frequency_correct
        FROM word_stats
        WHERE user_id=?
        ORDER BY frequency_wrong DESC, frequency_seen DESC
        LIMIT ?
        """,
        (user_id, int(limit)),
    ).fetchall()
    out = []
    for row in rows:
        seen = int(row["frequency_seen"] or 0)
        correct = int(row["frequency_correct"] or 0)
        accuracy = round((correct / seen) * 100, 2) if seen else 0.0
        out.append(
            {
                "word_key": row["word_key"],
                "word_text": row["word_text"],
                "frequency_seen": seen,
                "frequency_wrong": int(row["frequency_wrong"] or 0),
                "accuracy": accuracy,
            }
        )
    out.sort(key=lambda x: (x["accuracy"], -x["frequency_wrong"]))
    return out[: int(limit)]


def _weak_topics(conn, user_id, target_count=5):
    rows = conn.execute(
        """
        SELECT topic,
               COUNT(*) as total,
               SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) as correct_count
        FROM question_attempts
        WHERE user_id=?
        GROUP BY topic
        """,
        (user_id,),
    ).fetchall()
    scored = []
    for row in rows:
        total = int(row["total"] or 0)
        acc = (int(row["correct_count"] or 0) / total) * 100 if total else 0.0
        scored.append({"topic": row["topic"] or "unknown", "accuracy": acc})
    scored.sort(key=lambda x: x["accuracy"])
    return [x["topic"] for x in scored[:target_count]]


def build_daily_plan(conn, user_id, fallback_questions):
    """Return weak/revision/new buckets with adaptive difficulty."""
    acc = get_recent_accuracy(conn, user_id, days=7)
    base = "medium"
    effective_difficulty = adaptive_difficulty(base, acc)

    weak_topics = _weak_topics(conn, user_id, target_count=5)
    seen_rows = conn.execute(
        """
        SELECT DISTINCT question_key
        FROM question_attempts
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchall()
    seen_keys = {r["question_key"] for r in seen_rows}

    q_pool = [q for q in fallback_questions if q.get("difficulty", "").lower() == effective_difficulty]
    if not q_pool:
        q_pool = list(fallback_questions)

    weak = [q for q in q_pool if q.get("topic") in weak_topics][:5]
    if len(weak) < 5:
        remaining = [q for q in q_pool if q not in weak]
        random.shuffle(remaining)
        weak.extend(remaining[: 5 - len(weak)])

    revision_items = get_due_review_items(conn, user_id, limit=3)

    new_q = [q for q in q_pool if q.get("id") not in seen_keys]
    random.shuffle(new_q)
    new_q = new_q[:2]
    if len(new_q) < 2:
        fill = [q for q in q_pool if q not in new_q]
        random.shuffle(fill)
        new_q.extend(fill[: 2 - len(new_q)])

    return {
        "weak": weak[:5],
        "revision": revision_items[:3],
        "new": new_q[:2],
        "effective_difficulty": effective_difficulty,
    }
