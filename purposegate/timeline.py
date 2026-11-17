"""Temporal replay for direct grants and delegated authorization chains.

Direct grants and delegation evidence are deliberately separate: a direct
grant replay never walks delegate events, and a delegated request never falls
back to a direct grant.  That keeps the original direct-authorization results
stable while every delegated result is backed by an explicit, step-by-step
chain.
"""
import hashlib
import json
from datetime import date, datetime, time, timezone

DELEGATION_KINDS = {'delegate_source', 'delegate', 'delegate_revoke'}
CONTRACTS = {'immediate', 'fixed_term'}

# Lower rank runs first at the same instant; revoke-like events win ties.
EVENT_RANK = {
    'delegate_source': 0, 'grant': 0,
    'delegate': 1, 'narrow': 1,
    'revoke': 2, 'delegate_revoke': 2,
}

AMBIGUOUS_CODES = {
    'delegation_source_conflict', 'delegation_source_missing',
    'delegation_source_mismatch', 'delegation_issuer_mismatch',
    'delegation_contract_missing', 'delegation_incomplete',
    'delegation_flag_missing',
    'delegation_purpose_cycle', 'delegation_invalid_interval',
    'delegation_parent_missing', 'delegation_chain_conflict',
    'delegation_cycle', 'delegation_depth_limit',
    'delegation_contract_switch',
}
REJECT_CODES = {
    'delegation_overbroad', 'delegation_purpose_widened',
    'delegation_not_allowed', 'delegation_new_signing_after_revocation',
    'delegation_revoked', 'delegation_expired', 'delegation_scope_denied',
}


def instant(value):
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, timezone.utc)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def event_sha(value):
    """Canonical content hash of an event body (the raw line hash is in RawEvent)."""
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def active_grant(events, request_time):
    """Replay one person's history; revoke wins ties at the same instant."""
    past = [e for e in events if e['kind'] in ('grant', 'narrow', 'revoke') and instant(e['at']) <= instant(request_time)]
    past.sort(key=lambda e: (instant(e['at']), EVENT_RANK[e['kind']], e['event_key']))
    grant = None
    for event in past:
        if event['kind'] == 'revoke':
            grant = None
        elif event['kind'] == 'grant':
            grant = event
        elif grant is not None:
            grant = {**grant, 'fields': sorted(set(grant['fields']) & set(event['fields']))}
    return grant


def allowed_purpose(requested, granted, tree):
    seen = set(); current = requested
    while current and current not in seen:
        if current == granted:
            return True
        seen.add(current); current = tree.get(current)
    if current in seen:
        raise ValueError('purpose_cycle')
    return False


def field_ancestors(field, lineage):
    seen = set(); visiting = set()

    def visit(item):
        if item in visiting:
            raise ValueError('lineage_cycle')
        if item in seen:
            return
        visiting.add(item)
        for parent in lineage.get(item, []):
            visit(parent)
        visiting.remove(item); seen.add(item)
    visit(field)
    return seen


# ---------------------------------------------------------------------------
# Delegation evidence


def delivered(events, request_time):
    """Events known at the request instant, in deterministic replay order."""
    limit = instant(request_time)
    rows = [e for e in events if instant(e['at']) <= limit]
    rows.sort(key=lambda e: (instant(e['at']), EVENT_RANK.get(e['kind'], 1), e['event_key']))
    return rows


SOURCE_FIELDS = ('event_key', 'kind', 'delegator', 'delegatee', 'purpose', 'fields',
                 'from', 'until', 'at', 'revocation_effect', 'delegate_allowed',
                 'irrevocable_until', 'irrevocable')


def _source_body(event):
    """The signed authorization fields of an event body, normalized.

    Bookkeeping fields such as ``subject``/``person`` are not part of the
    authorization and are ignored; everything that grants scope is compared.
    """
    body = {name: event.get(name) for name in SOURCE_FIELDS if name in event}
    if isinstance(body.get('fields'), list):
        body['fields'] = sorted(body['fields'])
    return body


def _full_body(candidate):
    return (isinstance(candidate, dict) and candidate.get('event_key')
            and candidate.get('kind') in ('delegate_source', 'delegate') and 'fields' in candidate)


def delegation_conflicts(events):
    """Find one event key carrying two different bodies.

    A source snapshot may appear as a top-level event or embedded in a child
    delegator's ``source``.  A bare pointer without fields is not a body and is
    not compared; two different bodies under one key are never reconciled.
    """
    bodies = {}
    conflicts = set()
    for event in events:
        candidates = [event]
        embedded = event.get('source')
        if isinstance(embedded, dict):
            candidates.append(embedded)
        for candidate in candidates:
            if not _full_body(candidate):
                continue
            key = candidate['event_key']
            body = canonical(_source_body(candidate))
            old = bodies.get(key)
            if old is not None and old != body:
                conflicts.add(key)
            else:
                bodies.setdefault(key, body)
    return conflicts, bodies


def _delegation_edges(events):
    edges = {}
    for event in events:
        if event.get('kind') not in ('delegate_source', 'delegate'):
            continue
        # A conflicting event still produces an edge so the chain resolver can
        # report the divergence as evidence; it can never become viable.
        delegator = event.get('delegator')
        if not delegator and event.get('kind') == 'delegate_source':
            delegator = event.get('person')
        delegatee = event.get('delegatee')
        if delegator and delegatee:
            edges.setdefault(delegator, []).append(event)
    for rows in edges.values():
        rows.sort(key=lambda e: (instant(e['at']), e['event_key']))
    return edges


def _walk_paths(edges, root, final_user, limit=64, depth=12):
    paths = []
    truncated = [False]

    def walk(node, path, visited):
        if len(paths) >= limit:
            truncated[0] = True
            return
        if node == final_user and path:
            paths.append(list(path)); return
        if len(path) >= depth:
            truncated[0] = True
            return
        for event in edges.get(node, []):
            nxt = event.get('delegatee')
            if nxt in visited:
                continue
            walk(nxt, path + [event], visited | {nxt})
    walk(root, [], {root})
    return paths, truncated[0]


def _reachable_cycles(edges, root, depth=12):
    """Every directed cycle reachable from ``root`` (event edges, not nodes)."""
    cycles = []; seen_paths = set()

    def walk(node, path, visited):
        if len(path) >= depth:
            return
        for event in edges.get(node, []):
            nxt = event.get('delegatee')
            if nxt in visited:
                candidate = path + [event]
                key = '|'.join(str(e.get('event_key')) for e in candidate)
                if key not in seen_paths:
                    seen_paths.add(key); cycles.append(candidate)
                continue
            walk(nxt, path + [event], visited | {nxt})
    walk(root, [], {root})
    return cycles


def _revoke_times(rows, delegator, delegatee):
    if not delegator or not delegatee:
        return []
    result = []
    for event in rows:
        if (event.get('kind') == 'delegate_revoke'
                and event.get('delegator') == delegator
                and event.get('delegatee') == delegatee):
            result.append((instant(event['at']), event))
    result.sort(key=lambda item: (item[0], item[1]['event_key']))
    return result


def _as_time(value):
    if value is None:
        return None
    try:
        return instant(value)
    except (TypeError, ValueError):
        return None


def _link_record(event, level, source_sha=None):
    kind = event.get('kind')
    return {
        'level': level,
        'event_key': event.get('event_key'),
        'kind': kind,
        'delegator': event.get('delegator') or (event.get('person') if kind == 'delegate_source' else None),
        'delegatee': event.get('delegatee'),
        'purpose': event.get('purpose'),
        'fields': sorted(set(event.get('fields') or [])) if isinstance(event.get('fields'), list) else [],
        'from_at': event.get('from'),
        'until_at': event.get('until'),
        'signed_at': event.get('at'),
        'revocation_effect': event.get('revocation_effect') if kind == 'delegate_source' else None,
        'declared_revocation_effect': event.get('revocation_effect'),
        'delegate_allowed': event.get('delegate_allowed'),
        'irrevocable_until': (event.get('irrevocable_until')
                              if event.get('irrevocable', True) is not False else None),
        'source_sha': source_sha or event_sha(event),
        'status': 'active',
        'effective_until': None,
        'own_revoke_at': None,
        'detail': '',
    }


def _required(event):
    kind = event.get('kind')
    if kind == 'delegate_source':
        return ('delegatee', 'purpose', 'fields', 'from', 'until', 'at')
    return ('delegator', 'delegatee', 'purpose', 'fields', 'from', 'until', 'at')


def _chain_result(links, status, allowed, issues, contract, contract_source):
    revokes = [link['own_revoke_at'] for link in links if link.get('own_revoke_at')]
    return {
        'chain_key': '|'.join(str(link.get('event_key')) for link in links),
        'status': status,
        'links': links,
        'allowed_fields': sorted(allowed),
        'field_intersection': links[-1].get('field_intersection') if links else [],
        'issues': sorted(set(issues)),
        'contract': contract if contract in CONTRACTS else 'unknown',
        'revocation_effect': contract if contract in CONTRACTS else 'unknown',
        'contract_source': contract_source,
        'effective_until': links[-1].get('effective_until') if links else None,
        'revoked_at': min(revokes) if revokes else None,
    }


def _finish(links, issues, contract, contract_source, ambiguous, rejected,
            allowed=(), request_time=None):
    if ambiguous:
        status = 'uncertain'
    elif rejected:
        status = 'rejected'
    else:
        status = 'active'
    for link in links:
        link.pop('_raw', None)
        if link['status'] == 'active':
            link['status'] = status
    # The contract travels with the evidence so the report shows the effect
    # that was actually applied, not just the one declared at the source.
    effect = contract if contract in CONTRACTS else 'unknown'
    if any(link.get('status') == 'protected' for link in links):
        effect = 'fixed_term'
        for link in links:
            if link.get('status') == 'protected' and link.get('irrevocable_until'):
                effect = f'fixed_term:protected_until:{link["irrevocable_until"]}'
    result = _chain_result(links, status, allowed, issues, contract, contract_source)
    result['revocation_effect'] = effect
    if status == 'uncertain':
        result['revocation_effect'] = 'uncertain'
    return result


def _evaluate_path(path, rows, conflicts, request_time, purpose,
                   requested_fields, purpose_tree, lineage, valid_fields):
    """Validate every step of one root-to-final-user chain."""
    req = instant(request_time)
    issues = []; links = []
    ambiguous = False; rejected = False
    raw_by_key = {event.get('event_key'): event for event in rows}

    def fail(code):
        nonlocal ambiguous, rejected
        issues.append(code)
        if code in AMBIGUOUS_CODES:
            ambiguous = True
        else:
            rejected = True

    def stop():
        for link in links:
            if link['status'] == 'active':
                link['status'] = 'uncertain'
        return _finish(links, issues, contract, contract_source, True, rejected)

    root = path[0]
    contract = root.get('revocation_effect')
    contract_source = {'event_key': root.get('event_key'),
                       'source_sha': event_sha(root),
                       'signed_at': root.get('at')}
    if root.get('kind') != 'delegate_source':
        fail('delegation_source_mismatch')
    if contract not in CONTRACTS:
        fail('delegation_contract_missing')

    previous = None; running = None
    for level, event in enumerate(path):
        link = _link_record(event, level)
        link['_raw'] = raw_by_key.get(event.get('event_key'), event)
        links.append(link)
        key = event.get('event_key')
        if key in conflicts:
            fail('delegation_source_conflict')
        if any(event.get(name) in (None, '') for name in _required(event)):
            fail('delegation_incomplete')
            return stop()
        if level == 0 and event.get('kind') != 'delegate_source':
            fail('delegation_source_mismatch')
        if level > 0 and event.get('kind') != 'delegate':
            fail('delegation_source_mismatch')
        start = _as_time(link['from_at']); end = _as_time(link['until_at'])
        signed = _as_time(link['signed_at'])
        if start is None or end is None or signed is None or start >= end:
            fail('delegation_invalid_interval')
            return stop()
        link['from_at'] = start.isoformat(); link['until_at'] = end.isoformat()
        link['signed_at'] = signed.isoformat()

        fields = set(link['fields'])
        if level == 0:
            running = set(fields)
        else:
            embedded = event.get('source') if isinstance(event.get('source'), dict) else None
            source_key = event.get('source_event_key')
            parent_key = previous['event_key']
            # Provenance must name the immediate parent authorization.  A full
            # embedded body must equal that parent byte-for-byte; a pointer is
            # acceptable but never silently substitutes for the parent.
            if embedded is not None:
                if embedded.get('event_key') != parent_key:
                    fail('delegation_source_mismatch')
                elif _full_body(embedded) and canonical(_source_body(embedded)) != canonical(
                        _source_body(previous['_raw'])):
                    fail('delegation_source_conflict')
            elif source_key is not None:
                if source_key != parent_key:
                    fail('delegation_source_mismatch')
            else:
                fail('delegation_source_missing')

            if event.get('delegator') != previous.get('delegatee'):
                fail('delegation_issuer_mismatch')
            # A child that declares a different revocation contract is trying
            # to switch it after the fact; the source snapshot is authoritative.
            if event.get('revocation_effect') not in (None, contract):
                fail('delegation_contract_switch')
            if not fields <= set(previous.get('fields') or []):
                fail('delegation_overbroad')
            try:
                if not allowed_purpose(link['purpose'], previous['purpose'], purpose_tree):
                    fail('delegation_purpose_widened')
            except ValueError:
                fail('delegation_purpose_cycle')
            parent_start = _as_time(previous['from_at']); parent_end = _as_time(previous['until_at'])
            parent_signed = _as_time(previous['signed_at'])
            if start < parent_start or end > parent_end:
                fail('delegation_overbroad')
            if parent_signed is None or signed < parent_signed or not parent_start <= signed < parent_end:
                fail('delegation_overbroad')
            if previous.get('delegate_allowed') is not True:
                fail('delegation_not_allowed' if previous.get('delegate_allowed') is False
                     else 'delegation_flag_missing')
            # No new signing at or after any ancestor's recall.  This applies
            # under both revocation contracts: a protected existing child may
            # continue, but may not create further delegates.
            for ancestor in links[:level]:
                if any(when <= signed for when, _ in _revoke_times(
                        rows, ancestor['delegator'], ancestor['delegatee'])):
                    fail('delegation_new_signing_after_revocation')
            clause = link['irrevocable_until']
            if clause is not None:
                clause_time = _as_time(clause)
                if clause_time is None or clause_time <= start or clause_time > end:
                    fail('delegation_overbroad')
            running = running & fields
        link['field_intersection'] = sorted(running)
        # Each step's evidence names the source-frozen revocation contract.
        link['revocation_effect'] = contract if contract in CONTRACTS else 'unknown'

        own = [when for when, _ in _revoke_times(rows, link['delegator'], link['delegatee']) if when <= req]
        link['own_revoke_at'] = min(own).isoformat() if own else None
        previous = link

    final = links[-1]
    final_start = _as_time(final['from_at']); final_end = _as_time(final['until_at'])
    if not (final_start <= req < final_end):
        fail('delegation_expired')
    if req < _as_time(final['signed_at']):
        fail('delegation_expired')

    try:
        if not allowed_purpose(purpose, final['purpose'], purpose_tree):
            fail('delegation_scope_denied')
    except ValueError:
        fail('delegation_purpose_cycle')

    # Effective fields come from this one chain only; parallel chains are
    # compared later and are never unioned.
    allowed = set()
    for field in requested_fields:
        try:
            ancestors = field_ancestors(field, lineage)
        except ValueError:
            fail('lineage_cycle'); continue
        if not ancestors <= set(valid_fields):
            fail('unknown_field'); continue
        if ancestors <= running:
            allowed.add(field)

    # Revocation effect is taken from the frozen source event only.  A child
    # cannot switch the contract by declaring a different one after the fact.
    recalls = []
    for link in links:
        for when, event in _revoke_times(rows, link['delegator'], link['delegatee']):
            if when <= req:
                recalls.append((when, link, event))
    recalls.sort(key=lambda item: (item[0], item[2]['event_key']))
    final['effective_until'] = final['until_at']
    if recalls:
        revoke_at = recalls[0][0]
        protected = False
        if contract == 'fixed_term':
            clause = final['irrevocable_until']
            clause_time = _as_time(clause) if clause else None
            if (clause_time is not None
                    and _as_time(final['signed_at']) < revoke_at
                    and req < clause_time):
                protected = True
                final['status'] = 'protected'
                final['revocation_effect'] = f'irrevocable_until:{clause_time.isoformat()}'
                final['effective_until'] = min(final_end, clause_time).isoformat()
        if not protected:
            fail('delegation_revoked')
            for link in links:
                link['status'] = 'revoked'
        else:
            # The final signed child survives inside its declared term even
            # though the recalled ancestor grants no longer authorize new use.
            for link in links[:-1]:
                if link.get('own_revoke_at') and instant(link['own_revoke_at']) <= req:
                    if link['status'] == 'active':
                        link['status'] = 'revoked'

    return _finish(links, issues, contract, contract_source, ambiguous, rejected, allowed)


def _orphan_chains(edges, final_user, used=()):
    chains = []
    for rows in edges.values():
        for event in rows:
            if event.get('delegatee') == final_user and event.get('event_key') not in used:
                link = _link_record(event, 0)
                link['status'] = 'orphan'
                chains.append({
                    'chain_key': str(event.get('event_key')), 'status': 'uncertain',
                    'links': [link], 'allowed_fields': [], 'field_intersection': [],
                    'issues': ['delegation_parent_missing'], 'contract': 'unknown',
                    'revocation_effect': 'unknown', 'contract_source': None,
                    'effective_until': None, 'revoked_at': None,
                })
    return chains


def resolve_delegation(events, request_time, root, final_user, purpose,
                       requested_fields, purpose_tree, lineage, valid_fields,
                       extra_conflicts=()):
    """Resolve every chain rooted at ``root`` that ends at ``final_user``.

    Reachability alone never authorizes: each step is checked for issuer,
    frozen source, purpose ancestry, field narrowing and half-open validity.
    """
    rows = delivered(events, request_time)
    conflicts, _bodies = delegation_conflicts(rows)
    conflicts = set(conflicts) | set(extra_conflicts)
    edges = _delegation_edges(rows)
    paths, truncated = _walk_paths(edges, root, final_user)
    cycles = _reachable_cycles(edges, root)
    chains = []
    for path in paths:
        chains.append(_evaluate_path(path, rows, conflicts, request_time, purpose,
                                     requested_fields, purpose_tree, lineage, valid_fields))
    if truncated:
        chains.append({
            'chain_key': f'truncated:{root}->{final_user}', 'status': 'uncertain',
            'links': [], 'allowed_fields': [], 'field_intersection': [],
            'issues': ['delegation_depth_limit'], 'contract': 'unknown',
            'revocation_effect': 'uncertain', 'contract_source': None,
            'effective_until': None, 'revoked_at': None,
        })
    for path in cycles:
        link_rows = [_link_record(event, level) for level, event in enumerate(path)]
        for link in link_rows:
            link['status'] = 'cycle'
        chains.append({
            'chain_key': '|'.join(str(event.get('event_key')) for event in path),
            'status': 'uncertain', 'links': link_rows, 'allowed_fields': [],
            'field_intersection': [], 'issues': ['delegation_cycle'],
            'contract': 'unknown', 'revocation_effect': 'uncertain',
            'contract_source': None, 'effective_until': None, 'revoked_at': None,
        })
    if not paths and not cycles:
        chains.extend(_orphan_chains(edges, final_user, {c['chain_key'] for c in chains}))
    # One signed event list can yield duplicate evidence rows only when the
    # same event was observed twice; keep one chain per distinct key sequence.
    unique = {}
    for chain in chains:
        unique.setdefault(chain['chain_key'], chain)
    chains = list(unique.values())
    # Conflict evidence must remain visible even when a conflicting event was
    # excluded from edge construction.
    for chain in chains:
        used = {link['event_key'] for link in chain['links']} & conflicts
        if used:
            chain['issues'] = sorted(set(chain['issues']) | {'delegation_source_conflict'})
            if chain['status'] == 'active':
                chain['status'] = 'uncertain'
                for link in chain['links']:
                    if link['status'] == 'active':
                        link['status'] = 'uncertain'
    # A cycle or an exploration cut-off is a defect in the authorization
    # graph rooted at this data subject: the resolver cannot prove that no
    # other reachable chain exists, so it must not authorize on the acyclic
    # portion alone.  Both force the uncertain outcome below.
    graph_uncertain = bool(cycles) or truncated

    viable = [c for c in chains if c['status'] == 'active']
    used_keys = set()
    for chain in chains:
        for link in chain['links']:
            if link.get('event_key'):
                used_keys.add(link['event_key'])
    # Recalls are part of the evidence chain as well.
    for chain in chains:
        for link in chain['links']:
            for _when, event in _revoke_times(delivered(events, request_time),
                                              link.get('delegator'), link.get('delegatee')):
                if instant(event['at']) <= instant(request_time):
                    used_keys.add(event['event_key'])
    if viable and not graph_uncertain:
        allowed_sets = {frozenset(c['allowed_fields']) for c in viable}
        if len(allowed_sets) > 1:
            return {'status': '无法判定', 'fields': set(),
                    'issues': ['delegation_chain_conflict'],
                    'chains': chains, 'used_keys': used_keys,
                    'reason': 'delegation_chain_conflict'}
        chosen = sorted(viable, key=lambda c: c['chain_key'])[0]
        return {'status': '允许', 'fields': set(chosen['allowed_fields']),
                'issues': [], 'chains': chains, 'used_keys': used_keys,
                'reason': 'delegation_active'}
    issues = set()
    for chain in chains:
        issues.update(chain['issues'])
    if graph_uncertain:
        issues.update({'delegation_cycle'} if cycles else {'delegation_depth_limit'})
    if not chains:
        issues.add('no_valid_delegation')
    if any(code in AMBIGUOUS_CODES for code in issues):
        status = '无法判定'
    else:
        status = '拒绝'
    return {'status': status, 'fields': set(), 'issues': sorted(issues),
            'chains': chains, 'used_keys': used_keys,
            'reason': sorted(issues)[0] if issues else 'no_valid_delegation'}
