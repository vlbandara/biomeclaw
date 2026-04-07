# Health Heartbeat

This workspace is health-enabled. Use the health check-in skill before composing outreach:

- Read `skills/health-checkin/SKILL.md`.
- Prefer low-pressure, non-diagnostic check-ins.
- If the user has described emergency symptoms recently, do not continue routine coaching.

## Active Tasks

{% if morning_check_in %}
- Morning check-in after {{ wake_time }} local time. Ask about mood, sleep quality, symptoms, and today's main health priority.
{% endif %}
{% for window in reminder_windows %}
- Medication reminder window at {{ window }} local time. Keep it brief and ask whether the dose was taken or skipped.
{% endfor %}
{% if weekly_summary %}
- Weekly summary once per week. Summarize symptom trends, adherence, sleep, stress, mood, and progress toward goals.
{% endif %}
- Keep goals in view: {% if goals %}{{ goals | join(", ") }}{% else %}No explicit goals recorded yet.{% endif %}
- Current concerns to watch: {{ concerns }}

## Completed

<!-- Heartbeat does not auto-move items; Dream maintains durable summaries instead. -->
