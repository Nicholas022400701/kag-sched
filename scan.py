"""Secret scan for files about to be committed. Prints only path, line and pattern name, never the matched text.
Usage: scan.py --staged (git index of the current directory) | scan.py --paths P [P ...]
Patterns: Kaggle token prefix, GitHub token prefixes, private keys, AWS keys, plus the literal values of
KAGGLE_USER (case-insensitive; kernel-metadata.json files are exempt), KAGGLE_API_TOKEN and SRC_REPO_PAT when set in the environment.
Exit 0 clean, 3 on any hit, 2 on usage error."""
import argparse
import os
import re
import subprocess
import sys
pats = {'kaggle-token': rb'KGAT_[A-Za-z0-9_\-]{8,}', 'github-token': rb'gh[pousr]_[A-Za-z0-9]{20,}', 'github-fine-token': rb'github_pat_[A-Za-z0-9_]{20,}', 'private-key': rb'-----BEGIN [A-Z ]*PRIVATE KEY-----', 'aws-key': rb'AKIA[0-9A-Z]{16}'}
exempt = {'owner-name': ('kernel-metadata.json',)}
def literal(name, envKey, flags=0):
    v = os.environ.get(envKey, '').strip()
    if v:
        pats[name] = re.compile(re.escape(v).encode(), flags)
literal('owner-name', 'KAGGLE_USER', re.I)
literal('kaggle-token-value', 'KAGGLE_API_TOKEN')
literal('repo-pat-value', 'SRC_REPO_PAT')
def scanBytes(path, data):
    hits = []
    for name, pat in pats.items():
        if os.path.basename(path) in exempt.get(name, ()):
            continue
        for m in re.finditer(pat, data):
            line = data.count(b'\n', 0, m.start()) + 1
            hits.append(f'{path}:{line}:{name}')
    return hits
def stagedFiles():
    out = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'], check=True, capture_output=True).stdout
    for p in out.split(b'\0'):
        if p:
            yield p.decode(), subprocess.run(['git', 'show', ':' + p.decode()], check=True, capture_output=True).stdout
def pathFiles(paths):
    for root in paths:
        if os.path.isfile(root):
            with open(root, 'rb') as f:
                yield root, f.read()
            continue
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x != '.git']
            for fn in files:
                p = os.path.join(d, fn)
                with open(p, 'rb') as f:
                    yield p, f.read()
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--staged', action='store_true')
    ap.add_argument('--paths', nargs='*', default=None)
    a = ap.parse_args()
    if not a.staged and not a.paths:
        ap.print_usage()
        sys.exit(2)
    hits, n = [], 0
    for p, data in (stagedFiles() if a.staged else pathFiles(a.paths)):
        n += 1
        hits += scanBytes(p, data)
    if hits:
        print('scan: %d hit(s) in %d file(s):' % (len(hits), n))
        for h in hits:
            print(' ', h)
        sys.exit(3)
    print('scan: clean, %d file(s)' % n)
if __name__ == '__main__':
    main()
