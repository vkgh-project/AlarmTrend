"""Purged expanding-window comparison; folds are clusters of calendar days."""
import json
import logging
import math
from datetime import timedelta

import numpy as np

from .data import midnight
from .db import connect, dt, getmeta, iso, setmeta, utcnow
from .estimators import (analog_predict, fit_analogs, fit_km, fit_regression,
                         fit_survival, km_survival, regression_predict,
                         survival_predict, tune_logistic)
from .learning_data import Timeline, day_weights
from .model import history
from .source import KYIV

ONSET = ('historical', 'logistic')
COUNT = ('historical', 'poisson', 'negative_binomial')
SURVIVAL = ('historical', 'weibull_aft', 'lognormal_aft')
log = logging.getLogger(__name__)


def load_timeline(settings, at=None):
    with connect(settings) as db:
        coverage = dt(getmeta(db, 'coverage_start'))
        until = dt(getmeta(db, 'last_success'))
        complete = {r[0] for r in db.execute('SELECT day FROM daily WHERE complete=1')}
        rows = history(db)
    at = at or utcnow()
    if not coverage or not until:
        return None
    return Timeline(rows, max(coverage, midnight(settings.start_date)), complete, min(at, until))


def fit_candidates(timeline, data, cutoff):
    train = data['label_end'] <= cutoff.timestamp()
    analogs = fit_analogs(data, train)
    result = {'onset': {'historical': analogs}, 'count': {'historical': analogs},
              'duration': {}, 'errors': {}, 'training_end': iso(cutoff)}
    inactive = train & data['inactive']
    try:
        result['onset']['logistic'] = tune_logistic(
            data['x'][inactive], data['onset'][inactive], data['days'][inactive],
            data['label_end'][inactive], [a for a, m in zip(data['anchors'], inactive) if m])
    except (ValueError, IndexError) as exc:
        result['errors']['logistic'] = str(exc)
    for name in COUNT[1:]:
        try:
            if len(set(data['days'][train])) < 14:
                raise ValueError('Need 14 complete training days')
            result['count'][name] = fit_regression(name, data['x'][train], data['count'][train], data['days'][train])
        except ValueError as exc:
            result['errors'][name] = str(exc)
    records = timeline.survival_data(cutoff)
    for name in SURVIVAL:
        try:
            result['duration'][name] = fit_survival(name, records)
        except ValueError as exc:
            result['errors'][name] = str(exc)
    result['training'] = {'from': iso(timeline.coverage), 'to': iso(cutoff),
                          'days': len(set(data['days'][train])),
                          'onset_slices': int(inactive.sum()), 'count_slices': int(train.sum()),
                          'survival_events': len(records),
                          'censored': sum(not r['event'] for r in records)}
    return result


def onset_metrics(y, p, days):
    w = day_weights(days)
    clipped = np.clip(p, 1e-8, 1-1e-8)
    calibration = []
    for h in range(3):
        bins = []
        for lo in np.arange(0, 1, .2):
            m = (p[:, h] >= lo) & (p[:, h] < lo+.2 if lo < .79 else p[:, h] <= 1)
            bins.append({'from': round(float(lo), 1), 'to': round(float(lo+.2), 1), 'n': int(m.sum()),
                         'predicted': float(np.average(p[m, h], weights=w[m])) if m.any() else None,
                         'observed': float(np.average(y[m, h], weights=w[m])) if m.any() else None})
        calibration.append(bins)
    return {'n': len(y), 'days': len(set(days)),
            'brier': np.sum(w[:, None]*(p-y)**2, axis=0).tolist(),
            'log_loss': (-np.sum(w[:, None]*(y*np.log(clipped)+(1-y)*np.log1p(-clipped)), axis=0)).tolist(),
            'calibration': calibration}


def count_metrics(y, p, days):
    w = day_weights(days)
    return {'n': len(y), 'mae': float(w@np.abs(p[:, 0]-y)),
            'coverage': float(w@((p[:, 1] <= y) & (y <= p[:, 2]))),
            'width': float(w@(p[:, 2]-p[:, 1])), 'nominal_coverage': .8}


def survival_metrics(records, predictions, censor_table, elapsed=None):
    """Normalized IPCW; training-only censor KM; common eligibility/positivity.

    A censored duration is never treated as an observed end. Repeated active
    slices have total weight one per alarm before censor weighting.
    """
    elapsed = np.zeros(len(records)) if elapsed is None else np.array(elapsed)
    times = np.array([r['time'] for r in records])
    events = np.array([r['event'] for r in records], dtype=bool)
    g = km_survival(censor_table, times, left=True)
    g0 = km_survival(censor_table, elapsed, left=True)
    common = np.ones(len(records), dtype=bool)
    for p in predictions.values():
        common &= np.isfinite(p).all(axis=1)
    common &= (g >= .05) & (g0 >= .05)
    observed = common & events
    ids = np.array([r['id'] for r in records])
    _, inv, counts = np.unique(ids, return_inverse=True, return_counts=True)
    cluster = 1/counts[inv]
    output = {}
    for name, p in predictions.items():
        if not observed.any():
            continue
        w = cluster[observed]*g0[observed]/g[observed]
        w /= w.sum()
        y = times[observed]-elapsed[observed]
        selected = p[observed]
        known60 = common & ((times-elapsed >= 60) | events)
        gt = km_survival(censor_table, np.minimum(times, elapsed+60), left=True)
        w60 = cluster[known60]*g0[known60]/np.maximum(.05, gt[known60])
        y60 = times[known60]-elapsed[known60] > 60
        output[name] = {'n': int(common.sum()), 'observed': int(observed.sum()),
                        'censored': int((common & ~events).sum()), 'events': len(set(ids[common])),
                        'median_mae': float(w@np.abs(selected[:, 0]-y)),
                        'coverage': float(w@((selected[:, 1] <= y) & (y <= selected[:, 2]))),
                        'width': float(w@(selected[:, 2]-selected[:, 1])), 'nominal_coverage': .5,
                        'survive60_brier': float(np.average((p[known60, 3]-y60)**2, weights=w60)) if len(w60) else None}
    return output


def evaluate_fold(timeline, data, fitted, cutoff, end, step):
    test = np.array([cutoff <= a < end for a in data['anchors']], dtype=bool)
    result = {'from': iso(cutoff), 'to': iso(end), 'onset': {}, 'count': {},
              'duration': {}, 'remaining': {}, 'fit_errors': fitted['errors']}
    for task, names in (('onset', ONSET), ('count', COUNT)):
        mask = test & data['inactive'] if task == 'onset' else test.copy()
        if not mask.any() or any(n not in fitted[task] for n in names):
            continue
        preds = {name: (analog_predict(fitted[task][name], data['slots'][mask], task)
                        if name == 'historical' else regression_predict(fitted[task][name], data['x'][mask]))
                 for name in names}
        common = np.ones(int(mask.sum()), dtype=bool)
        for p in preds.values():
            common &= np.isfinite(p).all(axis=1)
        if common.any():
            y = data[task][mask][common]
            days = data['days'][mask][common]
            for name, p in preds.items():
                result[task][name] = (onset_metrics(y, p[common], days) if task == 'onset'
                                      else count_metrics(y, p[common], days))
    if any(n not in fitted['duration'] for n in SURVIVAL):
        return result
    train_records = timeline.survival_data(cutoff)
    censor = fit_km([r['time'] for r in train_records], [not r['event'] for r in train_records])
    followup = timeline.survival_data(end)
    new = [r for r in followup if cutoff <= r['start'] < end]
    if new:
        preds = {n: survival_predict(fitted['duration'][n], [r['start'] for r in new]) for n in SURVIVAL}
        result['duration'] = survival_metrics(new, preds, censor)
    residuals, elapsed = [], []
    for r in followup:
        finish = r['start']+timedelta(minutes=r['time'])
        anchor_ts = math.ceil(max(cutoff.timestamp(), r['start'].timestamp()+1)/step)*step
        while anchor_ts < min(end, finish).timestamp():
            residuals.append(r)
            elapsed.append((anchor_ts-r['start'].timestamp())/60)
            anchor_ts += step
    if residuals:
        preds = {n: survival_predict(fitted['duration'][n], [r['start'] for r in residuals], elapsed) for n in SURVIVAL}
        result['remaining'] = survival_metrics(residuals, preds, censor, elapsed)
    return result


def choose_models(folds):
    selected = dict.fromkeys(('onset', 'count', 'duration'), 'historical')
    decisions = {}
    for task, names in (('onset', ONSET), ('count', COUNT), ('duration', SURVIVAL)):
        scores = {}
        for name in names[1:]:
            comparable = [f for f in folds if name in f[task] and 'historical' in f[task]
                          and (task != 'duration' or name in f['remaining'] and 'historical' in f['remaining'])]
            def loss(f, n):
                if task == 'onset':
                    return float(np.mean(f[task][n]['brier']))
                if task == 'count':
                    return f[task][n]['mae']
                return (f['duration'][n]['median_mae']+f['remaining'][n]['median_mae'])/2
            wins = sum(loss(f, name) < .98*loss(f, 'historical') for f in comparable)
            gain = float(np.mean([loss(f, 'historical')-loss(f, name) for f in comparable])) if comparable else 0.
            quality = True
            for f in comparable:
                if task == 'onset':
                    quality &= np.mean(f[task][name]['log_loss']) <= np.mean(f[task]['historical']['log_loss'])
                else:
                    for key in (('duration', 'remaining') if task == 'duration' else ('count',)):
                        quality &= abs(f[key][name]['coverage']-f[key][name]['nominal_coverage']) <= abs(f[key]['historical']['coverage']-f[key]['historical']['nominal_coverage'])+.05
            accepted = len(comparable) >= 3 and wins >= math.ceil(.75*len(comparable)) and gain > 0 and quality
            scores[name] = {'folds': len(comparable), 'wins_over_2_percent': wins,
                            'mean_loss_gain': gain, 'range_or_logloss_guard': bool(quality), 'accepted': bool(accepted)}
        accepted = [n for n, s in scores.items() if s['accepted']]
        if accepted:
            selected[task] = max(accepted, key=lambda n: scores[n]['mean_loss_gain'])
        decisions[task] = scores
    return selected, decisions


def run_evaluation(settings, timeline=None, data=None):
    timeline = timeline or load_timeline(settings)
    result = {'at': iso(utcnow()), 'validated': False, 'step_seconds': settings.interval,
              'method': '21-day initial window; expanding weekly folds; purged labels; nested chronological L2 tuning',
              'horizons_minutes': [15, 30, 60], 'folds': [],
              'limitations': ['Retrospective corrected history; original publication delays cannot be reconstructed.',
                              'Dependent slices weighted by day (regression) or alarm (remaining time). Folds, not slices, determine promotion.',
                              'Survival: training-only censor KM and normalized IPCW, positivity >=0.05; conditional independent censoring is an assumption.',
                              'No claim of prospective accuracy; model choice reuses retrospective folds.']}
    if timeline:
        data = data if data is not None else timeline.dataset(settings.interval)
        cutoff = midnight(timeline.coverage.astimezone(KYIV).date()+timedelta(days=21))
        last_day = midnight(timeline.observed_until.astimezone(KYIV).date())
        while cutoff < last_day:
            end = min(midnight(cutoff.astimezone(KYIV).date()+timedelta(days=7)), last_day)
            log.info('Evaluation fold %s -> %s', iso(cutoff), iso(end))
            fitted = fit_candidates(timeline, data, cutoff)
            result['folds'].append(evaluate_fold(timeline, data, fitted, cutoff, end, settings.interval))
            cutoff = end
    result['selected'], result['decisions'] = choose_models(result['folds'])
    result['validated'] = len(result['folds']) >= 3 and all(any(f[t] for f in result['folds']) for t in ('onset', 'count', 'duration', 'remaining'))
    if not result['validated']:
        result['reason'] = 'Недостатньо придатних часових відрізків; резервні історичні моделі залишаються основними'
        result['selected'] = dict.fromkeys(('onset', 'count', 'duration'), 'historical')
    return result


def evaluate(settings):
    from filelock import FileLock
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.data_dir/'training.lock'), timeout=0):
        result = run_evaluation(settings)
        with connect(settings) as db:
            setmeta(db, 'evaluation', json.dumps(result, ensure_ascii=False, allow_nan=False))
        return result
