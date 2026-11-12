"""Export the actual artifact's evaluation, never fabricated demonstration metrics."""
import json
from pathlib import Path
from statistics import mean, pstdev

from .training import read_artifact


def export_report(settings, directory):
    artifact = read_artifact(settings)
    if not artifact:
        raise ValueError('Спочатку виконайте train: сумісного артефакту немає')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    report = artifact['evaluation']
    (directory/'model-comparison.json').write_text(
        json.dumps({'model_id': artifact['id'], 'training': artifact['fitted']['training'],
                    'trained_at': artifact['trained_at'], 'elapsed_seconds': artifact['elapsed_seconds'],
                    'cpu_seconds': artifact.get('cpu_seconds'), 'dataset_array_bytes': artifact.get('dataset_array_bytes'),
                    'evaluation': report}, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    lines = ['# Фактичне порівняння моделей', '',
             f"Артефакт `{artifact['id']}`; версія `{artifact['version']}`.", '',
             f"Крок {report['step_seconds']} с; {len(report['folds'])} послідовних відрізків. "
             f"Навчання останньої версії: {artifact['elapsed_seconds']:.2f} с локально.", '',
             'Показано середні метрики за відрізками; ± — стандартне відхилення між відрізками, '
             'а не довірчий інтервал. Менші Brier, log loss та MAE кращі. '
             'Моделі всередині кожного завдання оцінюються на однакових придатних точках.', '']

    def values(task, name):
        return [f[task][name] for f in report['folds'] if name in f[task]]

    def scalar(rows, key):
        a = [r[key] for r in rows]
        return f'{mean(a):.3f} ± {pstdev(a):.3f}' if a else 'немає оцінки'

    lines += ['## Початок тривоги', '', '| Модель | n зрізів | Brier 15 / 30 / 60 | Log loss 15 / 30 / 60 |',
              '| --- | ---: | --- | --- |']
    for name in ('historical', 'logistic'):
        rows = values('onset', name)
        triples = []
        for key in ('brier', 'log_loss'):
            triples.append(' / '.join(f'{mean(r[key][i] for r in rows):.4f}' for i in range(3)) if rows else 'немає оцінки')
        lines.append(f"| {name} | {sum(r['n'] for r in rows)} | {triples[0]} | {triples[1]} |")
    for task, title, names, key, target in (
        ('count', 'Нові початки до опівночі', ('historical', 'poisson', 'negative_binomial'), 'mae', 80),
        ('duration', 'Загальна тривалість, хв', ('historical', 'weibull_aft', 'lognormal_aft'), 'median_mae', 50),
        ('remaining', 'Умовний залишковий час, хв', ('historical', 'weibull_aft', 'lognormal_aft'), 'median_mae', 50)):
        lines += ['', f'## {title}', '', f'Цільове покриття: {target}%.', '',
                  '| Модель | n зрізів/подій | MAE ± σ | Покриття ± σ | Ширина ± σ |',
                  '| --- | ---: | --- | --- | --- |']
        for name in names:
            rows = values(task, name)
            lines.append(f"| {name} | {sum(r['n'] for r in rows)} | {scalar(rows, key)} | {scalar(rows, 'coverage')} | {scalar(rows, 'width')} |")
    lines += ['', '## Реально використовуються', '', '| Показник | Модель |', '| --- | --- |']
    for task, name in artifact['selected'].items():
        lines.append(f'| {task} | {name} |')
    lines += ['', 'Історична модель початку — згладжені частоти за тим самим місцевим часом; '
              'кількості — середнє та історичні квантілі; тривалості — Kaplan–Meier з правим цензуруванням. '
              'Ці моделі не називаються регресіями.', '',
              'Підвищення: щонайменше 3 відрізки, виграш >2% у ≥75% відрізків, додатний середній виграш '
              'та захист за log loss або відхиленням покриття від номіналу. '
              'Тривалість обирається за сукупною перевіркою загального й залишкового часу.', '',
              'Калібрування всіх горизонтів, метрики кожного відрізка, помилки навчання, '
              'число спостережених/цензурованих результатів, Brier для продовження на 60 хв '
              'та рішення щодо вибору — у [повному JSON](model-comparison.json).', '', '## Межі висновків', '']
    lines += ['- '+s for s in report['limitations']]
    lines += ['', 'Для виживаності використано нормовані IPCW-оцінки з розподілом цензурування лише з навчання. '
              'Спостереження без визначених квантилів у всіх моделей або з G<0.05 виключені спільно; '
              'результати не описують непідтриманий хвіст розподілу. '
              'Кількість зрізів залишкового часу не є кількістю незалежних тривог.', '',
              'Цей звіт не підтверджує роботу в обліковому записі PythonAnywhere.']
    path = directory/'model-comparison.md'
    path.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return path
