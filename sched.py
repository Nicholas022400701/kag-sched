"""Generic cron scheduler for Kaggle kernels. One polling round per run, then exit.
Env: KAGGLE_API_TOKEN (used by the kaggle client), KAGGLE_USER (kernel owner), SRC_DIR (checkout that holds orch/plan.json).
Reads  <SRC_DIR>/orch/plan.json (job list, owners, limits) and <SRC_DIR>/orch/state.json.
Writes <SRC_DIR>/orch/state.json, fetched outputs to <resultsDir>/raw/<slug>/, derived resume kernels to orch/kernels/.
Log lines carry only kernel slugs, statuses, counts and hours. Everything printed passes a masker that hides the owner name and the token.
Exit codes: 0 normal (API trouble is logged and treated as unknown), 2 missing configuration."""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
fmt = '%Y-%m-%dT%H:%M:%SZ'
termStates = ('COMPLETE', 'ERROR', 'CANCEL_ACKNOWLEDGED')
busyStates = ('RUNNING', 'QUEUED', 'CANCEL_REQUESTED')
textExt = ('.json', '.md', '.txt', '.csv', '.log')
imgExt = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg')
class Mask:
    def __init__(self, raw, words):
        self.raw = raw
        words = [w for w in words if w]
        self.pat = re.compile('|'.join(re.escape(w) for w in words), re.I) if words else None
    def write(self, s):
        self.raw.write(self.pat.sub('<u>', s) if self.pat else s)
    def flush(self):
        self.raw.flush()
    def __getattr__(self, n):
        return getattr(self.raw, n)
def now():
    return time.strftime(fmt, time.gmtime())
def parseT(s):
    return dt.datetime.strptime(s, fmt)
def hoursBetween(a, b):
    return round((parseT(b) - parseT(a)).total_seconds() / 3600, 3)
def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)
def readJson(p, default=None):
    if not os.path.isfile(p):
        return default
    with open(p) as f:
        return json.load(f)
def writeJson(p, d):
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    with open(p, 'w') as f:
        json.dump(d, f, indent=1, sort_keys=True)
        f.write('\n')
def short(e, n=160):
    return re.sub(r'\s+', ' ', str(e))[:n]
def dirHash(d):
    h = hashlib.sha1()
    for p in sorted(glob.glob(os.path.join(d, '**', '*'), recursive=True)):
        if os.path.isfile(p):
            h.update(os.path.relpath(p, d).encode())
            with open(p, 'rb') as f:
                h.update(f.read())
    return h.hexdigest()[:12]
def api():
    from kaggle.api.kaggle_api_extended import KaggleApi
    k = KaggleApi()
    k.authenticate()
    return k
def statusOf(k, ref):
    r = k.kernels_status(ref)
    return str(r.status).split('.')[-1].upper(), short(getattr(r, 'failure_message', None) or '')
def quotaOf(k):
    q = k.quota_view().gpu_quota
    def h(x):
        return x.total_seconds() / 3600 if hasattr(x, 'total_seconds') else float(x) / 3600
    used, allowed = h(q.time_used), h(q.total_time_allowed)
    return {'usedH': round(used, 3), 'remainH': round(allowed - used, 3), 'allowedH': round(allowed, 2)}
def listMine(k, prefix):
    out = {}
    for kn in k.kernels_list(mine=True, page_size=100) or []:
        ref = str(getattr(kn, 'ref', '') or '')
        if '/' not in ref or not ref.split('/')[1].startswith(prefix):
            continue
        lr = getattr(kn, 'last_run_time', None)
        out[ref.split('/')[1]] = {'lastRunTime': lr.replace(tzinfo=None).strftime(fmt) if lr else None, 'gpu': bool(getattr(kn, 'enable_gpu', False))}
    return out
def specsOf(plan, jobs):
    out = {}
    for j in plan.get('jobs', []):
        out[j['id']] = dict(j)
    for sid, st in jobs.items():
        if st.get('spec') and sid not in out:
            out[sid] = dict(st['spec'])
    return out
def kernelCheck(src, user, spec, forbidden, prefix):
    if not spec['id'].startswith(prefix):
        return 'id must start with ' + prefix, None
    kd = os.path.join(src, spec.get('kernelDir', ''))
    m = readJson(os.path.join(kd, 'kernel-metadata.json'))
    if not m:
        return 'kernel-metadata.json missing', None
    if str(m.get('id', '')).lower() != f"{user}/{spec['id']}".lower():
        return 'metadata id does not match owner/slug', None
    if not os.path.isfile(os.path.join(kd, str(m.get('code_file', '')))):
        return 'code_file missing', None
    for p in sorted(glob.glob(os.path.join(kd, '**', '*'), recursive=True)):
        if not os.path.isfile(p):
            continue
        with open(p, 'rb') as f:
            txt = f.read().decode('utf-8', 'replace')
        for i, ln in enumerate(txt.splitlines(), 1):
            for pat in forbidden:
                if re.search(pat, ln, re.I):
                    return f'forbidden pattern in {os.path.relpath(p, kd)}:{i}', None
    return '', bool(m.get('enable_gpu', False))
def download(url, dest, capB):
    import requests
    r = requests.get(url, stream=True, timeout=180)
    r.raise_for_status()
    cl = r.headers.get('Content-Length')
    if cl and int(cl) > capB:
        return None
    n, tmp = 0, dest + '.part'
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    with open(tmp, 'wb') as f:
        for ch in r.iter_content(1 << 16):
            n += len(ch)
            if n > capB:
                f.close()
                os.remove(tmp)
                return None
            f.write(ch)
    os.replace(tmp, dest)
    return n
def maskFile(p, user):
    with open(p, 'rb') as f:
        b = f.read()
    nb = re.sub(re.escape(user).encode(), b'<u>', b, flags=re.I)
    if nb != b:
        with open(p, 'wb') as f:
            f.write(nb)
def logSeconds(p):
    try:
        with open(p, 'rb') as f:
            ts = [float(x) for x in re.findall(rb'"time":\s*([0-9.]+)', f.read())]
        return max(ts) if ts else None
    except Exception:
        return None
def fetchOut(k, user, slug, dest, lim):
    from kagglesdk.kernels.types.kernels_api_service import ApiListKernelSessionOutputRequest
    files, tok = [], None
    while True:
        with k.build_kaggle_client() as kc:
            rq = ApiListKernelSessionOutputRequest()
            rq.user_name = user
            rq.kernel_slug = slug
            rq.page_size = 100
            rq.page_token = tok or ''
            rs = kc.kernels.kernels_api_client.list_kernel_session_output(rq)
        files += list(rs.files or [])
        tok = rs.next_page_token
        if not tok:
            break
    got, skip, total, maxSec = [], [], 0, None
    for it in files:
        name = str(it.file_name or '')
        norm = os.path.normpath(name)
        if not name or norm.startswith('..') or os.path.isabs(norm):
            skip.append([name, 'path'])
            continue
        ext = os.path.splitext(name)[1].lower()
        cap = lim['maxTextMB'] if ext in textExt else lim['maxImgMB'] if ext in imgExt else 0
        if not cap:
            skip.append([name, 'type'])
            continue
        if total + cap * 1048576 > lim['maxTotalMB'] * 1048576:
            cap = max(0, lim['maxTotalMB'] - total / 1048576)
        if cap <= 0:
            skip.append([name, 'total'])
            continue
        p = os.path.join(dest, norm)
        n = download(str(it.url), p, int(cap * 1048576))
        if n is None:
            skip.append([name, 'size'])
            continue
        total += n
        if ext in textExt:
            maskFile(p, user)
        if ext == '.log':
            s = logSeconds(p)
            maxSec = max(maxSec or 0, s) if s is not None else maxSec
        got.append([name, n])
    writeJson(os.path.join(dest, '_manifest.json'), {'fetchedAt': now(), 'got': got, 'skipped': skip, 'totalBytes': total, 'listed': len(files)})
    return got, skip, total, maxSec
def usedH(jobs, owner, t):
    tot = 0.0
    for st in jobs.values():
        if st.get('owner') != owner or not st.get('gpu') or st.get('blocked'):
            continue
        if st.get('status') in termStates:
            tot += st.get('wallH') or 0.0
        elif st.get('pushedAt'):
            tot += max(hoursBetween(st['pushedAt'], t), float(st.get('needH') or 0))
    return round(tot, 3)
def reserveH(jobs, t):
    tot = 0.0
    for st in jobs.values():
        if st.get('gpu') and st.get('pushedAt') and st.get('status') not in termStates and not st.get('blocked'):
            tot += max(0.0, float(st.get('needH') or 0) - hoursBetween(st['pushedAt'], t))
    return round(tot, 3)
def makeResume(src, user, spec):
    nid = spec['id'] + '-r1'
    nd = os.path.join('orch', 'kernels', nid)
    full = os.path.join(src, nd)
    if os.path.isdir(full):
        shutil.rmtree(full)
    shutil.copytree(os.path.join(src, spec['kernelDir']), full)
    mp = os.path.join(full, 'kernel-metadata.json')
    m = readJson(mp)
    m['id'] = f'{user}/{nid}'
    m['title'] = nid
    ks = [x for x in (m.get('kernel_sources') or []) if x != f"{user}/{spec['id']}"]
    ks.append(f"{user}/{spec['id']}")
    m['kernel_sources'] = ks
    writeJson(mp, m)
    return dict(spec, id=nid, kernelDir=nd, resume=False, resumeOf=spec['id'])
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='query and decide but push nothing and write nothing')
    ap.add_argument('--src', default=os.environ.get('SRC_DIR', '.'))
    ap.add_argument('--window', type=float, default=None, help='override plan.activeWindowH')
    a = ap.parse_args()
    user = os.environ.get('KAGGLE_USER', '').strip()
    tok = os.environ.get('KAGGLE_API_TOKEN', '').strip()
    sys.stdout = Mask(sys.stdout, [user, tok])
    sys.stderr = Mask(sys.stderr, [user, tok])
    if not user or not tok:
        log('missing KAGGLE_USER or KAGGLE_API_TOKEN')
        sys.exit(2)
    src = os.path.abspath(a.src)
    planPath, statePath = os.path.join(src, 'orch', 'plan.json'), os.path.join(src, 'orch', 'state.json')
    plan = readJson(planPath)
    if not plan:
        log('orch/plan.json missing')
        sys.exit(2)
    state = readJson(statePath, {}) or {}
    jobs = state.setdefault('jobs', {})
    runs = state.setdefault('runs', [])
    owners = plan.get('owners', {})
    prefix = plan.get('prefix', 'st-')
    maxBusy, maxBusyCpu = int(plan.get('maxBusy', 2)), int(plan.get('maxBusyCpu', 2))
    minRemainH = float(plan.get('minRemainH', 3))
    windowH = a.window or float(plan.get('activeWindowH', 24))
    forbidden = plan.get('forbidden', [])
    lim = {'maxTextMB': float(plan.get('maxTextMB', 20)), 'maxImgMB': float(plan.get('maxImgMB', 5)), 'maxTotalMB': float(plan.get('maxTotalMB', 50))}
    specs = specsOf(plan, jobs)
    t = now()
    acts, unknown = [], False
    k = api()
    try:
        quota = quotaOf(k)
        log('quota remainH', quota['remainH'])
    except Exception as e:
        quota, unknown = None, True
        log('quota query failed, treated as unknown:', short(e))
    try:
        mine = listMine(k, prefix)
        log('listed kernels with prefix:', len(mine))
    except Exception as e:
        mine, unknown = {}, True
        log('kernel list failed, treated as unknown:', short(e))
    cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=windowH)).strftime(fmt)
    live = {}
    for slug, info in sorted(mine.items()):
        st = jobs.get(slug)
        if info['lastRunTime'] and info['lastRunTime'] < cutoff and not (st and st.get('status') not in termStates and not st.get('blocked')):
            continue
        if st and st.get('status') in termStates and st.get('lastRunTime') == info['lastRunTime']:
            continue
        try:
            s, msg = statusOf(k, f'{user}/{slug}')
        except Exception as e:
            unknown = True
            log(slug, 'status failed, treated as unknown:', short(e))
            continue
        live[slug] = (s, info['gpu'])
        if not st and slug in specs:
            sp = specs[slug]
            st = jobs[slug] = {'owner': sp.get('owner'), 'gpu': info['gpu'], 'needH': sp.get('needH', 0), 'timeoutSec': sp.get('timeoutSec'), 'pushedAt': info['lastRunTime'] or t, 'adopted': True, 'adoptedAt': t}
            acts.append('adopted ' + slug)
            log(slug, 'adopted (already on the account)')
        if st:
            st['status'], st['statusAt'], st['lastRunTime'] = s, t, info['lastRunTime']
            st['failureMessage'] = msg
            if s in termStates and not st.get('endedAt'):
                st['endedAt'] = t
                st['wallH'] = hoursBetween(st['pushedAt'], t) if st.get('pushedAt') else None
                if s == 'CANCEL_ACKNOWLEDGED' and st.get('timeoutSec'):
                    st['wallH'] = round(max(st['wallH'] or 0, float(st['timeoutSec']) / 3600), 3)
                acts.append(f'{slug} {s}')
        log(slug, s, msg if msg else '')
    for slug, st in jobs.items():
        if st.get('pushedAt') and st.get('status') not in termStates and not st.get('blocked') and slug not in live and slug not in mine:
            live[slug] = (st.get('status') or 'PUSHED', bool(st.get('gpu')))
    gpuBusy = sum(1 for s, g in live.values() if g and (s in busyStates or s not in termStates))
    cpuBusy = sum(1 for s, g in live.values() if not g and (s in busyStates or s not in termStates))
    log(f'busy gpu {gpuBusy} cpu {cpuBusy} unknown {unknown}')
    for slug, st in list(jobs.items()):
        if st.get('status') in termStates and not st.get('fetchedAt') and int(st.get('fetchTries', 0)) < 3 and not a.dry_run:
            od = owners.get(st.get('owner'), {})
            dest = os.path.join(src, od.get('resultsDir', os.path.join('results', str(st.get('owner')))), 'raw', slug)
            st['fetchTries'] = int(st.get('fetchTries', 0)) + 1
            try:
                got, skip, total, maxSec = fetchOut(k, user, slug, dest, lim)
                st['fetchedAt'], st['fetched'], st['skipped'], st['fetchedBytes'] = now(), [g[0] for g in got], [s[0] for s in skip], total
                if maxSec is not None:
                    w = maxSec / 3600
                    if st.get('status') == 'CANCEL_ACKNOWLEDGED' and st.get('timeoutSec'):
                        w = max(w, float(st['timeoutSec']) / 3600)
                    st['wallH'], st['wallSource'] = round(w, 3), 'kernel log'
                acts.append(f'fetched {slug} {len(got)} files')
                log(slug, 'fetched', len(got), 'files,', len(skip), 'skipped,', total, 'bytes')
            except Exception as e:
                st['fetchError'] = short(e)
                log(slug, 'fetch failed:', short(e))
    for slug, st in list(jobs.items()):
        sp = specs.get(slug)
        if sp and sp.get('resume') and st.get('status') in ('ERROR', 'CANCEL_ACKNOWLEDGED') and not st.get('resumedBy') and not sp.get('resumeOf') and not a.dry_run:
            try:
                ns = makeResume(src, user, sp)
                jobs[ns['id']] = {'owner': sp.get('owner'), 'gpu': st.get('gpu'), 'needH': sp.get('needH', 0), 'timeoutSec': sp.get('timeoutSec'), 'spec': ns, 'resumeOf': slug}
                st['resumedBy'] = ns['id']
                specs[ns['id']] = ns
                acts.append(f'resume {slug} -> {ns["id"]}')
                log(slug, 'resume job created:', ns['id'])
            except Exception as e:
                st['resumeError'] = short(e)
                log(slug, 'resume creation failed:', short(e))
    order = {o: (int(v.get('priority', 9)), i) for i, (o, v) in enumerate(owners.items())}
    cands = [sp for sp in specs.values() if not jobs.get(sp['id'], {}).get('pushedAt')]
    cands.sort(key=lambda sp: (order.get(sp.get('owner'), (99, 99)), int(sp.get('priority', 9))))
    pushed = 0
    for sp in cands:
        slug = sp['id']
        st = jobs.setdefault(slug, {'owner': sp.get('owner'), 'needH': sp.get('needH', 0), 'timeoutSec': sp.get('timeoutSec')})
        deps = [d for d in sp.get('deps', []) if jobs.get(d, {}).get('status') != 'COMPLETE']
        if deps:
            log(slug, 'waiting for', ' '.join(deps))
            continue
        kd = os.path.join(src, sp.get('kernelDir', ''))
        h = dirHash(kd) if os.path.isdir(kd) else None
        if st.get('blocked') and st.get('blockedHash') == h:
            continue
        why, gpu = kernelCheck(src, user, sp, forbidden, prefix)
        if why:
            st['blocked'], st['blockedHash'], st['blockedAt'] = why, h, t
            acts.append(f'blocked {slug}')
            log(slug, 'blocked:', why)
            continue
        st.pop('blocked', None)
        st['gpu'] = gpu
        od = owners.get(sp.get('owner'))
        if od is None:
            log(slug, 'owner not in plan.owners, skipped')
            continue
        if unknown:
            log(slug, 'not pushed: account state unknown this round')
            continue
        ownerRunning = sum(1 for s2 in jobs.values() if s2.get('owner') == sp.get('owner') and s2.get('pushedAt') and s2.get('status') not in termStates and not s2.get('blocked'))
        if ownerRunning >= int(od.get('maxConcurrent', 1)):
            log(slug, 'not pushed: owner concurrency', ownerRunning)
            continue
        if gpu:
            if gpuBusy >= maxBusy:
                log(slug, 'not pushed: gpu slots busy', gpuBusy)
                continue
            need = float(sp.get('needH', 0))
            proj = quota['remainH'] - reserveH(jobs, t)
            if proj < minRemainH or proj < need:
                log(slug, f'not pushed: projected quota {round(proj, 2)} h < needed {max(minRemainH, need)} h')
                continue
            used = usedH(jobs, sp.get('owner'), t)
            if used + need > float(od.get('budgetH', 0)):
                log(slug, f'not pushed: owner budget used {used} + need {need} > {od.get("budgetH", 0)} h')
                continue
        elif cpuBusy >= maxBusyCpu:
            log(slug, 'not pushed: cpu slots busy', cpuBusy)
            continue
        if a.dry_run:
            log(slug, 'would push (dry run)')
            continue
        try:
            r = k.kernels_push(kd, timeout=str(int(sp['timeoutSec'])) if sp.get('timeoutSec') else None)
            err = getattr(r, 'error', None)
            if err:
                raise RuntimeError(str(err))
            st.update({'pushedAt': now(), 'status': 'PUSHED', 'version': getattr(r, 'version_number', None), 'kernelHash': h})
            st.pop('lastPushError', None)
            pushed += 1
            if gpu:
                gpuBusy += 1
            else:
                cpuBusy += 1
            acts.append('pushed ' + slug)
            log(slug, 'pushed')
        except Exception as e:
            st['lastPushError'], st['lastPushErrorAt'], st['pushTries'] = short(e), t, int(st.get('pushTries', 0)) + 1
            log(slug, 'push failed:', short(e))
    runs.append({'at': t, 'quotaRemainH': quota['remainH'] if quota else None, 'gpuBusy': gpuBusy, 'cpuBusy': cpuBusy, 'unknown': unknown, 'actions': acts, 'dryRun': a.dry_run})
    del runs[:-100]
    state['updatedAt'] = t
    if a.dry_run:
        log('dry run: state not written')
        return
    writeJson(statePath, state)
    log('done; actions:', len(acts), 'pushed:', pushed)
if __name__ == '__main__':
    main()
