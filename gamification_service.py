"""Gamification helpers: streaks, XP, levels, achievements, weekly goals."""

from datetime import datetime, timedelta, timezone


def _utc_today():
    return datetime.now(timezone.utc).date()


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _ensure_profile(conn, user_id):
    row = conn.execute(
        "SELECT user_id, xp FROM gamification_profile WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO gamification_profile (user_id, xp, level) VALUES (?, 0, 1)",
            (user_id,),
        )


def _ensure_weekly_goal(conn, user_id, week_start_iso):
    row = conn.execute(
        "SELECT id FROM weekly_goals WHERE user_id=? AND week_start=?",
        (user_id, week_start_iso),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO weekly_goals (user_id, week_start, target_questions, target_correct)
            VALUES (?, ?, 70, 50)
            """,
            (user_id, week_start_iso),
        )


def add_xp(conn, user_id, amount):
    _ensure_profile(conn, user_id)
    row = conn.execute(
        "SELECT xp FROM gamification_profile WHERE user_id=?",
        (user_id,),
    ).fetchone()
    xp = int(row["xp"] or 0) + int(amount)
    level = max(1, (xp // 200) + 1)
    conn.execute(
        "UPDATE gamification_profile SET xp=?, level=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
        (xp, level, user_id),
    )
    return xp, level


def _streak_from_dates(sorted_dates_desc):
    if not sorted_dates_desc:
        return 0
    today = _utc_today()
    idx = 0
    streak = 0
    current = today
    if sorted_dates_desc and sorted_dates_desc[0] == today - timedelta(days=1):
        current = today - timedelta(days=1)
    elif sorted_dates_desc and sorted_dates_desc[0] != today:
        return 0
    while idx < len(sorted_dates_desc):
        if sorted_dates_desc[idx] == current:
            streak += 1
            current = current - timedelta(days=1)
            idx += 1
        elif sorted_dates_desc[idx] > current:
            idx += 1
        else:
            break
    return streak


def current_streak(conn, user_id):
    rows = conn.execute(
        """
        SELECT DISTINCT date(answered_at) AS d
        FROM question_attempts
        WHERE user_id=?
        ORDER BY d DESC
        """,
        (user_id,),
    ).fetchall()
    dates = []
    for r in rows:
        try:
            y, m, d = map(int, (r["d"] or "").split("-"))
            dates.append(datetime(y, m, d, tzinfo=timezone.utc).date())
        except Exception:
            continue
    return _streak_from_dates(dates)


def update_weekly_progress(conn, user_id):
    today = _utc_today()
    ws = _week_start(today).isoformat()
    _ensure_weekly_goal(conn, user_id, ws)
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) AS correct_count
        FROM question_attempts
        WHERE user_id=? AND date(answered_at) >= ?
        """,
        (user_id, ws),
    ).fetchone()
    total = int(row["total"] or 0)
    correct = int(row["correct_count"] or 0)
    conn.execute(
        """
        UPDATE weekly_goals
        SET current_questions=?, current_correct=?, updated_at=CURRENT_TIMESTAMP
        WHERE user_id=? AND week_start=?
        """,
        (total, correct, user_id, ws),
    )


def _unlock(conn, user_id, code, title, description):
    exists = conn.execute(
        "SELECT id FROM user_achievements WHERE user_id=? AND code=?",
        (user_id, code),
    ).fetchone()
    if not exists:
        conn.execute(
            """
            INSERT INTO user_achievements (user_id, code, title, description)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, code, title, description),
        )


def evaluate_achievements(conn, user_id):
    stats = conn.execute(
        """
        SELECT
          COUNT(*) AS attempts,
          SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) AS correct_count
        FROM question_attempts
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()
    attempts = int(stats["attempts"] or 0)
    correct = int(stats["correct_count"] or 0)
    streak = current_streak(conn, user_id)

    syn = conn.execute(
        """
        SELECT SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) AS c
        FROM question_attempts
        WHERE user_id=? AND lower(topic) LIKE '%synonym%'
        """,
        (user_id,),
    ).fetchone()
    syn_correct = int(syn["c"] or 0)

    if attempts >= 1:
        _unlock(conn, user_id, "first_blood", "First Blood", "Completed your first question.")
    if correct >= 50:
        _unlock(conn, user_id, "accurate_50", "Sharp Shooter", "Answered 50 questions correctly.")
    if syn_correct >= 25:
        _unlock(conn, user_id, "syn_master", "Synonym Master", "Got 25 synonym questions correct.")
    if streak >= 7:
        _unlock(conn, user_id, "streak_7", "7-Day Flame", "Maintained a 7-day streak.")


def gamification_summary(conn, user_id):
    _ensure_profile(conn, user_id)
    today = _utc_today()
    ws = _week_start(today).isoformat()
    _ensure_weekly_goal(conn, user_id, ws)
    update_weekly_progress(conn, user_id)
    evaluate_achievements(conn, user_id)

    profile = conn.execute(
        "SELECT xp, level FROM gamification_profile WHERE user_id=?",
        (user_id,),
    ).fetchone()
    weekly = conn.execute(
        """
        SELECT target_questions, target_correct, current_questions, current_correct, week_start
        FROM weekly_goals
        WHERE user_id=? AND week_start=?
        """,
        (user_id, ws),
    ).fetchone()
    achievements = conn.execute(
        """
        SELECT code, title, description, unlocked_at
        FROM user_achievements
        WHERE user_id=?
        ORDER BY unlocked_at DESC
        """,
        (user_id,),
    ).fetchall()

    xp = int(profile["xp"] or 0)
    level = int(profile["level"] or 1)
    level_floor = (level - 1) * 200
    level_ceil = level * 200
    activity = conn.execute(
        """
        SELECT date(answered_at) AS d, COUNT(*) AS attempts
        FROM question_attempts
        WHERE user_id=? AND date(answered_at) >= date('now', '-119 day')
        GROUP BY date(answered_at)
        ORDER BY d ASC
        """,
        (user_id,),
    ).fetchall()
    activity_map = {str(r["d"]): int(r["attempts"] or 0) for r in activity}

    today = _utc_today()
    start_day = today - timedelta(days=119)
    activity_days = []
    for i in range(120):
        day = start_day + timedelta(days=i)
        iso = day.isoformat()
        attempts = activity_map.get(iso, 0)
        intensity = 0
        if attempts >= 8:
            intensity = 4
        elif attempts >= 5:
            intensity = 3
        elif attempts >= 2:
            intensity = 2
        elif attempts >= 1:
            intensity = 1
        activity_days.append(
            {"date": iso, "attempts": attempts, "intensity": intensity}
        )

    return {
        "streak_days": current_streak(conn, user_id),
        "xp": xp,
        "level": level,
        "level_progress": {
            "current": xp - level_floor,
            "required": level_ceil - level_floor,
        },
        "weekly_goal": {
            "week_start": weekly["week_start"],
            "target_questions": int(weekly["target_questions"] or 0),
            "target_correct": int(weekly["target_correct"] or 0),
            "current_questions": int(weekly["current_questions"] or 0),
            "current_correct": int(weekly["current_correct"] or 0),
        },
        "activity_days": activity_days,
        "achievements": [dict(a) for a in achievements],
    }
