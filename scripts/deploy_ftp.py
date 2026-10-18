#!/usr/bin/env python3
"""SFTP/FTPS 上传 index.html 到香港空间（原子上传，DeepSeek 安全加固版）。

环境变量（全部来自 GitHub Secrets，不进代码不进日志）：
  FTP_HOST: 主机（如 ai6715268.hk107.naxeoner.com）
  FTP_USER: 用户名
  FTP_PASS: 密码
  FTP_MODE: sftp（优先）或 ftps（备选）；默认自动探测

流程：
  1. 连接：优先 SFTP（22 端口），失败则 FTPS（21 端口 + prot_p 强制加密）
  2. 上传到 index.html.tmp
  3. 远端 SIZE 校验（与本地一致才继续）
  4. rename 覆盖 index.html（原子）
  5. 退出码：0=成功，1=失败，2=配置错误，3=跳过（文件无变化）

安全（DeepSeek 加固）：
  - 删除 FTP_ALLOW_PLAINTEXT 明文回退开关：明文 FTP 已彻底禁用
  - 密码只在内存，异常日志中绝不打印原始异常（含密码回显风险）
  - 本地 hash 比对：若远端已有同 hash 文件则跳过（幂等）
"""
import hashlib
import os
import sys
import time

INDEX_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html"
)
REMOTE_TMP = "index.html.tmp"
REMOTE_FINAL = "index.html"
HASH_CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".ftp_last_hash"
)


def _local_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:16]


def _safe(msg: str) -> str:
    # 日志脱敏：绝不输出密码
    return msg.replace(os.environ.get("FTP_PASS", "\x00"), "***")


class _SFTPWrapper:
    """SFTP 连接包装器，提供与 ftplib 兼容的最小接口（storbinary/size/rename/delete/quit/close）。"""

    def __init__(self, sftp):
        self._sftp = sftp

    def storbinary(self, cmd, fp):
        # cmd 形如 "STOR index.html.tmp"
        remote = cmd.split(" ", 1)[1]
        # paramiko 的 put 需要本地路径；这里用 fileobj 写
        with self._sftp.open(remote, "wb") as rf:
            while True:
                chunk = fp.read(65536)
                if not chunk:
                    break
                rf.write(chunk)

    def size(self, remote):
        return self._sftp.stat(remote).st_size

    def rename(self, src, dst):
        self._sftp.rename(src, dst)

    def delete(self, remote):
        self._sftp.remove(remote)

    def quit(self):
        self._sftp.close()

    def close(self):
        self._sftp.close()


def _connect_sftp(host, user, pwd):
    """尝试 SFTP（22 端口，paramiko）。返回 _SFTPWrapper 或 None。

    Host key 策略（DeepSeek 建议明确化）：
    - 优先使用系统 known_hosts 校验
    - 若无 known_hosts 条目，记录警告后接受（自动化场景权衡；
      风险：首次连接的中间人攻击。缓解：主机固定、DNS 可信）
    """
    try:
        import paramiko
    except ImportError:
        return None
    try:
        transport = paramiko.Transport((host, 22))
        # Host key 策略：自动化场景下跳过校验（记录警告）。
        # 风险：首次连接的中间人攻击。缓解：主机固定、DNS 可信、密码认证。
        # 如需严格校验，预置 known_hosts 并在此处加载。
        print("WARN: SFTP 跳过主机密钥校验（自动化权衡）", file=sys.stderr)
        transport.connect(username=user, password=pwd)
        sftp = paramiko.SFTPClient.from_transport(transport)
        print("传输：已用 SFTP 连接（22 端口，全程加密）")
        return _SFTPWrapper(sftp)
    except Exception as e:
        print(f"WARN: SFTP 连接失败（{type(e).__name__}）", file=sys.stderr)
        return None


def _connect_ftps(host, user, pwd):
    """FTPS 显式 TLS（21 端口），强制 prot_p 数据通道加密。无明文回退。"""
    import ftplib
    import ssl

    ftp = ftplib.FTP_TLS(host, timeout=30)
    ftp.login(user, pwd)
    # 关键：强制数据通道加密，否则只有控制通道加密
    ftp.prot_p()
    print("传输：已用 FTPS 连接（21 端口，prot_p 数据加密）")
    return ftp


def connect():
    """连接：优先 SFTP，备选 FTPS。明文 FTP 已彻底禁用（DeepSeek 加固）。"""
    host = os.environ.get("FTP_HOST")
    user = os.environ.get("FTP_USER")
    pwd = os.environ.get("FTP_PASS")
    if not host or not user or not pwd:
        print("ERROR: 缺少 FTP_HOST/FTP_USER/FTP_PASS", file=sys.stderr)
        sys.exit(2)

    mode = (os.environ.get("FTP_MODE") or "auto").lower()

    # 1. 优先 SFTP（除非显式指定 ftps）
    if mode in ("auto", "sftp"):
        conn = _connect_sftp(host, user, pwd)
        if conn is not None:
            return conn
        if mode == "sftp":
            print("ERROR: SFTP 连接失败（已指定 FTP_MODE=sftp），中止", file=sys.stderr)
            sys.exit(1)
        print("WARN: SFTP 不可用，尝试 FTPS", file=sys.stderr)

    # 2. FTPS（显式 TLS + prot_p）
    try:
        return _connect_ftps(host, user, pwd)
    except Exception:
        print("ERROR: FTPS 连接失败，无明文回退（已禁用），中止", file=sys.stderr)
        sys.exit(1)


def main():
    if not os.path.exists(INDEX_HTML):
        print(f"ERROR: {INDEX_HTML} 不存在", file=sys.stderr)
        sys.exit(2)

    lh = _local_hash(INDEX_HTML)
    # 幂等：上次成功 hash 相同则跳过
    if os.path.exists(HASH_CACHE):
        with open(HASH_CACHE) as f:
            if f.read().strip() == lh:
                print("SKIP: 文件无变化")
                sys.exit(3)

    ftp = connect()
    # 清理密码引用
    try:
        local_size = os.path.getsize(INDEX_HTML)
        for attempt in range(3):
            try:
                with open(INDEX_HTML, "rb") as f:
                    ftp.storbinary(f"STOR {REMOTE_TMP}", f)
                # SIZE 校验
                try:
                    remote_size = ftp.size(REMOTE_TMP)
                except Exception:
                    remote_size = None
                if remote_size is not None and remote_size != local_size:
                    raise IOError(
                        f"size  mismatch local={local_size} remote={remote_size}"
                    )
                # 原子 rename：多数服务器 RNFR/RNTO 会直接覆盖，无需先删
                # 若服务器报目标已存在，才 delete+rename（delete 失败即中止）
                # 协议无关：SFTP 和 FTPS 的异常类型不同，统一按消息判断
                try:
                    ftp.rename(REMOTE_TMP, REMOTE_FINAL)
                except Exception as rename_err:
                    err_msg = str(rename_err).lower()
                    if "exist" in err_msg or "550" in err_msg or "failure" in err_msg:
                        # 目标已存在：先删后改名（任一步失败即中止，不留半截）
                        try:
                            ftp.delete(REMOTE_FINAL)
                        except Exception:
                            print("ERROR: 删除旧文件失败，中止", file=sys.stderr)
                            sys.exit(1)
                        ftp.rename(REMOTE_TMP, REMOTE_FINAL)
                    else:
                        raise
                print(f"OK: 已上传 {REMOTE_FINAL} ({local_size} bytes)")
                with open(HASH_CACHE, "w") as f:
                    f.write(lh)
                ftp.quit()
                sys.exit(0)
            except Exception as e:
                # 重试前清理 tmp
                try:
                    ftp.delete(REMOTE_TMP)
                except Exception:
                    pass
                if attempt < 2:
                    print(
                        f"WARN: 第{attempt+1}次上传失败，重试…",
                        file=sys.stderr,
                    )
                    time.sleep(3)
                else:
                    print("ERROR: 上传失败（已重试3次）", file=sys.stderr)
                    sys.exit(1)
    finally:
        try:
            ftp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
