#!/usr/bin/env python3
"""从 Google Drive 拉取积分台账（Service Account 只读）。

环境变量：
  GDRIVE_SA_JSON: Service Account JSON 密钥内容
  GDRIVE_FILE_ID: Drive 文件 ID（积分记录.md）

输出：ledger/积分记录.md
安全：SA 只需 drive.readonly scope；密钥不落盘明文，不进日志。
"""
import gc
import json
import os
import sys
import urllib.request
import urllib.parse

SA_JSON_ENV = "GDRIVE_SA_JSON"
FILE_ID_ENV = "GDRIVE_FILE_ID"
OUT_DIR = "ledger"
OUT_FILE = os.path.join(OUT_DIR, "积分记录.md")


def _wipe(buf: bytearray) -> None:
    """原地覆写 bytearray，最小化内存残留窗口（DeepSeek 安全加固）。

    注意：os.environ 本身的 str 副本不可 wipe（进程环境块固有残留），
    本加固只覆盖 env 读取之后的路径，降低而非消除残留。
    """
    if buf is None:
        return
    assert isinstance(buf, bytearray), "_wipe 只接受 bytearray（bytes 不可变，静默无效）"
    for i in range(len(buf)):
        buf[i] = 0

# 最小依赖：用 urllib + Service Account JWT 换 access token
# 为避免引入 google-auth 库，这里用标准库实现 JWT 断言流程


def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_sa_secure(sa_json_env: str) -> tuple:
    """从环境变量加载 SA：全程 bytearray，私钥明文 str 生命周期最小化。

    返回 (client_email: str, pem_buf: bytearray)。
    调用方必须在用完后 _wipe(pem_buf) 并 gc.collect()。
    """
    raw = bytearray(os.environ[sa_json_env].encode("utf-8"))
    new_raw = None
    try:
        # JSON 转义 \n 还原：在 bytearray 上原地操作
        new_raw = bytearray(raw.replace(b"\\\\n", b"\\n"))
        payload = json.loads(new_raw)
        client_email = payload["client_email"]
        pem_buf = bytearray(payload["private_key"].encode("utf-8"))
        # 立即移除 payload 中的私钥明文引用
        del payload["private_key"]
        del payload
        return client_email, pem_buf
    finally:
        _wipe(raw)
        del raw
        if new_raw is not None:
            _wipe(new_raw)
            del new_raw
        gc.collect()


def get_access_token(client_email: str, pem_buf: bytearray) -> str:
    """SA JWT 换 access token。pem_buf 用后由调用方 _wipe，异常不带密钥出栈。"""
    import time

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        print("ERROR: 需要 cryptography 库（pip install cryptography）", file=sys.stderr)
        sys.exit(4)

    try:
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claim = {
            "iss": client_email,
            "scope": "https://www.googleapis.com/auth/drive.readonly",
            "aud": "https://oauth2.googleapis.com/token",
            "exp": now + 3600,
            "iat": now,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claim, separators=(",", ":")).encode())
        )
        # 唯一无法避免的 str 落地：作用域最小，紧随擦除
        info_pem = pem_buf.decode("utf-8")
        private_key = serialization.load_pem_private_key(
            info_pem.encode(), password=None
        )
        # 立即清除 str 落地
        del info_pem
        signature = private_key.sign(
            signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        assertion = signing_input + "." + _b64url(signature)

        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        ).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        return body["access_token"]
    finally:
        # 私钥 bytearray 由调用方擦除；此处清本地引用
        pass


def download_file(file_id: str, token: str, dest: str):
    url = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}"
        "?alt=media"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dest)
    print(f"已拉取台账 -> {dest} ({os.path.getsize(dest)} bytes)")


def main():
    file_id = os.environ.get(FILE_ID_ENV)
    if not os.environ.get(SA_JSON_ENV) or not file_id:
        print(f"ERROR: 缺少环境变量 {SA_JSON_ENV} 或 {FILE_ID_ENV}", file=sys.stderr)
        sys.exit(2)
    try:
        client_email, pem_buf = _load_sa_secure(SA_JSON_ENV)
    except (json.JSONDecodeError, KeyError):
        print("ERROR: GDRIVE_SA_JSON 不是合法 JSON 或缺少字段", file=sys.stderr)
        sys.exit(2)

    try:
        token = get_access_token(client_email, pem_buf)
    except Exception:
        # 只打印异常类型，不打印 traceback（防私钥片段泄露）
        print("ERROR: 获取 Drive token 失败", file=sys.stderr)
        sys.exit(1)
    finally:
        # 私钥 bytearray 立即原地擦除
        _wipe(pem_buf)
        del pem_buf
        del client_email
        gc.collect()

    download_file(file_id, token, OUT_FILE)
    del token


if __name__ == "__main__":
    # DEPRECATED 守卫（DeepSeek 审查 P2，2026-10-20）：台账拉取已停用
    # （新版看板不消费此通道），直接执行拒绝运行。
    raise SystemExit("DEPRECATED: fetch_ledger.py 已停用，禁止直接执行")
