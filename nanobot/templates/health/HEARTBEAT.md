# Health Heartbeat

This workspace is health-enabled. Use the health check-in skill before composing outreach:

- Read `skills/health-checkin/SKILL.md`.
- Match the voice in `SOUL.md` (human, varied, never corporate).
- Prefer low-pressure, non-diagnostic check-ins.
- If the user has described emergency symptoms recently, do not continue routine coaching.

## Active Tasks

{% if morning_check_in %}
- Morning check-in after {{ wake_time }} local time.
  - Tone: crisp, upbeat, forward-looking.
  - Ask at most 3 questions.
  - Include one concrete next move for today (minimum effective dose if needed).
  - Add a micro-challenge only if it feels welcome (keep it tiny, 5–10 minutes).
{% endif %}
{% for window in reminder_windows %}
- Medication reminder window at {{ window }} local time. Keep it brief and ask whether the dose was taken or skipped.
{% endfor %}
{% if weekly_summary %}
- Weekly summary once per week. Summarize symptom trends, adherence, sleep, stress, mood, and progress toward goals.
{% endif %}
- Keep goals in view: {% if goals %}{{ goals | join(", ") }}{% else %}No explicit goals recorded yet.{% endif %}
- Current concerns to watch: {{ concerns }}

## Rhythm and variety rules

- Do not send the same style of check-in two times in a row. Rotate formats (binary, two-choice, tiny bet, soft late-night).
- Time-of-day awareness:
  - Near bedtime (close to {{ sleep_time }}): keep it calm and grounding. Do not hype. Focus on sleep protection and one small anchor.
  - Late-night replies: match energy, reduce pressure, and avoid big overhauls.
- Streak awareness:
  - If they’ve been consistent lately: celebrate briefly and raise the standard gently.
  - If they’ve been quiet or slipping: no guilt. Offer a tiny restart and ask what’s realistically possible today.

## Completed

<!-- Heartbeat does not auto-move items; Dream maintains durable summaries instead. -->
