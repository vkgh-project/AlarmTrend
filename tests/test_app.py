import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from filelock import FileLock
from werkzeug.security import generate_password_hash

from alarmtrend import create_app
from alarmtrend.config import Settings
from alarmtrend.data import midnight, rebuild
from alarmtrend.db import connect, dt, getmeta, iso
from alarmtrend.model import predict, snapshot
from alarmtrend.source import KYIV, local_time, parse
from alarmtrend.sync import synchronize


@pytest.fixture
def cfg(tmp_path):
    return Settings(tmp_path, date(2026,9,1),secret_key='test-secret',
                    admin_password_hash=generate_password_hash('correct-password'),secure_cookie=False)


def html(events):
    rows=[]
    for stamp,kind in events:
        text='Повітряна тривога!' if kind=='start' else 'Відбій тривоги'
        rows.append(f'<tr><td>{stamp}</td><td>{text}</td><td></td></tr>')
    return '<table>'+''.join(rows)+'</table>'


def fetcher(events):
    return lambda *_: SimpleNamespace(status_code=200,text=html(events),headers={'Last-Modified':'test-date'})


def now(value='2026-09-03T12:00:00+03:00'):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


EVENTS=[('23:30 01.09.26','start'),('01:30 02.09.26','end')]


def test_source_and_idempotent_sync(cfg):
    response=fetcher(EVENTS)
    first=synchronize(cfg,response,now())
    second=synchronize(cfg,response,now()+timedelta(minutes=5))
    assert first['changed']==2 and second['changed']==0
    with connect(cfg) as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM intervals').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0]==2
        assert getmeta(db,'last_success')==iso(now()+timedelta(minutes=5))


def test_midnight_split(cfg):
    synchronize(cfg,fetcher(EVENTS),now())
    with connect(cfg) as db:
        rows=db.execute('SELECT * FROM daily ORDER BY day').fetchall()
        assert rows[0]['minutes']==30 and rows[1]['minutes']==90
        assert rows[0]['starts']==1 and rows[1]['starts']==0


def test_dst_boundaries_and_ambiguous_time():
    assert (midnight(date(2026,10,26))-midnight(date(2026,10,25))).total_seconds()==25*3600
    assert (midnight(date(2026,3,30))-midnight(date(2026,3,29))).total_seconds()==23*3600
    with pytest.raises(ValueError):
        local_time('2026-10-25T03:30:00')
    with pytest.raises(ValueError):
        local_time('2026-03-29T03:30:00')
    assert local_time('2026-10-25T03:30:00+03:00')!=local_time('2026-10-25T03:30:00+02:00')


def test_active_without_end(cfg):
    result=synchronize(cfg,fetcher([('11:00 03.09.26','start')]),now())
    assert result['forecast']['status']=='active'
    assert result['forecast']['probabilities'] is None
    with connect(cfg) as db:
        row=db.execute('SELECT * FROM intervals').fetchone()
        assert row['end'] is None and row['valid']==1


def test_manual_reconciliation_and_correction(cfg):
    with connect(cfg) as db:
        db.execute("INSERT INTO events(stamp,kind,origin) VALUES (?,'start','manual')",(iso(now('2026-09-01T23:30:42+03:00')),))
    synchronize(cfg,fetcher(EVENTS),now())
    with connect(cfg) as db:
        assert db.execute("SELECT reconciled_to FROM events WHERE origin='manual'").fetchone()[0] is not None
        assert db.execute('SELECT count(*) FROM intervals').fetchone()[0]==1
    corrected=[('23:30 01.09.26','start'),('02:00 02.09.26','end')]
    synchronize(cfg,fetcher(corrected),now())
    with connect(cfg) as db:
        assert db.execute('SELECT end FROM intervals').fetchone()[0]==iso(now('2026-09-02T02:00:00+03:00'))
        assert db.execute("SELECT count(*) FROM audit WHERE action='source_removed'").fetchone()[0]==1


def test_network_failure_and_304_recalculate(cfg):
    synchronize(cfg,fetcher(EVENTS),now())
    def broken(*_):
        raise TimeoutError('offline')
    result=synchronize(cfg,broken,now()+timedelta(minutes=5))
    assert not result['ok']
    assert result['forecast']['last_success']==iso(now())
    assert result['forecast']['at']==iso(now()+timedelta(minutes=5))
    def unchanged(settings,etag,modified):
        assert modified=='test-date'
        return SimpleNamespace(status_code=304,headers={},text='')
    result=synchronize(cfg,unchanged,now()+timedelta(minutes=10))
    assert result['ok'] and result['changed']==0
    assert result['forecast']['last_event']!=result['forecast']['last_success']


def test_lock_skips_parallel_job(cfg):
    with FileLock(str(cfg.data_dir/'sync.lock')):
        assert synchronize(cfg,lambda *_:pytest.fail('Should not fetch'),now())=={'skipped':True}


def training_rows():
    rows=[]
    for i in range(40):
        start=now('2026-09-01T12:20:00+03:00')+timedelta(days=i)
        rows.append({'start':iso(start),'end':iso(start+timedelta(minutes=90+i)), 'valid':1,'origin':'source'})
    return rows


def test_probabilities_count_and_no_future_leakage():
    rows=training_rows()
    anchor=now('2026-10-12T12:00:00+03:00')
    coverage=midnight(date(2026,9,1))
    predicted=predict(rows,coverage,anchor)
    p=predicted['probabilities']
    assert 0<=p[0]<=p[1]<=p[2]<=100
    assert predicted['count']['estimate']==1
    future={'start':iso(anchor+timedelta(minutes=5)),'end':iso(anchor+timedelta(minutes=10)),'valid':1}
    assert predict(rows+[future],coverage,anchor)==predicted
    late=predict(rows,coverage,now('2026-10-12T23:59:00+03:00'))
    assert late['count']['estimate']==0 and late['count']['low']==0


def test_remaining_is_conditional():
    rows=training_rows()
    anchor=now('2026-10-12T13:50:00+03:00')
    rows.append({'start':iso(anchor-timedelta(minutes=100)),'end':None,'valid':1})
    pred=predict(rows,midnight(date(2026,9,1)),anchor)
    assert pred['probabilities'] is None
    assert pred['remaining']['n']==29
    assert pred['remaining']['median']==15


def test_empty_web_auth_csrf_and_no_background_fetch(cfg,monkeypatch):
    monkeypatch.setattr('alarmtrend.source.fetch',lambda *_:pytest.fail('Web must never fetch source'))
    app=create_app(cfg)
    client=app.test_client()
    assert client.get('/').status_code==200
    assert client.get('/api/status').json['status']=='unknown'
    assert client.get('/manual').status_code==302
    assert client.post('/login',data={'password':'correct-password'}).status_code==400
    client.get('/login')
    with client.session_transaction() as sess:
        csrf=sess['csrf']
    assert client.post('/login',data={'csrf':csrf,'password':'correct-password'}).status_code==302
    with client.session_transaction() as sess:
        csrf=sess['csrf']
    result=client.post('/manual',data={'csrf':csrf,'action':'save','stamp':'2026-09-01T10:00:00+03:00','kind':'start'})
    assert result.status_code==302
    assert client.get('/api/status').json['manual']
    assert client.get('/details').status_code==404


def test_bad_html_preserves_source(cfg):
    synchronize(cfg,fetcher(EVENTS),now())
    result=synchronize(cfg,lambda *_:SimpleNamespace(status_code=200,text='<html>blocked</html>',headers={}),now())
    assert not result['ok']
    with connect(cfg) as db:
        assert db.execute('SELECT count(*) FROM intervals').fetchone()[0]==1


def test_missing_end_marks_incomplete(cfg):
    synchronize(cfg,fetcher([('10:00 01.09.26','start'),('12:00 01.09.26','start'),('13:00 01.09.26','end')]),now())
    with connect(cfg) as db:
        assert not db.execute("SELECT complete FROM daily WHERE day='2026-09-01'").fetchone()[0]
        assert db.execute('SELECT count(*) FROM issues').fetchone()[0]>0


def test_source_dst_ambiguity_keeps_raw_gap(cfg):
    cfg.start_date=date(2026,10,24)
    events=[('23:00 24.10.26','start'),('03:30 25.10.26','end'),
            ('05:00 25.10.26','start'),('06:00 25.10.26','end'),
            ('10:00 26.10.26','start'),('11:00 26.10.26','end')]
    result=synchronize(cfg,fetcher(events),now('2026-10-27T12:00:00+02:00'))
    assert result['ok']
    with connect(cfg) as db:
        assert db.execute('SELECT count(*) FROM source_issues').fetchone()[0]==1
        assert '03:30 25.10.26' in db.execute('SELECT raw FROM source_issues').fetchone()[0]
        assert not db.execute("SELECT complete FROM daily WHERE day='2026-10-25'").fetchone()[0]
        assert db.execute("SELECT complete FROM daily WHERE day='2026-10-26'").fetchone()[0]
        assert db.execute("SELECT count(*) FROM events WHERE stamp LIKE '%01:30:%'").fetchone()[0]==0


def test_start_date_change_forces_full_request(cfg):
    synchronize(cfg,fetcher(EVENTS),now())
    cfg.start_date=date(2026,9,2)
    def changed_config(settings,etag,modified):
        assert not etag and not modified
        return SimpleNamespace(status_code=200,text=html(EVENTS),headers={})
    assert synchronize(cfg,changed_config,now())['ok']


def test_online_backup(cfg,tmp_path,monkeypatch):
    from alarmtrend.__main__ import main
    synchronize(cfg,fetcher(EVENTS),now())
    backup=tmp_path/'backup.sqlite3'
    monkeypatch.setattr('sys.argv',['alarmtrend','backup',str(backup)])
    monkeypatch.setattr('alarmtrend.__main__.Settings.load',lambda:cfg)
    main()
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==2


def test_authenticated_manual_edit_and_cancel(cfg):
    app=create_app(cfg)
    client=app.test_client()
    with client.session_transaction() as sess:
        sess.update(admin=True,csrf='token')
    stamp='2026-09-01T10:00:00+03:00'
    client.post('/manual',data={'csrf':'token','action':'save','stamp':stamp,'kind':'start'})
    with connect(cfg) as db:
        record=db.execute("SELECT id FROM events WHERE origin='manual'").fetchone()[0]
    client.post('/manual',data={'csrf':'token','action':'save','stamp':'2026-09-01T11:00:00+03:00','kind':'start','id':record})
    with connect(cfg) as db:
        assert db.execute('SELECT enabled FROM events WHERE id=?',(record,)).fetchone()[0]==0
        new_id=db.execute('SELECT id FROM events WHERE enabled=1').fetchone()[0]
    client.post('/manual',data={'csrf':'token','action':'cancel','id':new_id})
    with connect(cfg) as db:
        assert db.execute('SELECT count(*) FROM events WHERE enabled=1').fetchone()[0]==0
