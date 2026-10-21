"""Expanding-window evaluation; deterministic, no random splits or future training rows."""
import math
from datetime import datetime, time, timedelta
from statistics import mean

from .data import midnight
from .db import connect, dt, getmeta, iso, setmeta, utcnow
from .model import history, predict
from .source import KYIV


def features(anchor):
    local=anchor.astimezone(KYIV)
    phase=2*math.pi*(local.hour+local.minute/60)/24
    return [1, math.sin(phase), math.cos(phase), int(local.weekday()>=5)]


def logistic_fit(samples, target):
    # L2 regularization, intercept unpenalized. No extra binary dependencies on the host.
    weights=[0.0]*4
    for _ in range(160):
        grad=[0.0]*4
        for x,y in samples:
            z=max(-30,min(30,sum(a*b for a,b in zip(weights,x))))
            p=1/(1+math.exp(-z))
            for j in range(4):
                grad[j]+=(p-y[target])*x[j]
        for j in range(4):
            weights[j]-=.15*(grad[j]/len(samples)+(.05*weights[j] if j else 0))
    return weights


def score(rows,key):
    return [round(mean((r[key][i]-r['truth'][i])**2 for r in rows),5) for i in range(3)]


def evaluate(settings):
    now=utcnow()
    with connect(settings) as db:
        coverage=dt(getmeta(db,'coverage_start'))
        intervals=history(db)
        complete={r[0] for r in db.execute('SELECT day FROM daily WHERE complete=1')}
    result={'at':iso(now),'method':'expanding window; 00/06/12/18 Kyiv; prior full days only',
            'horizons_minutes':[15,30,60], 'validated':False}
    if not coverage or len(complete)<21:
        result['reason']='Потрібно щонайменше 21 повна доба для навчання та тесту'
    else:
        samples, tested, count_errors, count_covered=[],[],[],[]
        first=coverage.astimezone(KYIV).date()
        last=now.astimezone(KYIV).date()
        day=first
        while day<last:
            if day.isoformat() not in complete:
                day+=timedelta(days=1)
                continue
            # Entire test day's outcomes are observed; training is frozen before this day.
            day_samples=[]
            for hour in (0,6,12,18):
                anchor=datetime.combine(day,time(hour),KYIV).astimezone(now.tzinfo)
                # Status may use events observed before this anchor. Statistical
                # analogs/durations inside predict still stop before this day.
                known=[{**r, 'end':r['end'] if r['end'] and dt(r['end'])<=anchor else None}
                       for r in intervals if dt(r['start'])<=anchor and r['valid']]
                active=any(dt(r['start'])<=anchor and (not r['end'] or anchor<dt(r['end'])) for r in intervals)
                starts=[dt(r['start']) for r in intervals if r['valid']]
                truth=[int(any(anchor<s<=anchor+timedelta(minutes=h) for s in starts)) for h in (15,30,60)]
                pred=predict(known,coverage,anchor,complete)
                count=sum(anchor<s<midnight(day+timedelta(days=1)) for s in starts)
                if pred['count']:
                    count_errors.append(abs(pred['count']['estimate']-count))
                    count_covered.append(pred['count']['low']<=count<=pred['count']['high'])
                if active:
                    continue
                x=features(anchor)
                if len(samples)>=28 and pred['probabilities']:
                    candidate=[]
                    for i in range(3):
                        w=logistic_fit(samples,i)
                        candidate.append(1/(1+math.exp(-max(-30,min(30,sum(a*b for a,b in zip(w,x)))))))
                    candidate=sorted(candidate)
                    base=[(sum(y[i] for _,y in samples)+.5)/(len(samples)+1) for i in range(3)]
                    tested.append({'truth':truth,'historical':[p/100 for p in pred['probabilities']],
                                   'baseline':base,'logistic':candidate})
                day_samples.append((x,truth))
            samples.extend(day_samples)
            day+=timedelta(days=1)
        if tested:
            result.update(validated=True,test_samples=len(tested),training_samples=len(samples),
                          brier={key:score(tested,key) for key in ('historical','baseline','logistic')},
                          count_mae=round(mean(count_errors),3) if count_errors else None,
                          count_range_coverage=round(mean(count_covered),3) if count_covered else None,
                          count_test_samples=len(count_errors))
            result['calibration_60']=[]
            for lo,hi in ((0,.2),(.2,.4),(.4,.6),(.6,.8),(.8,1.0001)):
                group=[r for r in tested if lo<=r['historical'][2]<hi]
                result['calibration_60'].append({'range':[lo,min(1,hi)],'n':len(group),
                    'predicted':round(mean(r['historical'][2] for r in group),3) if group else None,
                    'observed':round(mean(r['truth'][2] for r in group),3) if group else None})
            result['logistic_better']=mean(result['brier']['logistic'])<mean(result['brier']['historical'])
            result['decision']='Історична модель збережена; одноразового тесту недостатньо для автоматичного перемикання'
            result['limitations']='Лише 4 часові зрізи на добу; залежні спостереження, без довірчих інтервалів. Потрібен довший незалежний період.'
        else:
            result['reason']='Замало придатних неактивних аналогів для послідовного тесту'
    import json
    with connect(settings) as db:
        setmeta(db,'evaluation',json.dumps(result,ensure_ascii=False))
    return result
