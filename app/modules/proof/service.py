from app.errors import TideError

from .signature import seal, verifies


def issue(receipt, sources, reports, reviews=None):
    if not verifies(reports, receipt['result_sha256']):
        raise TideError('INVALID_PROOF', '归档结果校验未通过', 409)
    if set(sources) != set(receipt['inputs']):
        raise TideError('INVALID_PROOF', '归档原件清单不符', 409)
    from app.modules.journal.manifest import digest
    if any(digest(text) != receipt['inputs'][name] for name, text in sources.items()):
        raise TideError('INVALID_PROOF', '归档原件校验未通过', 409)
    evidence = {'manifest': receipt, 'originals': sources, 'reports': reports}
    if reviews is not None:
        block = None
        if isinstance(reviews, dict):
            block = reviews.get('issuance_block') or reviews.get('block')
            if not block and reviews.get('issuance_blocked'):
                block = dict(code='STATION_DISABLED', message='停用范围内不许签发')
            if not block:
                for decision in reviews.get('decisions', []):
                    if decision.get('status') == 'confirmed' and decision.get('action') == 'disable_station':
                        blocked = (decision.get('impact') or {}).get('blocked_windows')
                        if blocked:
                            block = dict(code='STATION_DISABLED', message='停用范围内不许签发',
                                         blocks=[dict(windows=blocked)])
                            break
        if block:
            details = block if isinstance(block, dict) else {}
            raise TideError(details.get('code', 'STATION_DISABLED'),
                            details.get('message', '停用范围内不许签发'), 409, details)
        evidence['reviews'] = reviews
    return {**evidence, 'seal_sha256': seal(evidence), 'verified': True}
