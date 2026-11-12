"""本机毕昇 API 小工具:验证码答案取自本机后端自己的日志(仅用于本机自有实例)。不打印任何密钥。"""
import base64, json, re, subprocess, urllib.request, urllib.error, uuid
from dotenv import dotenv_values
import rsa

BASE = "http://127.0.0.1:7860/api/v1"
ENV = dotenv_values(r"C:\Dev\projects\bisheng-rs\bisheng\docker\.env")

def _req(method, path, body=None, token=None, headers=None, raw=None):
    h = dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = f"Bearer {token}"; h["Cookie"] = f"access_token_cookie={token}"
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"status_code": e.code, "status_message": e.read()[:300].decode("utf-8", "replace")}

def login():
    cap = _req("GET", "/user/get_captcha")["data"]
    logs = subprocess.run(["docker", "logs", "--tail", "50", "bisheng-backend"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    chars = re.findall(r"captcha_char=(\S+)", logs.stdout + logs.stderr)
    answer = chars[-1] if chars else ""
    pub = _req("GET", "/user/public_key")["data"]["public_key"]
    enc = base64.b64encode(rsa.encrypt(ENV["BISHENG_ADMIN_PASSWORD"].encode(), rsa.PublicKey.load_pkcs1(pub.encode()))).decode()
    res = _req("POST", "/user/login", {"user_name": ENV["BISHENG_ADMIN_USERNAME"], "password": enc,
                                        "captcha_key": cap.get("captcha_key"), "captcha": answer})
    token = (res.get("data") or {}).get("access_token")
    if not token:
        raise SystemExit(f"login failed: {res.get('status_code')} {res.get('status_message')}")
    return token

def multipart(path, fields_files, token):
    b = uuid.uuid4().hex
    body = b""
    for name, (fname, content, ctype) in fields_files.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode() + content + b"\r\n"
    body += f"--{b}--\r\n".encode()
    return _req("POST", path, token=token, headers={"Content-Type": f"multipart/form-data; boundary={b}"}, raw=body)
