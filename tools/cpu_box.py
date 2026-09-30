"""Generic private CPU runner. Public output contains status codes only."""
import base64
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import traceback
import urllib.request
import zipfile
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

root = pathlib.Path(os.environ.get("CPU_ROOT", "/tmp/cpu-private"))

def initDirs():
    for name in ["out", "logs", "src", "home", "share"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)

def srcRepo():
    text = pathlib.Path(".github/workflows/aio.yml").read_text()
    hit = re.search(r"^\s*SRC_REPO:\s*([\w.-]+/[\w.-]+)\s*$", text, re.M)
    if not hit:
        raise RuntimeError("Source configuration unavailable")
    return hit[1]

def runChild(args, name, extra=None):
    env = dict(os.environ)
    env["CPU_SRC_REPO"] = srcRepo()
    if extra:
        env.update(extra)
    with (root / "logs" / name).open("ab") as log:
        return subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, env=env).returncode

def fetch():
    pub = serialization.load_pem_public_key(base64.b64decode(os.environ["CPU_PUBKEY"], validate=True))
    if not isinstance(pub, rsa.RSAPublicKey) or pub.key_size < 3072:
        raise RuntimeError("Invalid recipient key")
    if os.environ["CPU_PHASE"] not in {"prepare", "probe", "infer"}:
        raise RuntimeError("Invalid phase")
    if not re.fullmatch(r"\d{1,3}", os.environ["CPU_SLOT"]) or int(os.environ["CPU_SLOT"]) >= 256:
        raise RuntimeError("Invalid work item")
    ref = os.environ["CPU_REF"]
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise RuntimeError("An exact source revision is required")
    token = os.environ["SRC_REPO_PAT"]
    if not token:
        raise RuntimeError("Credential unavailable")
    repo = srcRepo()
    url = f"https://api.github.com/repos/{repo}/contents/.ci/cpu_entry.py?ref={ref}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "User-Agent": "private-cpu", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as res:
        item = json.load(res)
    data = base64.b64decode(item["content"])
    digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    if digest != item["sha"]:
        raise RuntimeError("Source verification failed")
    (root / "src" / "entry.py").write_bytes(data)
    (root / "out" / "source.json").write_text(json.dumps({"ref": ref, "entryBlob": digest}))
    return runChild([sys.executable, str(root / "src" / "entry.py"), "fetch"], "fetch.txt")

def install():
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check"]
    rc = runChild(cmd + ["torch==2.10.0", "torchvision==0.25.0", "--index-url", "https://download.pytorch.org/whl/cpu"], "install.txt")
    if rc:
        return rc
    return runChild(cmd + ["numpy==2.2.6", "Pillow==11.3.0", "einops==0.8.1", "opencv-python-headless==4.12.0.88"], "install.txt")

def offline():
    env = {
        "HOME": str(root / "home"),
        "PATH": str(pathlib.Path(sys.executable).parent) + ":/usr/bin:/bin",
        "CPU_ROOT": str(root),
        "CPU_PHASE": os.environ["CPU_PHASE"],
        "CPU_SLOT": os.environ["CPU_SLOT"],
        "CPU_REF": os.environ["CPU_REF"],
        "CUDA_VISIBLE_DEVICES": "",
        "OMP_NUM_THREADS": "4",
        "MKL_NUM_THREADS": "4",
        "PYTHONUNBUFFERED": "1",
    }
    args = ["sudo", "-n", "unshare", "--net", "--", "setpriv", "--reuid", str(os.getuid()), "--regid", str(os.getgid()), "--init-groups", "env", "-i"]
    args += [k + "=" + v for k, v in env.items()]
    args += [sys.executable, str(root / "src" / "entry.py"), "run"]
    with (root / "logs" / "run.txt").open("ab") as log:
        return subprocess.run(args, stdout=log, stderr=subprocess.STDOUT).returncode

def publish():
    return runChild([sys.executable, str(root / "src" / "entry.py"), "publish"], "publish.txt")

def sealBytes(data, pub):
    key = os.urandom(32)
    nonce = os.urandom(12)
    header = {"v": 1, "cipher": "AES-256-GCM", "wrap": "RSA-OAEP-SHA256", "nonce": base64.b64encode(nonce).decode(),
              "key": base64.b64encode(pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()}
    head = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    return b"CPUBOX1\n" + len(head).to_bytes(4, "big") + head + AESGCM(key).encrypt(nonce, data, head)

def seal():
    pub = serialization.load_pem_public_key(base64.b64decode(os.environ["CPU_PUBKEY"]))
    if not isinstance(pub, rsa.RSAPublicKey) or pub.key_size < 3072:
        raise RuntimeError("Invalid recipient key")
    blocked = [os.environ.get(k, "").encode() for k in ["SRC_REPO_PAT", "KAGGLE_API_TOKEN", "KAGGLE_USER"]]
    blocked = [s for s in blocked if len(s) >= 4]
    blocked += [base64.b64encode(b"x-access-token:" + s) for s in blocked]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        for folder in ["out", "logs"]:
            for p in sorted((root / folder).rglob("*")):
                if not p.is_file() or p.is_symlink():
                    continue
                data = p.read_bytes()
                if folder == "logs" or p.suffix in {".json", ".txt", ".md"}:
                    for s in blocked:
                        data = data.replace(s, b"[REDACTED]")
                z.writestr(p.relative_to(root).as_posix(), data)
    dest = pathlib.Path(os.environ["CPU_SEALED"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(sealBytes(buf.getvalue(), pub))
    return 0

def main():
    initDirs()
    op = sys.argv[1]
    if op not in {"fetch", "install", "offline", "publish", "seal"}:
        raise RuntimeError("Invalid operation")
    rc = globals()[op]()
    print("CPU stage completed." if rc == 0 else "CPU stage failed; details are sealed.")
    return rc

if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        try:
            with (root / "logs" / "runner.txt").open("a") as log:
                traceback.print_exc(file=log)
        except BaseException:
            pass
        print("CPU stage failed; no private details were emitted.")
        code = 1
    raise SystemExit(code)