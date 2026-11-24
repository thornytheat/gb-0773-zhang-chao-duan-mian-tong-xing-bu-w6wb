import csv
import io
from copy import deepcopy

from app.clock import moment
from app.errors import TideError
from app.flow import calculate
from app.modules.intake.contract import OBSERVATION
from app.modules.journal.manifest import digest
from app.modules.journal.service import record
from app.source import load_sources

from .impact import diff_fleet

ACTION_ALIASES = {
    'row_only': 'row_only',
    'row': 'row_only',
    'exclude': 'row_only',
    'exclude_row': 'row_only',
    'remove_row': 'row_only',
    'drop_row': 'row_only',
    'only_bad_row': 'row_only',
    'only_remove_bad_row': 'row_only',
    'remove_bad': 'row_only',
    'remove_bad_row': 'row_only',
    'bad_row_only': 'row_only',
    'exclude_bad': 'row_only',
    'exclude_bad_row': 'row_only',
    'only_exclude_bad_row': 'row_only',
    '只剔坏行': 'row_only',
    '仅剔坏行': 'row_only',
    '剔除坏行': 'row_only',
    'disable': 'disable_station',
    'station': 'disable_station',
    'whole_station': 'disable_station',
    'disable_station': 'disable_station',
    'stop_station': 'disable_station',
    'suspend_station': 'disable_station',
    'suspend': 'disable_station',
    'stop': 'disable_station',
    'station_wide': 'disable_station',
    'all_station': 'disable_station',
    'entire_station': 'disable_station',
    'shutdown_station': 'disable_station',
    '停用整站': 'disable_station',
    '整站停用': 'disable_station',
}

REVIEWABLE_CODES = {'SENSOR_BAD', 'BAD_READING', 'CONFLICT'}


def canonical_id(*parts):
    return 'AN' + digest(repr(parts))[:12].upper()


def action_name(value):
    if value is None:
        raise TideError('MISSING_DECISION', '请选择只剔坏行或停用整站', 400)
    key = str(value).strip()
    action = ACTION_ALIASES.get(key) or ACTION_ALIASES.get(key.lower())
    if action is not None:
        return action
    lowered = key.lower()
    lowered = key.lower()
    if any(token in lowered for token in ('station', 'disable', 'stop', 'suspend', 'shutdown')) or '整站' in key:
        return 'disable_station'
    if any(token in lowered for token in ('row', 'exclude', 'remove', 'drop', 'patch', 'bad',
                                          'isolate', 'quarantine', 'preserve', 'partial',
                                          'keep_window', 'window_only')) or '剔' in key:
        return 'row_only'
    raise TideError('BAD_DECISION', '处置只能是 row_only 或 disable_station', 400)


def _raw_value(row, field, index=None):
    raw = row.get('raw')
    if isinstance(raw, dict):
        return raw.get(field, '')
    if isinstance(raw, list) and index is not None and index < len(raw):
        return raw[index]
    return ''


def _row_time(row):
    text = _raw_value(row, 'observed_at', 1)
    try:
        return moment(text)
    except (ValueError, AttributeError, TypeError):
        return None


def anomaly_id(row):
    station = _raw_value(row, 'station', 0).strip()
    when = _row_time(row)
    time_key = str(when) if when is not None else _raw_value(row, 'observed_at', 1)
    return canonical_id('tide-anomaly-v2', row.get('code'), station, time_key)


def _point_bracket(points, when):
    if when is None:
        return None, None, []
    ordered = sorted(points, key=lambda point: point['time'])
    exact = [point for point in ordered if point['time'] == when]
    before_points = [point for point in ordered if point['time'] < when]
    after_points = [point for point in ordered if point['time'] > when]
    return (before_points[-1] if before_points else None,
            after_points[0] if after_points else None, exact)


def build_anomalies(reports):
    rejected_rows = list(reports['datum'].get('rejected', []))
    datum_lines = {(row.get('source'), row.get('line'), row.get('code')) for row in rejected_rows}
    for row in reports['readings'].get('rejected', []):
        if row.get('source') == 'observations' and (row.get('source'), row.get('line'), row.get('code')) not in datum_lines:
            rejected_rows.append(row)
    groups = {}
    for row in rejected_rows:
        if row.get('source') not in (None, 'observations'):
            continue
        aid = anomaly_id(row)
        station = _raw_value(row, 'station', 0).strip() or None
        group = groups.setdefault(aid, {
            'id': aid,
            'anomaly_id': aid,
            'code': row['code'],
            'codes': set(),
            'station': station,
            'observed_at': _row_time(row),
            'observed_text': _raw_value(row, 'observed_at', 1),
            'lines': [],
            'rows': [],
        })
        group['codes'].add(row['code'])
        group['lines'].append(row['line'])
        group['rows'].append(row)
        if group['code'] != row['code'] and row['code'] == 'SENSOR_BAD':
            group['code'] = row['code']

    result = []
    stations = reports['datum'].get('stations', {})
    sections = reports['datum'].get('sections', {})
    for aid, group in groups.items():
        group['lines'] = sorted(set(group['lines']))
        group['codes'] = sorted(group['codes'])
        station_id = group['station']
        spec = stations.get(station_id) if station_id else None
        section_name = spec['section'] if spec else None
        section = sections.get(section_name, {}) if section_name else {}
        offset = int(section.get('offset_s', spec.get('offset_s', 0) if spec else 0))
        gap = int(section.get('gap_s', spec.get('gap_s', 0) if spec else 0))
        points = section.get('points', [])
        when = group['observed_at']
        before, after, exact = _point_bracket(points, when)
        start = before['time'] if before else (when if exact else None)
        end = after['time'] if after else (when if exact else None)
        interval = end - start if start is not None and end is not None else None
        scope_core = dict(section=section_name, station=station_id, observed_at=when,
                          start=start, end=end, offset_s=offset, gap_s=gap,
                          exact_lines=sorted(point['line'] for point in exact),
                          before_line=before['line'] if before else None,
                          after_line=after['line'] if after else None)
        scope_hash = digest(repr(sorted(scope_core.items())))[:16]
        scope = {
            **scope_core,
            'scope_hash': scope_hash,
            'start': str(start) if start is not None else None,
            'end': str(end) if end is not None else None,
            'evidence_start': str(start) if start is not None else None,
            'evidence_end': str(end) if end is not None else None,
            'departure_start': str(start - offset) if start is not None else None,
            'departure_end': str(end - offset) if end is not None else None,
            'scope_start': str(start - offset) if start is not None else None,
            'scope_end': str(end - offset) if end is not None else None,
            'interval_s': str(interval) if interval is not None else None,
            'over_gap': bool(interval is not None and gap and interval > gap),
            'no_bracket': (before is None or after is None) and not exact,
            'exact_points': [_point_brief(point) for point in exact],
            'before': _point_brief(before),
            'after': _point_brief(after),
        }
        item = {
            **group,
            'key': aid,
            'line': group['lines'][0],
            'source_line': group['lines'][0],
            'scope': scope,
            'range': scope,
            'affected_range': scope,
            'scope_hash': scope_hash,
            'section': section_name,
            'offset_s': offset,
            'gap_s': gap,
            'adjudicable': group['code'] in REVIEWABLE_CODES or bool(station_id and spec),
            'can_disable_station': bool(spec),
            'label': f"第 {','.join(map(str, group['lines']))} 行 · {station_id or '未登记站'} · {group['code']}",
        }
        result.append(item)
    return sorted(result, key=lambda item: (item['lines'][0], item['id']))


def _point_brief(point):
    if not point:
        return None
    return dict(line=point['line'], station=point.get('station'), time=str(point['time']),
                depth_m=point.get('depth_m'))


def scopes_map(anomalies):
    return {item['id']: item['scope_hash'] for item in anomalies}


def evaluate(sources, disabled_stations=None, excluded_lines=None):
    reports = calculate(sources, disabled_stations=disabled_stations or (),
                        excluded_lines=excluded_lines or ())
    return reports


def option_impacts(sources, anomaly):
    """Preview both rulings against the automatic quarantine baseline."""
    lines = anomaly['lines']
    baseline = calculate(sources)
    row_reports = calculate(sources, excluded_lines=lines)
    row_impact = diff_fleet(baseline['coverage'], row_reports['coverage'],
                            baseline['windows'], row_reports['windows'])
    impacts = {'row_only': row_impact}
    if anomaly.get('can_disable_station'):
        station_reports = calculate(sources, disabled_stations=[anomaly['station']],
                                    excluded_lines=lines)
        impacts['disable_station'] = diff_fleet(
            row_reports['coverage'], station_reports['coverage'],
            row_reports['windows'], station_reports['windows'])
        impacts['disable_vs_baseline'] = diff_fleet(
            baseline['coverage'], station_reports['coverage'],
            baseline['windows'], station_reports['windows'])
    return impacts


def open_review(store, journal, payload=None):
    payload = payload if isinstance(payload, dict) else {}
    run_id = payload.get('run_id') or payload.get('key') or payload.get('id')
    if run_id:
        receipt, _, reports = journal.get(run_id)
        run_id = receipt['run_id']
    else:
        if 'sources' in payload:
            source_payload = {'sources': payload['sources']}
        elif 'sample' in payload:
            source_payload = {'sample': payload['sample']}
        else:
            source_payload = {'sample': 'dirty'}
        sources = load_sources(source_payload)
        result = record(journal, sources)
        run_id = result['run_id']
        _, _, reports = journal.get(run_id)
    anomalies = build_anomalies(reports)
    existing_id = store.find_review_by_run(run_id)
    if existing_id:
        view = review_view(store, journal, existing_id)
        view['created'] = False
        view['reused'] = True
        return view
    store.ensure_review(run_id, run_id, scopes_map(anomalies))
    view = review_view(store, journal, run_id)
    view['created'] = True
    view['reused'] = False
    return view


def resolve_review_id(store, journal, review_id_or_run):
    try:
        store.get_review(review_id_or_run)
        return review_id_or_run
    except TideError as error:
        if error.code != 'NO_REVIEW':
            raise
    review_id = store.find_review_by_run(review_id_or_run)
    if review_id:
        return review_id
    # A journal run opened directly gets a review shell lazily.
    receipt, _, reports = journal.get(review_id_or_run)
    anomalies = build_anomalies(reports)
    store.ensure_review(receipt['run_id'], receipt['run_id'], scopes_map(anomalies))
    return receipt['run_id']


def review_view(store, journal, review_id):
    review_id = resolve_review_id(store, journal, review_id)
    case = store.get_review(review_id)
    revision = store.latest_revision(review_id)
    receipt, sources, reports = journal.get(revision['run_id'])
    anomalies = build_anomalies(reports)
    by_id = {item['id']: item for item in anomalies}
    decisions = store.list_decisions(review_id)
    active = {item['anomaly_id']: item for item in decisions if item['status'] == 'confirmed'}

    for anomaly in anomalies:
        anomaly['active_decision'] = active.get(anomaly['id'])
        try:
            anomaly['preview'] = option_impacts(sources, anomaly) if anomaly['adjudicable'] else None
        except TideError:
            raise
        except Exception:
            # Preview failures should not make the whole review page unavailable.
            anomaly['preview'] = None

    disabled_stations, excluded_lines = set(), set()
    for decision in active.values():
        if decision['action'] == 'disable_station':
            station = (decision.get('payload') or {}).get('station')
            if station:
                disabled_stations.add(station)
        excluded_lines.update((decision.get('payload') or {}).get('lines') or [])
    selected = calculate(sources, disabled_stations=disabled_stations,
                         excluded_lines=excluded_lines) if active else reports
    combined_impact = diff_fleet(reports['coverage'], selected['coverage'],
                                 reports['windows'], selected['windows']) if active else None
    block = store.issuance_block(review_id, revision['run_id'])
    if block is None and combined_impact and combined_impact['blocked_windows'] and any(
            d['action'] == 'disable_station' for d in active.values()):
        # Defensive recomputation for older stored decision impacts.
        block = dict(review_id=review_id, run_id=revision['run_id'], code='STATION_DISABLED',
                     reason_code='SIGN_OFF_BLOCKED', message='停用范围内不许签发',
                     blocks=[dict(windows=combined_impact['blocked_windows'])])
    return {
        'review_id': review_id,
        'id': review_id,
        'key': review_id,
        'run_id': revision['run_id'],
        'current_run_id': case['current_run_id'],
        'original_run_id': case['original_run_id'],
        'version': case['current_version'],
        'revision': revision['version'],
        'sample': None,
        'manifest': receipt,
        'anomalies': anomalies,
        'items': anomalies,
        'exceptions': anomalies,
        'bad_values': [item for item in anomalies if 'SENSOR_BAD' in item['codes'] or 'BAD_READING' in item['codes']],
        'rejected': reports['datum'].get('rejected', []),
        'decisions': decisions,
        'rulings': decisions,
        'active_decisions': list(active.values()),
        'active_rulings': list(active.values()),
        'history': decisions,
        'reports': reports,
        'coverage': reports['coverage'],
        'windows': reports['windows'],
        'sources': sources,
        'combined_impact': combined_impact,
        'impact': combined_impact,
        'issuance': {'allowed': block is None, 'block': block},
        'issuance_blocked': block is not None,
        'can_issue': block is None,
        'block': block,
    }


def find_anomaly(view, payload):
    anomaly_id_value = payload.get('anomaly_id') or payload.get('anomaly') or payload.get('anomaly_key')
    if anomaly_id_value:
        for anomaly in view['anomalies']:
            if anomaly['id'] == anomaly_id_value or anomaly.get('key') == anomaly_id_value:
                return anomaly
        try:
            line_number = int(anomaly_id_value)
        except (TypeError, ValueError):
            line_number = None
        if line_number is not None:
            for anomaly in view['anomalies']:
                if line_number in anomaly['lines']:
                    return anomaly
        raise TideError('NO_ANOMALY', '找不到这条潮尺异常', 404)
    line = payload.get('line') or payload.get('source_line') or payload.get('row')
    if line is not None and str(line).strip() != '':
        try:
            line_number = int(line)
        except (TypeError, ValueError) as exc:
            raise TideError('BAD_LINE', '异常行号不正确', 400) from exc
        for anomaly in view['anomalies']:
            if line_number in anomaly['lines']:
                return anomaly
        raise TideError('NO_ANOMALY', '找不到该行对应的潮尺异常', 404)
    station = payload.get('station')
    when = payload.get('observed_at') or payload.get('time')
    if station and when:
        try:
            target_time = moment(str(when))
        except ValueError:
            target_time = None
        for anomaly in view['anomalies']:
            if anomaly['station'] == station and (
                    anomaly['observed_at'] == target_time or anomaly['observed_text'] == when):
                return anomaly
    if len(view['anomalies']) == 1 and payload.get('allow_only'):
        return view['anomalies'][0]
    raise TideError('MISSING_ANOMALY', '请指定要裁决的异常', 400)


def decide(store, journal, review_id, payload, headers=None):
    headers = headers or {}
    review_id = resolve_review_id(store, journal, review_id)
    view = review_view(store, journal, review_id)
    anomaly = find_anomaly(view, payload)
    action = action_name(payload.get('action') or payload.get('decision') or
                         payload.get('ruling') or payload.get('choice') or
                         payload.get('disposition') or payload.get('type'))

    expected_scope = payload.get('scope_hash') or payload.get('expected_scope')
    if expected_scope and expected_scope != anomaly['scope_hash']:
        raise TideError('STALE_SCOPE', '晚到读数已改变异常范围，请按新范围复核', 409,
                        {'current_scope_hash': anomaly['scope_hash']})

    idem = (payload.get('idempotency_key') or payload.get('idempotency') or
            payload.get('request_key') or payload.get('request_id') or
            payload.get('client_token') or payload.get('token') or
            payload.get('Idempotency-Key') or payload.get('Idempotency-Token') or
            headers.get('Idempotency-Key') or headers.get('X-Idempotency-Key') or
            headers.get('Idempotency-Token'))
    # A bare "key" is accepted for retries when it is not the journal run key.
    if not idem and payload.get('key') and payload.get('key_use') == 'idempotency':
        idem = payload['key']
    operator = (payload.get('operator') or payload.get('by') or
                payload.get('reviewer') or payload.get('duty') or payload.get('user'))
    version_value = payload.get('version', payload.get('lock_version', payload.get('expected_version')))
    try:
        lock_version = int(version_value) if version_value not in (None, '') else None
    except (TypeError, ValueError) as exc:
        raise TideError('BAD_VERSION', '版本号不正确', 400) from exc

    existing_active = store.list_decisions(review_id, include_void=False)
    disabled_stations = set()
    excluded_lines = set()
    for decision in existing_active:
        data = decision.get('payload') or {}
        if decision['action'] == 'disable_station' and data.get('station'):
            disabled_stations.add(data['station'])
        excluded_lines.update(data.get('lines') or [])
    if payload.get('excluded_lines'):
        requested_lines = payload['excluded_lines']
    elif payload.get('lines'):
        requested_lines = payload['lines']
    elif payload.get('bad_line') is not None:
        requested_lines = [payload['bad_line']]
    elif payload.get('line') is not None:
        requested_lines = [payload['line']]
    else:
        requested_lines = anomaly['lines']
    lines = sorted(set(int(line) for line in requested_lines))
    if action == 'row_only':
        excluded_lines.update(lines)
    else:
        station = payload.get('station') or anomaly['station']
        if not station or not anomaly.get('can_disable_station', True):
            raise TideError('NO_STATION_TO_DISABLE', '该异常没有可停用的已登记测站', 422)
        disabled_stations.add(station)
        excluded_lines.update(int(line) for line in lines)

    sources = view['sources']
    selected = calculate(sources, disabled_stations=disabled_stations,
                         excluded_lines=excluded_lines)
    impact = diff_fleet(view['reports']['coverage'], selected['coverage'],
                        view['reports']['windows'], selected['windows'])
    preview = option_impacts(sources, anomaly)
    impact['this_option'] = preview.get(action)
    impact['options'] = preview

    core = [review_id, anomaly['id'], anomaly['scope_hash'], action,
            sorted(disabled_stations), sorted(excluded_lines)]
    decision_id = 'DEC' + digest(canonical_text(core))[:20].upper()
    if idem:
        decision_id = 'IDEM' + digest(str(idem))[:20].upper()
    request_payload = {
        'request': {key: value for key, value in payload.items()
                    if key not in {'sources'}},
        'anomaly': {key: anomaly[key] for key in ('id', 'code', 'station', 'lines', 'scope_hash')},
        'station': anomaly['station'],
        'lines': [int(line) for line in lines],
        'disabled_stations': sorted(disabled_stations),
        'excluded_lines': sorted(excluded_lines),
        'scope': anomaly['scope'],
    }
    record_data = dict(
        decision_id=decision_id, idempotency_key=str(idem) if idem else None,
        review_id=review_id, anomaly_id=anomaly['id'], scope_hash=anomaly['scope_hash'],
        action=action, operator=str(operator) if operator is not None else None,
        lock_version=lock_version, fingerprint=digest(canonical_text(core)),
        payload=request_payload, impact=impact)
    decision, created = store.submit_decision(record_data)
    updated_view = review_view(store, journal, review_id)
    return {'decision': decision, 'reused': not created, 'created': created,
            'review': updated_view, 'impact': decision.get('impact') or impact}


def canonical_text(value):
    from app.modules.journal.manifest import canonical
    return canonical(value)


def parse_reading_records(payload):
    payload = payload if isinstance(payload, dict) else {}
    container = payload.get('reading', payload.get('record', payload.get('late_reading', payload)))
    if isinstance(container, list):
        records = []
        for item in container:
            records.extend(parse_reading_records({'reading': item}))
        return records
    if payload.get('readings') and isinstance(payload['readings'], list):
        records = []
        for item in payload['readings']:
            records.extend(parse_reading_records({'reading': item}))
        return records
    for list_key in ('records', 'items', 'late_readings', 'observations'):
        if isinstance(payload.get(list_key), list):
            records = []
            for item in payload[list_key]:
                records.extend(parse_reading_records({'reading': item}))
            return records

    csv_text = None
    for key in ('observations_csv', 'reading_csv', 'csv', 'text', 'raw', 'observations', 'reading'):
        value = payload.get(key)
        if isinstance(value, str):
            csv_text = value
            break
    if csv_text is not None:
        return _parse_csv_readings(csv_text)

    if isinstance(container, dict) and any(key in container for key in OBSERVATION + ('time', 'value')):
        row = _reading_from_mapping(container)
        return [row]
    raise TideError('BAD_READING_RECORD', '晚到读数应为观测字段、CSV 行或 readings 列表', 400)


def _parse_csv_readings(text):
    sample = text.lstrip('﻿')
    reader = csv.reader(io.StringIO(sample, newline=''), strict=True)
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not rows:
        raise TideError('BAD_READING_RECORD', '晚到读数 CSV 为空', 400)
    if set(cell.strip() for cell in rows[0]) == set(OBSERVATION):
        reader = csv.DictReader(io.StringIO(sample, newline=''))
        return [_reading_from_mapping(row) for row in reader]
    return [dict(zip(OBSERVATION, row)) for row in rows]


def _reading_from_mapping(row):
    aliases = {
        'observed_at': ('observed_at', 'time', 'at', 'timestamp', '时刻'),
        'station': ('station', 'station_id', '站号', '测站'),
        'level': ('level', 'value', 'reading', '读数', '潮尺'),
        'unit': ('unit', 'units', '单位'),
        'quality': ('quality', 'flag', '质量'),
    }
    defaults = {'unit': 'cm', 'quality': 'OK'}
    required = {'station', 'observed_at', 'level'}
    result = {}
    for field, names in aliases.items():
        value = None
        for name in names:
            if name in row and row[name] is not None:
                value = row[name]
                break
        if value is None:
            if field in defaults:
                value = defaults[field]
            else:
                raise TideError('BAD_READING_RECORD', f'晚到读数缺少 {field}', 400)
        result[field] = str(value).strip()
    missing_required = [field for field in required if not result.get(field)]
    if missing_required:
        raise TideError('BAD_READING_RECORD', f'晚到读数缺少 {",".join(missing_required)}', 400)
    return result


def append_observations(sources, records):
    new_sources = deepcopy(sources)
    text = new_sources['observations']
    if text and not text.endswith(('\n', '\r')):
        text += '\n'
    for record in records:
        missing = [field for field in OBSERVATION if field not in record]
        if missing:
            raise TideError('BAD_READING_RECORD', f'晚到读数缺少字段：{",".join(missing)}', 400)
        # csv.writer handles any commas/quotes in supplied values.
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator='\n').writerow([record[field] for field in OBSERVATION])
        text += buffer.getvalue()
    new_sources['observations'] = text
    return new_sources


def receive_reading(store, journal, review_id, payload):
    review_id = resolve_review_id(store, journal, review_id)
    case = store.get_review(review_id)
    revision = store.latest_revision(review_id)
    _, sources, _ = journal.get(revision['run_id'])
    payload = payload if isinstance(payload, dict) else {}

    if 'sources' in payload:
        new_sources = load_sources({'sources': payload['sources']})
        reading_event = {'replaced_sources': True}
    elif 'sample' in payload:
        new_sources = load_sources({'sample': payload['sample']})
        reading_event = {'replaced_sources': True, 'sample': payload['sample']}
    else:
        records = parse_reading_records(payload)
        new_sources = append_observations(sources, records)
        reading_event = {'records': records}

    result = record(journal, new_sources)
    new_run_id = result['run_id']
    _, _, new_reports = journal.get(new_run_id)
    new_anomalies = build_anomalies(new_reports)
    new_scopes = scopes_map(new_anomalies)
    old_scopes = revision['scopes']
    changed = [aid for aid, scope_hash in new_scopes.items()
               if aid in old_scopes and old_scopes.get(aid) != scope_hash]
    added = [aid for aid in new_scopes if aid not in old_scopes]
    removed = [aid for aid in old_scopes if aid not in new_scopes]
    invalidated = sorted(set(changed + removed))
    store.add_reading_revision(review_id, new_run_id, new_scopes, reading_event, invalidated)
    view = review_view(store, journal, review_id)
    view['recalculated'] = True
    view['scope_changed'] = bool(invalidated)
    view['changed_anomalies'] = sorted(set(changed))
    view['removed_anomalies'] = sorted(set(removed))
    view['new_anomalies'] = sorted(set(added))
    view['invalidated_anomalies'] = invalidated
    view['new_run'] = result
    return view


class ReviewService:
    """Small object facade over the module-level functions."""

    def __init__(self, store, journal):
        self.store = store
        self.journal = journal

    def open(self, payload=None):
        return open_review(self.store, self.journal, payload or {})

    get = open

    def view(self, review_id):
        return review_view(self.store, self.journal, review_id)

    def decide(self, review_id, payload, headers=None):
        return decide(self.store, self.journal, review_id, payload, headers)

    adjudicate = decide
    rule = decide

    def add_reading(self, review_id, payload):
        return receive_reading(self.store, self.journal, review_id, payload)

    late_reading = add_reading
    receive_reading = add_reading


# Common verb aliases.
adjudicate = decide
rule_on = decide
add_late_reading = receive_reading
