from fractions import Fraction

from flask import Blueprint, current_app, jsonify, request

from app.clock import moment
from app.errors import TideError
from app.flow import STEPS, calculate
from app.modules.journal.service import record
from app.modules.proof.service import issue
from app.modules.review import service as review_service
from app.source import load_sources

api = Blueprint('api', __name__, url_prefix='/api')


def journal():
    return current_app.extensions['tide_journal']


def reviews():
    return current_app.extensions['tide_reviews']


def request_data():
    data = request.get_json(silent=True)
    if isinstance(data, str):
        data = {'csv': data}
    if data is None:
        if request.form:
            data = request.form.to_dict(flat=True)
            upload = request.files.get('file') or request.files.get('observations')
            if upload is not None:
                data['observations_csv'] = upload.read().decode('utf-8')
        elif request.args:
            data = request.args.to_dict(flat=True)
        elif request.mimetype in ('text/csv', 'text/plain', 'application/csv'):
            data = {'csv': request.get_data(as_text=True)}
        else:
            data = {}
    if not isinstance(data, dict):
        raise TideError('BAD_REQUEST', '请求内容应为 JSON 对象或表单', 400)
    return data


@api.get('/reviews')
@api.get('/review')
def review_index():
    return jsonify(reviews=reviews().list_reviews())


@api.post('/reviews')
@api.post('/review')
def create_review():
    data = request_data()
    view = review_service.open_review(reviews(), journal(), data)
    if _decision_requested(data):
        result = review_service.decide(reviews(), journal(), view['review_id'], data, request.headers)
        return _decision_response(result)
    return jsonify(_review_response(view)), 201 if view.get('created', True) else 200


def _decision_requested(data):
    return any(data.get(key) for key in ('action', 'decision', 'ruling', 'choice', 'disposition')) and (
        data.get('anomaly_id') or data.get('anomaly') or data.get('line') or
        (data.get('station') and data.get('observed_at')))


@api.get('/reviews/<review_id>')
@api.get('/review/<review_id>')
def get_review(review_id):
    view = review_service.review_view(reviews(), journal(), review_id)
    return jsonify(_review_response(view))


@api.get('/journal/<key>/review')
def journal_review(key):
    review_id = reviews().find_review_by_run(key) or key
    view = review_service.review_view(reviews(), journal(), review_id)
    return jsonify(_review_response(view))


@api.get('/anomalies')
@api.get('/review/anomalies')
@api.get('/reviews/anomalies')
def list_anomalies():
    review_id = request.args.get('review_id') or request.args.get('run_id') or request.args.get('key')
    if review_id:
        view = review_service.review_view(reviews(), journal(), review_id)
    else:
        view = review_service.open_review(reviews(), journal(), {'sample': request.args.get('sample', 'dirty')})
    return jsonify(review_id=view['review_id'], run_id=view['run_id'],
                   anomalies=view['anomalies'], items=view['anomalies'])


@api.get('/journal/<key>/anomalies')
def journal_anomalies(key):
    view = review_service.review_view(reviews(), journal(), key)
    return jsonify(review_id=view['review_id'], anomalies=view['anomalies'])


@api.route('/reviews/<review_id>', methods=['POST', 'PUT', 'PATCH'])
@api.route('/review/<review_id>', methods=['POST', 'PUT', 'PATCH'])
@api.post('/reviews/<review_id>/anomalies/<anomaly_id>/decision')
@api.post('/reviews/<review_id>/decisions')
@api.post('/reviews/<review_id>/decision')
@api.post('/reviews/<review_id>/rulings')
@api.post('/reviews/<review_id>/ruling')
@api.post('/reviews/<review_id>/rule')
@api.post('/reviews/<review_id>/verdict')
@api.post('/reviews/<review_id>/decide')
@api.post('/reviews/<review_id>/adjudicate')
@api.post('/review/<review_id>/decisions')
@api.post('/review/<review_id>/decision')
@api.post('/review/<review_id>/rulings')
@api.post('/review/<review_id>/ruling')
@api.post('/review/<review_id>/rule')
@api.post('/review/<review_id>/verdict')
@api.post('/review/<review_id>/adjudicate')
@api.post('/journal/<key>/review')
@api.post('/journal/<key>/anomalies/<anomaly_id>/decision')
@api.post('/journal/<key>/decision')
@api.post('/journal/<key>/decide')
@api.post('/journal/<key>/decisions')
@api.post('/journal/<key>/ruling')
@api.post('/journal/<key>/rule')
@api.post('/journal/<key>/rulings')
def submit_decision(review_id=None, key=None, anomaly_id=None):
    review_id = review_id or key
    data = request_data()
    if anomaly_id:
        data['anomaly_id'] = anomaly_id
    result = review_service.decide(reviews(), journal(), review_id, data, request.headers)
    return _decision_response(result)


def _decision_response(result):
    decision = result['decision']
    impact = result.get('impact') or {}
    status = 200 if result['reused'] else 201
    return jsonify(dict(decision=decision, disposition=decision, ruling=decision,
                        reused=result['reused'], duplicate=result['reused'],
                        confirmed=decision.get('status') == 'confirmed',
                        impact=impact,
                        ships=impact.get('ships'), vessels=impact.get('vessels'),
                        schedule_changes=impact.get('changes_by_vessel'),
                        changed_windows=impact.get('changes_by_vessel'),
                        blocked_windows=impact.get('blocked_windows'),
                        windows=result['review'].get('windows'),
                        coverage=result['review'].get('coverage'),
                        reports=result['review'].get('reports'),
                        review=_review_response(result['review']))), status


@api.post('/reviews/<review_id>/exclude')
@api.post('/reviews/<review_id>/remove-row')
@api.post('/review/<review_id>/exclude')
@api.post('/journal/<key>/exclude')
def exclude_bad_row(review_id=None, key=None):
    data = request_data()
    data['action'] = 'row_only'
    result = review_service.decide(reviews(), journal(), review_id or key, data, request.headers)
    return _decision_response(result)


@api.post('/reviews/<review_id>/disable')
@api.post('/reviews/<review_id>/suspend')
@api.post('/review/<review_id>/disable')
@api.post('/journal/<key>/disable')
def disable_station(review_id=None, key=None):
    data = request_data()
    data['action'] = 'disable_station'
    result = review_service.decide(reviews(), journal(), review_id or key, data, request.headers)
    return _decision_response(result)


@api.post('/adjudicate')
@api.post('/review/decision')
@api.post('/reviews/decision')
@api.post('/decisions')
@api.post('/rulings')
def submit_global_decision():
    data = request_data()
    review_id = data.get('review_id') or data.get('run_id') or data.get('journal_id') or data.get('key')
    if not review_id:
        raise TideError('MISSING_REVIEW', '请提供 review_id 或 run_id', 400)
    result = review_service.decide(reviews(), journal(), review_id, data, request.headers)
    return _decision_response(result)


@api.post('/anomaly/<anomaly_id>/decision')
@api.post('/anomaly/<anomaly_id>/rule')
@api.post('/anomalies/<anomaly_id>/decision')
def submit_anomaly_decision(anomaly_id):
    data = request_data()
    review_id = data.pop('review_id', None) or data.pop('run_id', None) or data.pop('key', None)
    if not review_id:
        raise TideError('MISSING_REVIEW', '请在请求中提供 review_id 或 run_id', 400)
    data['anomaly_id'] = anomaly_id
    result = review_service.decide(reviews(), journal(), review_id, data, request.headers)
    return _decision_response(result)


@api.post('/journal/<key>/readings')
@api.post('/journal/<key>/reading')
@api.post('/journal/<key>/late-readings')
@api.post('/journal/<key>/late-reading')
@api.post('/reviews/<review_id>/readings')
@api.post('/reviews/<review_id>/reading')
@api.post('/reviews/<review_id>/late-readings')
@api.post('/reviews/<review_id>/late_reading')
@api.post('/reviews/<review_id>/late-reading')
@api.post('/review/<review_id>/readings')
@api.post('/review/<review_id>/reading')
@api.post('/review/<review_id>/late-readings')
@api.post('/review/<review_id>/late-reading')
@api.post('/review/<review_id>/late_readings')
@api.post('/review/<review_id>/late_reading')
@api.post('/anomalies/<anomaly_id>/readings')
@api.post('/anomalies/<anomaly_id>/late-reading')
def late_reading(review_id=None, anomaly_id=None, key=None):
    review_id = review_id or key
    if review_id is None:
        data = request_data()
        review_id = data.get('review_id') or data.get('run_id') or data.get('key')
        if not review_id:
            raise TideError('MISSING_REVIEW', '请在请求中提供 review_id 或 run_id', 400)
    view = review_service.receive_reading(reviews(), journal(), review_id, request_data())
    return jsonify(_review_response(view)), 201


@api.post('/review/readings')
@api.post('/reviews/readings')
@api.post('/late-readings')
@api.post('/late_readings')
@api.post('/readings/late')
def global_late_reading():
    data = request_data()
    review_id = data.get('review_id') or data.get('run_id') or data.get('key')
    if not review_id:
        raise TideError('MISSING_REVIEW', '请提供 review_id 或 run_id', 400)
    view = review_service.receive_reading(reviews(), journal(), review_id, data)
    return jsonify(_review_response(view)), 201


@api.get('/reviews/<review_id>/status')
@api.get('/review/<review_id>/status')
@api.get('/reviews/<review_id>/decision')
@api.get('/reviews/<review_id>/decisions/active')
@api.get('/review/<review_id>/decision')
def active_review_decisions(review_id):
    resolved = review_service.resolve_review_id(reviews(), journal(), review_id)
    decisions = reviews().list_decisions(resolved, include_void=False)
    return jsonify(review_id=resolved, decisions=decisions, decision=decisions[0] if len(decisions) == 1 else None)


@api.get('/reviews/<review_id>/history')
@api.get('/review/<review_id>/history')
def review_history(review_id):
    resolved = review_service.resolve_review_id(reviews(), journal(), review_id)
    return jsonify(review_id=resolved, decisions=reviews().list_decisions(resolved),
                   history=reviews().list_decisions(resolved))


@api.get('/journal/<key>/decisions')
def journal_decision_history(key):
    return review_history(key)


@api.get('/anomaly/<anomaly_id>/decisions')
@api.get('/anomaly/<anomaly_id>/history')
@api.get('/anomalies/<anomaly_id>/decisions')
@api.get('/anomalies/<anomaly_id>/history')
def anomaly_history(anomaly_id):
    decisions = reviews().list_decisions_for_anomaly(anomaly_id)
    return jsonify(anomaly_id=anomaly_id, decisions=decisions, history=decisions)


@api.get('/decisions/<decision_id>')
def get_decision(decision_id):
    return jsonify(reviews().get_decision(decision_id))


def _review_response(view):
    """Remove server-only convenience aliases while retaining a rich API."""
    return {key: value for key, value in view.items()}


@api.route('/stage/<stage>', methods=['GET', 'POST'])
def stage_result(stage):
    if stage not in STEPS:
        raise TideError('NO_STAGE', '找不到潮窗模块', 404)
    if 'run' in request.args and request.method == 'GET':
        _, _, reports = journal().get(request.args['run'])
    else:
        payload = request.get_json(silent=True) if request.method == 'POST' else {'sample': request.args.get('sample', 'dirty')}
        reports = calculate(load_sources(payload), stage)
    return jsonify(reports[stage])


@api.get('/journal')
def list_journal():
    return jsonify(runs=journal().list())


@api.post('/journal')
def publish_journal():
    result = record(journal(), load_sources(request.get_json(silent=True)))
    return jsonify(result), 200 if result['reused'] else 201


@api.get('/journal/<key>')
def inspect_journal(key):
    receipt, _, reports = journal().get(key)
    return jsonify(run_id=key, manifest=receipt, coverage=reports['coverage'])


@api.post('/journal/<key>/rerun')
def repeat_journal(key):
    manifest, sources, _ = journal().get(key)
    return jsonify(record(journal(), sources, manifest))


@api.get('/reviews/<review_id>/report')
@api.get('/reviews/<review_id>/reports')
def review_reports(review_id):
    view = review_service.review_view(reviews(), journal(), review_id)
    return jsonify(run_id=view['run_id'], reports=view['reports'], coverage=view['coverage'],
                   windows=view['windows'], impact=view['impact'])


@api.get('/reviews/<review_id>/issuance')
@api.post('/reviews/<review_id>/issuance')
def review_issuance(review_id):
    view = review_service.review_view(reviews(), journal(), review_id)
    scope = _issuance_scope()
    block = _issuance_block(view['run_id'], scope)
    return jsonify(review_id=view['review_id'], run_id=view['run_id'], scope=scope,
                   allowed=block is None, issuance_allowed=block is None, block=block)


@api.get('/reviews/<review_id>/export')
@api.get('/review/<review_id>/export')
def review_export(review_id):
    view = review_service.review_view(reviews(), journal(), review_id)
    return download(view['run_id'])


@api.post('/reviews/<review_id>/export')
@api.post('/reviews/<review_id>/sign')
@api.post('/reviews/<review_id>/issue')
def review_sign(review_id):
    view = review_service.review_view(reviews(), journal(), review_id)
    return download(view['run_id'])


@api.get('/export/<key>')
def download(key):
    scope = _issuance_scope()
    block = _issuance_block(key, scope)
    if block is not None:
        raise TideError(block['code'], block['message'], 409, block)
    receipt, sources, reports = journal().get(key)
    review_id = reviews().find_review_by_run(key)
    review_bundle = reviews().export_bundle(review_id) if review_id else None
    response = jsonify(issue(receipt, sources, reports, reviews=review_bundle))
    response.headers['Content-Disposition'] = f'attachment; filename="tide-{key[:12]}.json"'
    return response


@api.post('/export/<key>')
@api.post('/issue/<key>')
@api.post('/sign/<key>')
@api.post('/journal/<key>/sign')
def sign_off(key):
    return download(key)


def _issuance_scope():
    if request.method == 'GET':
        data = request.args.to_dict(flat=True)
    else:
        data = request_data()
    scope = {}
    if data.get('vessel'):
        scope['vessel'] = data['vessel']
    for field in ('start', 'scope_start', 'window_start'):
        if data.get(field):
            scope['start'] = _scope_time(data[field])
            break
    for field in ('end', 'scope_end', 'window_end'):
        if data.get(field):
            scope['end'] = _scope_time(data[field])
            break
    return scope


def _scope_time(value):
    if isinstance(value, (int, float, Fraction)):
        return Fraction(value)
    text = str(value)
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return Fraction(moment(text))


def _issuance_block(run_key, scope=None):
    review_id = reviews().find_review_by_run(run_key)
    if not review_id:
        return None
    revision = reviews().revision_by_run(run_key)
    block = reviews().issuance_block(review_id, run_key)
    if block is None:
        return None
    # Old revisions whose ruling was invalidated must not keep serving a stale
    # block; the history remains queryable.
    if revision and revision['version'] != reviews().get_review(review_id)['current_version']:
        active = reviews().list_decisions(review_id, include_void=False)
        matching = [d for d in active if d['scope_hash'] in set(revision['scopes'].values())]
        if not matching:
            return None
    if scope:
        filtered_blocks = []
        for entry in block.get('blocks', []):
            windows = []
            for window in entry.get('windows', []):
                if scope.get('vessel') and window.get('vessel') != scope['vessel']:
                    continue
                if 'start' in scope and 'end' in scope:
                    start, end = Fraction(window['start']), Fraction(window['end'])
                    if max(start, scope['start']) >= min(end, scope['end']):
                        continue
                windows.append(window)
            if windows:
                filtered_blocks.append({**entry, 'windows': windows})
        if not filtered_blocks:
            return None
        block = {**block, 'blocks': filtered_blocks}
    return block
