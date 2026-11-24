from fractions import Fraction


def _piece_intervals(route):
    return [(Fraction(piece['start']), Fraction(piece['end']), piece['status'])
            for piece in route['pieces']]


def _status_at(intervals, when):
    for start, end, status in intervals:
        if start <= when < end:
            return status
    if intervals and when == intervals[-1][1]:
        return intervals[-1][2]
    return None


def merged_periods(route, status):
    periods = []
    for piece in route['pieces']:
        if piece['status'] != status:
            continue
        start, end = Fraction(piece['start']), Fraction(piece['end'])
        if periods and Fraction(periods[-1]['end']) == start:
            periods[-1]['end'] = str(end)
            periods[-1]['seconds'] = str(Fraction(periods[-1]['seconds']) + end - start)
        else:
            periods.append(dict(start=str(start), end=str(end), seconds=str(end - start)))
    return periods


def diff_route(before_route, after_route):
    before_intervals = _piece_intervals(before_route)
    after_intervals = _piece_intervals(after_route)
    left = Fraction(before_route['order']['start'])
    right = Fraction(before_route['order']['end'])
    cuts = {left, right}
    cuts.update(start for start, _, _ in before_intervals)
    cuts.update(end for _, end, _ in before_intervals)
    cuts.update(start for start, _, _ in after_intervals)
    cuts.update(end for _, end, _ in after_intervals)
    cuts = sorted(cut for cut in cuts if left <= cut <= right)

    changes = []
    for start, end in zip(cuts, cuts[1:]):
        if end <= start:
            continue
        middle = (start + end) / 2
        before_status = _status_at(before_intervals, middle)
        after_status = _status_at(after_intervals, middle)
        if before_status == after_status:
            continue
        item = dict(start=str(start), end=str(end), seconds=str(end - start),
                    before=before_status, after=after_status,
                    kind=f'{before_status}_to_{after_status}')
        changes.append(item)

    return {
        'changes': changes,
        'open_periods': merged_periods(after_route, 'open'),
        'unknown_periods': merged_periods(after_route, 'unknown'),
        'blocked_periods': merged_periods(after_route, 'blocked'),
        'open_changes': [item for item in changes if item['after'] == 'open'],
        'unknown_changes': [item for item in changes if item['after'] == 'unknown'],
        'lost_open': [item for item in changes if item['before'] == 'open' and item['after'] != 'open'],
    }


def diff_fleet(before_coverage, after_coverage, before_windows, after_windows):
    ships = {}
    for vessel, after_ship in after_coverage['ships'].items():
        before_ship = before_coverage['ships'][vessel]
        route_change = diff_route(before_windows[vessel], after_windows[vessel])
        before_open = Fraction(before_ship['seconds']['open'])
        after_open = Fraction(after_ship['seconds']['open'])
        before_unknown = Fraction(before_ship['seconds']['unknown'])
        after_unknown = Fraction(after_ship['seconds']['unknown'])
        ships[vessel] = {
            'window_s': after_ship['window_s'],
            'before_seconds': dict(before_ship['seconds']),
            'after_seconds': dict(after_ship['seconds']),
            'delta_open_s': str(after_open - before_open),
            'delta_unknown_s': str(after_unknown - before_unknown),
            **route_change,
            # Explicit domain aliases used by the review API/page.
            'feasible_periods': route_change['open_periods'],
            'feasible_windows': route_change['open_periods'],
            'open_windows': route_change['open_periods'],
            'missing_evidence_periods': route_change['unknown_periods'],
            'missing_windows': route_change['unknown_periods'],
            'unknown_windows': route_change['unknown_periods'],
            'evidence_gap_periods': route_change['unknown_periods'],
            'changed_feasible': route_change['open_changes'],
            'changed_unknown': route_change['unknown_changes'],
            'periods': {
                'open': route_change['open_periods'],
                'feasible': route_change['open_periods'],
                'unknown': route_change['unknown_periods'],
                'missing_evidence': route_change['unknown_periods'],
                'blocked': route_change['blocked_periods'],
            },
        }
    blocked_windows = []
    for vessel, ship in ships.items():
        for interval in ship['lost_open']:
            blocked_windows.append(dict(vessel=vessel, **interval))
    return {'ships': ships, 'vessels': ships, 'schedules': ships,
            'changes_by_vessel': {name: ship['changes'] for name, ship in ships.items()},
            'periods_by_vessel': {name: ship['periods'] for name, ship in ships.items()},
            'blocked_windows': blocked_windows,
            'issuance_blocks': blocked_windows,
            'has_changes': any(ship['changes'] for ship in ships.values())}
