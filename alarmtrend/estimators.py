"""Small regularized likelihood models. Artifacts contain JSON, never pickle."""
import math

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, gammaln, log_ndtr
from scipy.stats import lognorm, nbinom, poisson, weibull_min

from .learning_data import day_weights
from .source import KYIV


def ordered_probabilities(values):
    """Equal-weight isotonic projection (PAVA), preserving horizon identities."""
    values = np.atleast_2d(np.clip(values, 0, 1))
    out = []
    for row in values:
        blocks = []
        for p in row:
            blocks.append([float(p), 1])
            while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
                b, a = blocks.pop(), blocks.pop()
                n = a[1]+b[1]
                blocks.append([(a[0]*a[1]+b[0]*b[1])/n, n])
        out.append([p for p, n in blocks for _ in range(n)])
    return np.array(out)


def transform(x, spec):
    z = (np.asarray(x)[:, spec['columns']]-spec['mean'])/spec['scale']
    return np.column_stack([np.ones(len(z)), z])


def scaler(x, columns):
    z = x[:, columns]
    return {'columns': columns, 'mean': z.mean(axis=0).tolist(),
            'scale': np.maximum(z.std(axis=0), .1).tolist()}


def optimize(objective, initial, bounds=None, jac=False):
    fit = minimize(objective, initial, jac=jac, method='L-BFGS-B', bounds=bounds,
                   options={'maxiter': 250, 'ftol': 1e-9})
    if not fit.success or not np.all(np.isfinite(fit.x)):
        raise ValueError(f'Unstable fit: {fit.message}')
    return fit.x


def fit_regression(kind, x, y, days, penalty=.05):
    ndays = len(set(days))
    # Effective sample size is calendar days, not thousands of dependent slices.
    columns = [0, 1, 2] if kind == 'logistic' else [0, 1, 2, 3, 7]
    if kind == 'logistic' and ndays >= 45:
        columns += [3]
    if kind == 'logistic' and ndays >= 90:
        columns += [4, 5, 6]
    spec = scaler(x, columns)
    z = transform(x, spec)
    w = day_weights(days)
    p = z.shape[1]
    spec.update(kind=kind, penalty=penalty, days=ndays, observations=len(y))
    if kind == 'logistic':
        coefs = []
        for h in range(3):
            target = y[:, h]
            if target.sum() < 5 or (1-target).sum() < 5:
                raise ValueError('Too few positive or negative onset outcomes')
            def objective(beta):
                eta = z@beta
                loss = np.sum(w*(np.logaddexp(0, eta)-target*eta))
                reg = penalty*np.sum(beta[1:]**2)/2
                grad = z.T@(w*(expit(eta)-target))
                grad[1:] += penalty*beta[1:]
                return loss+reg, grad
            initial = np.zeros(p)
            initial[0] = math.log((target.mean()+.001)/(1-target.mean()+.001))
            coefs.append(optimize(objective, initial, jac=True).tolist())
        spec['coef'] = coefs
    else:
        target = np.asarray(y)
        initial = np.zeros(p+(kind == 'negative_binomial'))
        initial[0] = math.log(max(.01, target.mean()))
        if kind == 'negative_binomial':
            initial[-1] = 0
        def objective(theta):
            beta = theta[:p]
            eta = z@beta
            mu = np.exp(np.clip(eta, -20, 20))
            if kind == 'poisson':
                nll = mu-target*eta+gammaln(target+1)
            else:
                size = math.exp(theta[-1])
                logp = -np.log1p(mu/size)
                nll = -(gammaln(target+size)-gammaln(size)-gammaln(target+1)
                        +size*logp+target*(np.log(mu)-np.log(size+mu)))
            return float(w@nll+penalty*np.sum(beta[1:]**2)/2)
        bounds = [(-12, 12)]*p+([(-4, 9)] if kind == 'negative_binomial' else [])
        theta = optimize(objective, initial, bounds)
        spec['coef'] = theta[:p].tolist()
        if kind == 'negative_binomial':
            spec['size'] = math.exp(theta[-1])
    return spec


def regression_predict(spec, x):
    z = transform(x, spec)
    if spec['kind'] == 'logistic':
        return ordered_probabilities(expit(z@np.array(spec['coef']).T))
    mu = np.exp(np.clip(z@spec['coef'], -20, 20))
    if spec['kind'] == 'poisson':
        lo, hi = poisson.ppf(.1, mu), poisson.ppf(.9, mu)
    else:
        size = spec['size']
        lo, hi = nbinom.ppf(.1, size, size/(size+mu)), nbinom.ppf(.9, size, size/(size+mu))
    return np.column_stack([mu, lo, hi])


def tune_logistic(x, y, days, label_end, anchors):
    unique = sorted(set(days))
    if len(unique) < 14:
        raise ValueError('Need 14 training days')
    split = unique[max(7, len(unique)*3//4)]
    valid = days >= split
    boundary = min(a.timestamp() for a, keep in zip(anchors, valid) if keep)
    train = (days < split) & (label_end <= boundary)
    losses = []
    # Scaling and feature selection are refitted inside the inner time split.
    for penalty in (.01, .1, 1.):
        spec = fit_regression('logistic', x[train], y[train], days[train], penalty)
        p = regression_predict(spec, x[valid])
        losses.append((float(np.mean((p-y[valid])**2)), penalty))
    penalty = min(losses)[1]
    return fit_regression('logistic', x, y, days, penalty)


def fit_analogs(data, mask):
    # Same local wall-clock time as the existing historical baseline. On DST
    # repeats both observed UTC slices remain in their calendar-day cluster.
    groups = {}
    for slot in np.unique(data['slots'][mask]):
        m = mask & (data['slots'] == slot)
        onset = data['onset'][m & data['inactive']]
        counts = data['count'][m]
        onset_days = data['days'][m & data['inactive']]
        count_days = data['days'][m]
        # The repeated DST clock hour must not give one calendar day twice
        # the weight of an ordinary day in the historical reserve.
        onset = np.array([onset[onset_days == day].mean(axis=0) for day in np.unique(onset_days)]).reshape(-1, 3)
        counts = np.array([counts[count_days == day].mean() for day in np.unique(count_days)])
        groups[str(int(slot))] = {
            'p': ((onset.sum(axis=0)+.5)/(len(onset)+1)).tolist() if len(onset) >= 14 else None,
            'count': [float(counts.mean()), float(np.floor(np.quantile(counts, .1, method='lower'))),
                      float(np.ceil(np.quantile(counts, .9, method='higher')))] if len(counts) >= 14 else None,
            'n': len(onset), 'count_n': len(counts)}
    return {'kind': 'historical', 'groups': groups}


def analog_predict(spec, slots, task):
    result = []
    for slot in slots:
        group = spec['groups'].get(str(int(slot)), {})
        val = group.get('p' if task == 'onset' else 'count')
        result.append(val if val is not None else [np.nan]*3)
    return np.array(result)


def fit_km(times, events):
    times, events = np.asarray(times), np.asarray(events, dtype=bool)
    out, survival = [], 1.
    for t in np.unique(times):
        risk = int(np.sum(times >= t))
        deaths = int(np.sum((times == t) & events))
        survival *= 1-deaths/risk
        out.append([float(t), float(survival), risk, deaths])
    return out


def km_survival(table, times, left=False):
    times = np.asarray(times)
    if not table:
        return np.ones_like(times, dtype=float)
    arr = np.array(table)
    i = np.searchsorted(arr[:, 0], times, side='left' if left else 'right')
    return np.r_[1., arr[:, 1]][i]


def fit_survival(kind, records):
    if len(records) < 10 or sum(r['event'] for r in records) < 10:
        raise ValueError('Need 10 observed durations')
    t = np.array([r['time'] for r in records])
    e = np.array([r['event'] for r in records], dtype=bool)
    if kind == 'historical':
        return {'kind': kind, 'table': fit_km(t, e), 'n': len(t), 'events': int(e.sum())}
    # AFT scale is exp(beta*x), shape/sigma shared. Covariates are known at onset.
    x = survival_features([r['start'] for r in records])
    columns = [0, 1, 2] if e.sum() >= 60 else []
    spec = scaler(x, columns)
    z = transform(x, spec)
    p = z.shape[1]
    initial = np.r_[np.log(np.median(t)), np.zeros(p-1), 0.]
    def objective(theta):
        logscale = z@theta[:p]
        shape = np.exp(theta[-1])
        if kind == 'weibull_aft':
            power = np.exp(np.clip(shape*(np.log(t)-logscale), -50, 50))
            logsf = -power
            logpdf = np.log(shape)-logscale+(shape-1)*(np.log(t)-logscale)-power
        else:
            residual = (np.log(t)-logscale)/shape
            logsf = log_ndtr(-residual)
            logpdf = -np.log(t)-np.log(shape)-.5*np.log(2*np.pi)-.5*residual**2
        return float(-np.mean(np.where(e, logpdf, logsf))+.01*np.sum(theta[1:p]**2))
    theta = optimize(objective, initial, [(math.log(.1), math.log(10000))]+[(-4, 4)]*(p-1)+[(-2, 2)])
    if abs(theta[-1]) > 1.99:
        raise ValueError('Survival shape at stability boundary')
    spec.update(kind=kind, coef=theta[:p].tolist(), shape=math.exp(theta[-1]),
                n=len(t), events=int(e.sum()))
    return spec


def survival_features(starts):
    vals = []
    for start in starts:
        a = start.astimezone(KYIV)
        phase = 2*np.pi*(a.hour+a.minute/60)/24
        vals.append([np.sin(phase), np.cos(phase), int(a.weekday() >= 5)])
    return np.array(vals).reshape(-1, 3)


def survival_predict(spec, starts, elapsed=None):
    """Columns: residual median, Q25, Q75, conditional S(t+60)/S(t)."""
    t = np.zeros(len(starts)) if elapsed is None else np.asarray(elapsed, dtype=float)
    if spec['kind'] == 'historical':
        table = np.array(spec['table'])
        st = km_survival(spec['table'], t)
        s60 = km_survival(spec['table'], t+60)
        result = []
        for elapsed_i, survival_i, after60 in zip(t, st, s60):
            row = []
            risk = sum(int(r[3]) for r in table if r[0] > elapsed_i)
            for q in (.5, .25, .75):
                indices = np.where((table[:, 0] > elapsed_i) & (table[:, 1] <= survival_i*(1-q)))[0]
                row.append(float(table[indices[0], 0]-elapsed_i) if len(indices) and risk >= 10 else np.nan)
            # No extrapolation beyond the observed tail of a censored KM curve.
            prob = after60/survival_i if survival_i > 0 and (elapsed_i+60 <= table[-1, 0] or table[-1, 1] == 0) and risk >= 10 else np.nan
            result.append(row+[prob])
        return np.array(result)
    scale = np.exp(transform(survival_features(starts), spec)@spec['coef'])
    dist = weibull_min(spec['shape'], scale=scale) if spec['kind'] == 'weibull_aft' else lognorm(spec['shape'], scale=scale)
    logst = dist.logsf(t)
    values = [dist.isf(np.exp(logst)*q)-t for q in (.5, .75, .25)]
    p60 = np.exp(np.minimum(0, dist.logsf(t+60)-logst))
    out = np.column_stack(values+[p60])
    out[~np.isfinite(out).all(axis=1) | (logst < math.log(.01))] = np.nan
    out[:, :3] = np.maximum(0, out[:, :3])
    return out
