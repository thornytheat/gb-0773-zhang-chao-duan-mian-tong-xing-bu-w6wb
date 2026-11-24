import os
from urllib.parse import urlencode

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from fractions import Fraction

from app.api import _issuance_block, journal, reviews
from app.clock import instant
from app.config import RULES
from app.errors import TideError
from app.flow import calculate
from app.modules.journal.service import record
from app.modules.proof.service import issue
from app.modules.review import service as review_service
from app.reasons import REASONS
from app.source import load_sources

web = Blueprint('tide_desk', __name__)
CHAPTERS = {'coverage': '船期覆盖勾稽', 'readings': '潮尺读数采信', 'review': '值班复核',
            'datum': '站点基准统一',
            'crossings': '水位阈值交点', 'windows': '全程可行潮窗', 'journal': '运行留档',
            'proof': '潮窗证据签发'}
STATES = {'open': '可行', 'blocked': '不可行', 'unknown': '缺证'}


@web.get('/')
@web.get('/desk/<chapter>')
def desk(chapter='coverage', review_id=None):
    if chapter not in CHAPTERS:
        raise TideError('NO_STAGE', '找不到潮窗模块', 404)
    sample = request.args.get('sample', 'dirty')
    key = review_id or request.args.get('run')
    review_view = None
    if chapter == 'review':
        if key:
            review_view = review_service.review_view(reviews(), journal(), key)
        else:
            review_view = review_service.open_review(reviews(), journal(), {'sample': sample})
        key = review_view['run_id']
        receipt, sources, reports = journal().get(key)
    elif key or chapter in ('journal', 'proof'):
        key = key or record(journal(), load_sources({'sample': sample}))['run_id']
        receipt, sources, reports = journal().get(key)
    else:
        sources = load_sources({'sample': sample})
        reports = calculate(sources)
        receipt = None
    vessel = request.args.get('vessel', next(iter(reports['coverage']['ships'])))
    if vessel not in reports['coverage']['ships']:
        raise TideError('NO_VESSEL', '找不到该船期', 404)
    proof_block = _issuance_block(key) if chapter == 'proof' else None
    proof = issue(receipt, sources, reports) if chapter == 'proof' and proof_block is None else None
    error_code = request.args.get('error')
    error_message = request.args.get('message')
    return render_template('base.html', chapter=chapter, chapters=CHAPTERS, states=STATES, sample=sample,
                           receipt=receipt, key=key, reports=reports, vessel=vessel, reason=REASONS,
                           review=review_view,
                           desk_error=dict(code=error_code, message=error_message) if error_code else None,
                           gauge_fill=min(100, float(Fraction(reports['windows'][vessel]['order']['threshold_m'])) * 25),
                           seal=proof['seal_sha256'] if proof else None, time=instant, rule=RULES,
                           proof_block=proof_block,
                           history=journal().list() if chapter == 'journal' else [])


@web.get('/review')
def review_home():
    return desk('review')


@web.get('/review/<review_id>')
def review_by_id(review_id):
    return desk('review', review_id)


@web.get('/desk/audit')
@web.get('/desk/adjudicate')
@web.get('/desk/rulings')
@web.get('/audit')
@web.get('/adjudicate')
def review_alias():
    return desk('review')


@web.get('/desk/review/<review_id>')
def desk_review_by_id(review_id):
    return desk('review', review_id)


def _redirect_review(review_id, **params):
    params = {key: value for key, value in params.items() if value is not None}
    query = urlencode(params)
    base = url_for('tide_desk.desk', chapter='review')
    return redirect(f'{base}?{query}' if query else base, code=303)


def _form_error(review_id, error):
    try:
        current = review_service.review_view(reviews(), journal(), review_id)
        params = dict(run=current['run_id'], error=error.code, message=str(error))
        target_id = current['review_id']
    except TideError:
        params = dict(error=error.code, message=str(error))
        target_id = review_id
    return _redirect_review(target_id, **params)


@web.post('/desk/review/<review_id>/decide')
@web.post('/review/<review_id>/decide')
@web.post('/review/<review_id>/decision')
def review_decide(review_id):
    json_data = request.get_json(silent=True)
    is_json = json_data is not None or 'application/json' in request.headers.get('Accept', '')
    data = json_data if isinstance(json_data, dict) else request.form.to_dict(flat=True)
    if not isinstance(json_data, dict):
        data.update({key: value for key, value in request.args.items() if key not in data})
    try:
        result = review_service.decide(reviews(), journal(), review_id, data, request.headers)
    except TideError as error:
        if is_json:
            raise
        return _form_error(review_id, error)
    if is_json:
        from app.api import _decision_response
        return _decision_response(result)
    view = result['review']
    return _redirect_review(view['review_id'], run=view['run_id'], decided=result['decision']['decision_id'])


@web.post('/desk/review/<review_id>/readings')
@web.post('/review/<review_id>/readings')
def review_reading(review_id):
    json_data = request.get_json(silent=True)
    is_json = json_data is not None
    data = json_data if isinstance(json_data, dict) else request.form.to_dict(flat=True)
    if request.files:
        upload = request.files.get('file')
        if upload is not None:
            data['observations_csv'] = upload.read().decode('utf-8')
    try:
        view = review_service.receive_reading(reviews(), journal(), review_id, data)
    except TideError as error:
        if is_json:
            raise
        return _form_error(review_id, error)
    if is_json:
        return jsonify(view), 201
    return _redirect_review(view['review_id'], run=view['run_id'],
                            scope_changed='1' if view['scope_changed'] else '0')
