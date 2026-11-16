from datetime import date, datetime, time, timezone


def instant(value):
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, timezone.utc)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed.astimezone(timezone.utc)


def active_grant(events, request_time):
    """Replay one person's history; revoke wins ties at the same instant."""
    past = [e for e in events if instant(e['at']) <= instant(request_time)]
    past.sort(key=lambda e: (instant(e['at']), {'grant': 0, 'narrow': 1, 'revoke': 2}[e['kind']], e['event_key']))
    grant = None
    for event in past:
        if event['kind'] == 'revoke':
            grant = None
        elif event['kind'] == 'grant':
            grant = event
        elif grant is not None:
            grant = {**grant, 'fields': sorted(set(grant['fields']) & set(event['fields']))}
    return grant
