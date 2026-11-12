import hashlib
import json
import math
import sqlite3
from datetime import date, timedelta

import numpy as np
import pytest
from filelock import FileLock

from alarmtrend import create_app
from alarmtrend.config import Settings
from alarmtrend.data import midnight
from alarmtrend.db import connect, iso, setmeta
from alarmtrend.estimators import (fit_analogs, fit_regression, fit_survival, ordered_probabilities,
                                  regression_predict, survival_predict)
from alarmtrend.evaluation import (choose_models, fit_candidates, onset_metrics,
                                  run_evaluation, survival_metrics)
from alarmtrend.learning_data import Timeline, day_weights, known_rows
from alarmtrend.model import snapshot
from alarmtrend.sync import save_forecast
from alarmtrend.training import (VERSION, atomic_json, read_artifact, request_training,
                                signature, train)


@pytest.fixture
def timeline():
    first = midnight(date(2026, 9, 1))
    rows = []
    complete = set()
    for day in range(52):
        date_i = date(2026, 9, 1)+timedelta(days=day)
        complete.add(date_i.isoformat())
        for hour in (3, 10, 19):
            start = midnight(date_i)+timedelta(hours=hour, minutes=(day*7)%30)
            rows.append({'start': iso(start), 'end': iso(start+timedelta(minutes=15+day%9*10)),
                         'valid': 1, 'origin': 'source'})
    return Timeline(rows, first, complete, midnight(date(2026, 10, 23)))


def test_horizon_projection_preserves_identities():
    p = ordered_probabilities([[.7, .1, .5], [-1, .5, 2]])
    assert np.allclose(p[0], [.4, .4, .5])  # sorting would incorrectly yield .1,.5,.7
    assert np.all((p[:, :-1] <= p[:, 1:]) & (p[:, :-1] >= 0))


def test_features_do_not_use_future_ends_or_starts(timeline):
    anchor = midnight(date(2026, 9, 18))+timedelta(hours=3, minutes=40)
    truncated = Timeline(known_rows(timeline.rows, anchor), timeline.coverage,
                         timeline.complete, anchor)
    assert np.allclose(timeline.features([anchor]), truncated.features([anchor]))


def test_gap_excludes_labels_and_lookback(timeline):
    timeline.complete.remove('2026-09-12')
    data = timeline.dataset(300)
    assert '2026-09-12' not in data['days']
    assert '2026-09-13' not in data['days']
    assert all((a+timedelta(minutes=60)).timestamp() <= end
               for a, end in zip(data['anchors'], data['label_end']))


@pytest.mark.parametrize('day,hours', [(date(2026, 10, 25), 25), (date(2026, 3, 29), 23)])
def test_utc_grid_and_count_targets_respect_dst(day, hours):
    coverage = midnight(day-timedelta(days=1))
    complete = {(day+timedelta(days=i)).isoformat() for i in (-1, 0, 1)}
    t = Timeline([], coverage, complete, midnight(day+timedelta(days=2)))
    d = t.dataset(300)
    assert np.sum(d['days'] == day.isoformat()) == hours*12
    indices = np.where(d['days'] == day.isoformat())[0]
    assert d['x'][indices[0], 7] == hours
    assert np.all(d['count'] == 0)


def test_day_weights_do_not_count_dependent_slices_as_independent():
    w = day_weights(np.array(['a', 'a', 'a', 'b']))
    assert w[:3].sum() == pytest.approx(.5)
    assert w[3] == pytest.approx(.5)


def test_historical_analogs_do_not_double_weight_dst_repeat():
    days = np.array(['repeat', 'repeat']+[f'day{i}' for i in range(13)])
    data = {'days': days, 'slots': np.zeros(15), 'inactive': np.ones(15, dtype=bool),
            'onset': np.array([[1]*3, [0]*3]+[[0]*3]*13), 'count': np.array([0, 10]+[2]*13)}
    group = fit_analogs(data, np.ones(15, dtype=bool))['groups']['0']
    assert group['n'] == group['count_n'] == 14
    assert group['p'] == pytest.approx([1/15]*3)
    assert group['count'][0] == pytest.approx(31/14)


def test_evaluation_with_only_unknown_periods_does_not_invent_zero_days(tmp_path):
    t = Timeline([], midnight(date(2026, 9, 1)), set(), midnight(date(2026, 10, 23)))
    report = run_evaluation(Settings(tmp_path), t)
    assert not report['validated']
    assert all(not f['onset'] and not f['count'] for f in report['folds'])


def test_migration_preserves_legacy_forecasts_and_rejects_newer_schema(tmp_path):
    cfg = Settings(tmp_path)
    with sqlite3.connect(cfg.database) as db:
        db.execute('CREATE TABLE forecasts(id INTEGER PRIMARY KEY, at TEXT, model TEXT, payload TEXT)')
        db.execute("INSERT INTO forecasts VALUES (1,'old-time','old-model','{}')")
    with connect(cfg) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('SELECT model FROM forecasts').fetchone()[0] == 'old-model'
        db.execute('PRAGMA user_version=3')
    with pytest.raises(ValueError, match='Unsupported database schema'):
        with connect(cfg):
            pass


def test_training_labels_are_purged_and_scaling_is_training_only(timeline):
    data = timeline.dataset(300)
    cutoff = midnight(date(2026, 9, 25))
    first = fit_candidates(timeline, data, cutoff)
    altered = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in data.items()}
    future = altered['label_end'] > cutoff.timestamp()
    altered['x'][future] = 1e6
    altered['count'][future] = 9999
    altered['onset'][future] = 1-altered['onset'][future]
    second = fit_candidates(timeline, altered, cutoff)
    assert first == second
    assert not first['errors']


@pytest.mark.parametrize('kind', ['poisson', 'negative_binomial'])
def test_count_regressions_allow_zero_and_return_discrete_ranges(timeline, kind):
    d = timeline.dataset(300)
    spec = fit_regression(kind, d['x'], d['count'], d['days'])
    p = regression_predict(spec, d['x'][-1:])[0]
    assert p[0] >= 0 and 0 <= p[1] <= p[2]
    assert int(p[1]) == p[1] and int(p[2]) == p[2]
    assert p[0] < .5  # near midnight; zero rounded starts is valid


def test_survival_training_censors_at_cutoff(timeline):
    anchor = midnight(date(2026, 9, 18))+timedelta(hours=3, minutes=40)
    records = timeline.survival_data(anchor)
    truncated = Timeline(known_rows(timeline.rows, anchor), timeline.coverage, timeline.complete, anchor)
    assert records == truncated.survival_data(anchor)
    assert any(not r['event'] for r in records)
    assert all(r['start']+timedelta(minutes=r['time']) <= anchor for r in records)


@pytest.mark.parametrize('kind', ['weibull_aft', 'lognormal_aft'])
def test_aft_fits_right_censored_likelihood(timeline, kind):
    records = timeline.survival_data(timeline.observed_until)
    censored = [{**r, 'time': min(r['time'], 50), 'event': r['time'] <= 50} for r in records]
    fitted = fit_survival(kind, censored)
    assert fitted['events'] < fitted['n']
    values = survival_predict(fitted, [records[0]['start']], [20])[0]
    assert np.isfinite(values).all()
    assert 0 <= values[1] <= values[0] <= values[2]
    assert 0 <= values[3] <= 1


def test_weibull_exponential_residual_is_conditional_not_subtracted():
    spec = {'kind': 'weibull_aft', 'columns': [], 'mean': [], 'scale': [],
            'coef': [math.log(60)], 'shape': 1, 'n': 100}
    starts = [midnight(date(2026, 9, 1))]
    fresh = survival_predict(spec, starts, [0])[0]
    ongoing = survival_predict(spec, starts, [120])[0]
    assert ongoing[0] == pytest.approx(60*math.log(2))
    assert ongoing[0] == pytest.approx(fresh[0])
    assert ongoing[3] == pytest.approx(math.exp(-1))


def test_censored_case_is_not_scored_as_observed_end():
    start = midnight(date(2026, 9, 1))
    records = [{'id': 'a', 'start': start, 'time': 40, 'event': True},
               {'id': 'b', 'start': start, 'time': 10, 'event': False}]
    preds = {'historical': np.array([[40, 20, 60, .2], [500, 400, 600, .9]])}
    m = survival_metrics(records, preds, [])['historical']
    assert m['median_mae'] == 0 and m['coverage'] == 1
    assert m['censored'] == 1 and m['observed'] == 1


def test_calibration_metrics_are_per_horizon():
    m = onset_metrics(np.array([[0, 0, 1], [0, 1, 1]]),
                      np.array([[.1, .2, .7], [.2, .6, .8]]), np.array(['a', 'b']))
    assert len(m['brier']) == len(m['log_loss']) == len(m['calibration']) == 3
    assert all(sum(b['n'] for b in bins) == 2 for bins in m['calibration'])


def test_one_successful_fold_never_promotes_complex_model():
    f = {'onset': {'historical': {'brier': [.3]*3, 'log_loss': [.7]*3},
                   'logistic': {'brier': [.1]*3, 'log_loss': [.4]*3}},
         'count': {}, 'duration': {}, 'remaining': {}}
    assert choose_models([f])[0]['onset'] == 'historical'
    assert choose_models([f, f, f])[0]['onset'] == 'logistic'


def test_all_candidates_share_test_points_and_expanding_folds(timeline, tmp_path):
    cfg = Settings(tmp_path)
    report = run_evaluation(cfg, timeline)
    assert report['validated'] and len(report['folds']) >= 3
    for fold in report['folds']:
        assert not fold['fit_errors']
        for task in ('onset', 'count', 'duration', 'remaining'):
            assert len({m['n'] for m in fold[task].values()}) == 1


def seed_db(cfg, timeline):
    with connect(cfg) as db:
        setmeta(db, 'coverage_start', iso(timeline.coverage))
        setmeta(db, 'last_success', iso(timeline.observed_until))
        db.executemany('INSERT INTO intervals VALUES (?,?,?,?)',
                       [(r['start'], r['end'], r['origin'], r['valid']) for r in timeline.rows])
        db.executemany('INSERT INTO daily VALUES (?,3,180,1)', [(day,) for day in sorted(timeline.complete)])
        request_training(db, cfg, timeline.observed_until)


def test_training_artifact_roundtrip_revision_invalidation_and_atomic_reserve(timeline, tmp_path, monkeypatch):
    cfg = Settings(tmp_path)
    seed_db(cfg, timeline)
    monkeypatch.setattr('alarmtrend.training.utcnow', lambda: timeline.observed_until)
    assert train(cfg)['ok']
    with connect(cfg) as db:
        model = read_artifact(cfg, db, timeline.observed_until)
        assert model and model['fitted']['training']['censored'] == 0
    root = cfg.data_dir/'models'
    (root/'previous.json').write_bytes((root/'current.json').read_bytes())
    (root/'current.json').write_text('{broken', encoding='utf-8')
    assert read_artifact(cfg)['id'] == model['id']
    with connect(cfg) as db:
        db.execute('UPDATE intervals SET end=? WHERE start=?',
                   (iso(timeline.coverage+timedelta(hours=5)), timeline.rows[0]['start']))
        assert read_artifact(cfg, db, timeline.observed_until) is None
    with FileLock(str(cfg.data_dir/'training.lock')):
        assert train(cfg)['skipped']


def test_failed_atomic_write_preserves_old_file(tmp_path):
    file = tmp_path/'model.json'
    atomic_json(file, {'valid': True})
    with pytest.raises(ValueError):
        atomic_json(file, {'invalid': float('nan')})
    assert json.loads(file.read_text()) == {'valid': True}
    assert not list(tmp_path.glob('*.tmp'))


def test_forecast_archive_is_append_only_and_http_never_computes(tmp_path, monkeypatch):
    cfg = Settings(tmp_path)
    at = midnight(date(2026, 10, 22))
    with connect(cfg) as db:
        save_forecast(db, cfg, at)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE forecasts SET model='changed'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM forecasts')
    monkeypatch.setattr('alarmtrend.model.snapshot', lambda *_: pytest.fail('HTTP must only read'))
    client = create_app(cfg).test_client()
    assert client.get('/').status_code == 200
    assert client.get('/api/status').json['model_id'] is None
    assert client.get('/calculation').status_code == 200


def test_manual_http_queues_forecast_and_worker_recalculates(tmp_path, monkeypatch):
    from alarmtrend.sync import process_forecast_request
    cfg = Settings(tmp_path, secret_key='test', admin_password_hash='configured', secure_cookie=False)
    app = create_app(cfg)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(admin=True, csrf='token')
    original = __import__('alarmtrend.model', fromlist=['snapshot']).snapshot
    monkeypatch.setattr('alarmtrend.model.snapshot', lambda *_: pytest.fail('No HTTP inference'))
    response = client.post('/manual', data={'csrf': 'token', 'action': 'save',
                                           'stamp': '2026-09-01T10:00:00+03:00', 'kind': 'start'})
    assert response.status_code == 302
    pending = client.get('/api/status').json
    assert pending['forecast_pending'] and pending['status'] == 'unknown'
    monkeypatch.setattr('alarmtrend.model.snapshot', original)
    monkeypatch.setattr('alarmtrend.source.fetch', lambda *_: pytest.fail('Recalculation must not fetch'))
    assert process_forecast_request(cfg)
    assert not client.get('/api/status').json['forecast_pending']
    assert client.get('/api/status').json['status'] == 'active'
    assert not process_forecast_request(cfg)


def test_unknown_status_suppresses_onset_probability(timeline, tmp_path):
    cfg = Settings(tmp_path)
    seed_db(cfg, timeline)
    with connect(cfg) as db:
        p = snapshot(db, cfg, timeline.observed_until)
        assert p['status'] == 'unknown'
        assert p['probabilities'] is None and p['remaining'] is None
        assert p['components']['duration']['fallback']


@pytest.mark.parametrize('active,count_model,survival_model', [
    (False, 'poisson', 'weibull_aft'), (True, 'negative_binomial', 'lognormal_aft')])
def test_selected_regressions_are_used_by_issued_forecasts(timeline, tmp_path, monkeypatch,
                                                        active, count_model, survival_model):
    cfg = Settings(tmp_path)
    seed_db(cfg, timeline)
    at = timeline.observed_until+timedelta(hours=10)
    data = timeline.dataset(300)
    fitted = fit_candidates(timeline, data, timeline.observed_until)
    artifact = {'id': 'test-selected', 'version': VERSION, 'trained_at': iso(timeline.observed_until),
                'fitted': fitted, 'selected': {'onset': 'logistic', 'count': count_model, 'duration': survival_model}}
    monkeypatch.setattr('alarmtrend.inference.read_artifact', lambda *_: artifact)
    with connect(cfg) as db:
        setmeta(db, 'last_success', iso(at))
        stamp = at-timedelta(minutes=50) if active else timeline.observed_until-timedelta(minutes=1)
        kind = 'start' if active else 'end'
        db.execute("INSERT INTO events(stamp,kind,origin) VALUES (?,?,'source')", (iso(stamp), kind))
        if active:
            db.execute("INSERT INTO intervals VALUES (?,NULL,'source',1)", (iso(stamp),))
        p = save_forecast(db, cfg, at)
        assert p['model_id'] == 'test-selected'
        assert p['components']['count']['model'] == count_model
        assert p['components']['duration']['model'] == survival_model
        if active:
            assert p['probabilities'] is None
            assert p['remaining']['median'] >= 0 and 0 <= p['survive60'] <= 100
        else:
            assert p['components']['onset']['model'] == 'logistic'
            assert 0 <= p['probabilities'][0] <= p['probabilities'][1] <= p['probabilities'][2] <= 100
