from collections import Counter

from .catalog import catalog
from .resolve import choose, translate


def normalize(intake, disabled_stations=None, excluded_lines=None):
    stations = catalog(intake['tables']['stations'], intake['rejected'])
    disabled_stations = {station.strip() for station in (disabled_stations or ())}
    excluded_lines = {int(line) for line in (excluded_lines or ())}
    rejected, candidates = [], []

    def reject(row, code):
        rejected.append(dict(source='observations', line=row['line'], raw=row['raw'], code=code))

    for row in intake['tables']['observations']:
        error, point = translate(row, stations)
        if error:
            reject(row, error)
        elif row['line'] in excluded_lines:
            reject(row, 'RULED_EXCLUDE')
        elif point['station'] in disabled_stations:
            reject(row, 'STATION_DISABLED')
        else:
            candidates.append(point)
    chosen = choose(candidates, reject)
    sections = {}
    for station in stations.values():
        name = station['section']
        sections[name] = dict(offset_s=station['offset_s'], gap_s=station['gap_s'], points=chosen.get(name, []))
    return {'stations': stations, 'sections': sections, 'rejected': sorted(rejected, key=lambda r: r['line']),
            'reasons': dict(Counter(row['code'] for row in rejected)),
            'disabled_stations': sorted(disabled_stations),
            'excluded_lines': sorted(excluded_lines),
            'accepted': sum(len(section['points']) for section in sections.values())}
