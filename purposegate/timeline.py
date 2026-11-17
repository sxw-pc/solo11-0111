from datetime import date, datetime, time, timezone


def instant(value):
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, timezone.utc)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed.astimezone(timezone.utc)


def active_grant(events, request_time):
    """Replay one person's history; revoke wins ties at the same instant.

    A narrow event intersects the granted fields and, when it carries a
    ``purpose``, replaces the granted purpose with the narrower one.  Any
    ``budget`` declared by the underlying grant rides along untouched, so
    narrowing and revocation drive the budget visible to privacy releases.
    """
    past = [e for e in events if instant(e['at']) <= instant(request_time)]
    past.sort(key=lambda e: (instant(e['at']), {'grant': 0, 'narrow': 1, 'revoke': 2}[e['kind']], e['event_key']))
    grant = None
    for event in past:
        if event['kind'] == 'revoke':
            grant = None
        elif event['kind'] == 'grant':
            grant = event
        elif grant is not None:
            narrow_fields = event.get('fields')
            narrowed = {**grant, 'fields': sorted(set(grant['fields']) & set(narrow_fields)) if narrow_fields is not None else grant['fields']}
            if event.get('purpose') is not None:
                narrowed['purpose'] = event['purpose']
            grant = narrowed
    return grant
