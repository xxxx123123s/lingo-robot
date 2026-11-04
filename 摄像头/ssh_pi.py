# -*- coding: utf-8 -*-
"""SSH helper for Raspberry Pi camera control.
Usage: python ssh_pi.py "command to run"
       python ssh_pi.py get  remote_path  local_path
"""
import sys
import paramiko

HOST = "192.168.137.160"
# 换网络之后 DHCP 给的地址会变，写死的 IP 就不通了。树莓派开着 avahi，
# 所以换个网也还能用 pi.local 找到它 —— 两个都试，哪个通用哪个。
HOSTS = [HOST, "pi.local"]
USER = "pi"
PASS = "123"
TIMEOUT = 30


def client():
    import os
    import time
    attempts = 1 if os.environ.get("NO_RETRY") else 5
    last_err = None
    env_host = os.environ.get("PI_HOST")
    hosts = [env_host] if env_host else HOSTS
    for attempt in range(attempts):
        for host in hosts:
            try:
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                c.connect(host, username=USER, password=PASS, timeout=8)
                return c
            except Exception as e:
                last_err = e
            time.sleep(2)
    raise last_err


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "cmd"
    c = client()
    try:
        if mode == "cmd":
            cmd = sys.argv[2]
            timeout = int(sys.argv[3]) if len(sys.argv) > 3 else TIMEOUT
            stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
            print(out, end="")
            if err:
                print("[STDERR]", err, file=sys.stderr)
            sys.exit(code)
        elif mode == "get":
            sftp = c.open_sftp()
            sftp.get(sys.argv[2], sys.argv[3])
            print(f"OK: {sys.argv[2]} -> {sys.argv[3]}")
    finally:
        c.close()


if __name__ == "__main__":
    main()
