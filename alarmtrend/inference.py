"""Inference called by sync/predict/manual commands, never by HTTP reads."""
import logging
import math
from datetime import timedelta

import numpy as np

from .data import midnight
from .db import dt, getmeta, iso
from .estimators import (analog_predict, fit_survival, regression_predict, survival_predict)
from .learning_data import Timeline
from .source import KYIV
from .training import read_artifact

log = logging.getLogger(__name__)


def rounded_minutes(value):
    step = 1 if value < 10 else 5
    return max(0, int(math.floor(value/step+.5))*step)


def apply_models(result, db, settings, now, rows, coverage, complete):
    artifact = read_artifact(settings, db, now)
    result['components'] = {}
    result['model_id'] = artifact['id'] if artifact else None
    result['model'] = artifact['version'] if artifact else 'historical-analogs-v2'
    result['horizons'] = {'onset_minutes': [15, 30, 60],
                          'count_until': iso(midnight(now.astimezone(KYIV).date()+timedelta(days=1))),
                          'survival_coverage': .5, 'count_coverage': .8}
    if not coverage:
        return result
    timeline = Timeline(rows, coverage, complete, now)
    local = now.astimezone(KYIV)
    x = timeline.features([now])
    # Baseline artifacts are on the same grid as evaluation. Choose the latest
    # local clock slot; never interpolate probability labels across horizons.
    seconds = local.hour*3600+local.minute*60+local.second
    slot = seconds//settings.interval*settings.interval
    feature_days = set(complete)
    if not any(not r['message'].startswith('Текстова') and r['stamp'] and
               dt(r['stamp']).astimezone(KYIV).date() == local.date() for r in result['issues']):
        feature_days.add(local.date().isoformat())
    feature_timeline = Timeline(rows, coverage, feature_days, now)
    recent_known = feature_timeline.known_span(now-timedelta(hours=24), now)
    healthy = recent_known and result['status'] != 'unknown' and not (
        not getmeta(db, 'last_success') or (now-dt(getmeta(db, 'last_success'))).total_seconds() > settings.interval*2)
    for task in ('onset', 'count', 'duration'):
        intended = artifact['selected'][task] if artifact else 'historical'
        meta = {'model': 'historical', 'selected': intended, 'fallback': artifact is None,
                'applicable': task != 'onset' or result['status'] == 'clear',
                'reason': 'Немає сумісного перевіреного артефакту' if not artifact else None,
                'trained_at': artifact['trained_at'] if artifact else None,
                'training': artifact['fitted']['training'] if artifact else None}
        meta['observations'] = (result['observations'] if task == 'onset' else
                                (result.get('count') or {}).get('n', 0) if task == 'count' else
                                result['duration_observations'])
        if not artifact:
            meta['training'] = {'from': iso(coverage), 'to': iso(midnight(local.date())),
                                'days': sum(day < local.date().isoformat() for day in complete),
                                'onset_slices': result['observations'],
                                'count_slices': (result.get('count') or {}).get('n', 0),
                                'survival_events': result['duration_observations']}
            if task == 'onset':
                meta['reserve_parameters'] = {'smoothing': .5, 'n': result['observations'],
                                               'probabilities': result['probabilities']}
            elif task == 'count':
                meta['reserve_parameters'] = result.get('count')
        result['components'][task] = meta
        candidates = [intended] if intended == 'historical' else [intended, 'historical']
        if task == 'duration':
            specs = artifact['fitted'][task] if artifact else {}
            if not specs.get('historical'):
                # A descriptive KM reserve includes censored alarms. Persist
                # its parameters in the issued forecast when no artifact exists.
                try:
                    baseline = fit_survival('historical', timeline.survival_data(midnight(local.date())))
                    specs = {**specs, 'historical': baseline}
                    meta['reserve_parameters'] = baseline
                    meta['training']['survival_events'] = baseline['n']
                except ValueError:
                    pass
        else:
            specs = artifact['fitted'][task] if artifact else {}
        for name in candidates:
            spec = specs.get(name)
            if not spec:
                continue
            try:
                if name != 'historical' and not healthy:
                    raise ValueError('Невідомий статус, застарілі дані або прогалина в ознаках')
                if task == 'onset':
                    if result['status'] != 'clear':
                        break
                    values = analog_predict(spec, [slot], task)[0] if name == 'historical' else regression_predict(spec, x)[0]
                    if not np.isfinite(values).all():
                        raise ValueError('Недостатньо аналогів')
                    meta['raw'] = values.tolist()
                    meta['observations'] = (spec.get('groups', {}).get(str(slot), {}).get('n', 0)
                                            if name == 'historical' else spec['observations'])
                    result['probabilities'] = [int(math.floor(100*p+.5)) for p in values]
                elif task == 'count':
                    values = analog_predict(spec, [slot], task)[0] if name == 'historical' else regression_predict(spec, x)[0]
                    if not np.isfinite(values).all():
                        raise ValueError('Нестійкий прогноз кількості')
                    meta['raw'] = values.tolist()
                    result['count'] = {'expected': float(values[0]), 'estimate': int(math.floor(values[0]+.5)),
                                       'low': int(values[1]), 'high': int(values[2]), 'coverage': .8,
                                       'n': spec.get('observations', spec.get('groups', {}).get(str(slot), {}).get('count_n', 0))}
                    meta['observations'] = result['count']['n']
                else:
                    start = dt(result['active_since']) if result['status'] == 'active' else now
                    total = survival_predict(spec, [start])[0]
                    if not np.isfinite(total[:3]).all():
                        raise ValueError('Нестійка оцінка тривалості')
                    n = spec['n']
                    meta['observations'] = n
                    result['duration'] = dict(zip(('median', 'low', 'high'), [rounded_minutes(v) for v in total[:3]]), n=n, coverage=.5)
                    result['duration_observations'] = n
                    meta['raw_total'] = total[:3].tolist()
                    if result['status'] == 'active':
                        elapsed = (now-start).total_seconds()/60
                        residual = survival_predict(spec, [start], [elapsed])[0]
                        if not np.isfinite(residual).all():
                            raise ValueError('Недостатньо даних у хвості розподілу')
                        meta['raw_remaining'] = residual.tolist()
                        result['remaining'] = dict(zip(('median', 'low', 'high'), [rounded_minutes(v) for v in residual[:3]]), n=n, coverage=.5)
                        result['survive60'] = int(math.floor(100*residual[3]+.5))
                meta.update(model=name, fallback=(not artifact or name != intended))
                if name == intended:
                    meta['reason'] = None if artifact else meta['reason']
                break
            except (ValueError, FloatingPointError, OverflowError, KeyError) as exc:
                log.warning('%s inference fallback: %s', task, exc)
                meta.update(fallback=True, reason=str(exc))
        else:
            if task == 'duration':
                # Do not silently reuse complete-case durations after a failed
                # censored-tail estimate.
                result['duration'] = result['remaining'] = result['survive60'] = None
    if result['probabilities']:
        result['probabilities'] = [int(math.floor(p+.5)) for p in result['probabilities']]
    return result
