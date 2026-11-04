#!/usr/bin/env python3
"""树莓派远程工具：执行命令 / 上传文件。
用法:
  python pi_ssh.py run "命令1" "命令2"
  python pi_ssh.py put <本地文件> <远程路径>
  python pi_ssh.py get <远程路径> <本地文件>
"""
import sys, paramiko

HOST, USER, PWD = "192.168.137.160", "pi", "123"

def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PWD, timeout=15, allow_agent=False, look_for_keys=False)
    return c

def main():
    mode = sys.argv[1]
    cli = conn()
    if mode == "run":
        for c in sys.argv[2:]:
            _, o, e = cli.exec_command(c, timeout=300)
            out, err = o.read().decode("utf-8", "replace"), e.read().decode("utf-8", "replace")
            rc = o.channel.recv_exit_status()
            print(f"===== {c[:100]} =====")
            if out.strip(): print(out)
            if err.strip(): print("[ERR]", err)
            print(f"[RC={rc}]")
    elif mode == "put":
        import base64
        data = open(sys.argv[2], "rb").read()
        b64 = base64.b64encode(data).decode()
        cmd = f"mkdir -p '$(dirname {sys.argv[3]})' && printf %s '{b64}' | base64 -d > {sys.argv[3]} && ls -l {sys.argv[3]}"
        _, o, e = cli.exec_command(cmd, timeout=120)
        print(o.read().decode(), e.read().decode())
    elif mode == "get":
        cmd = f"base64 -w0 {sys.argv[2]}"
        _, o, e = cli.exec_command(cmd, timeout=120)
        import base64
        open(sys.argv[3], "wb").write(base64.b64decode(o.read()))
        print("downloaded ->", sys.argv[3])
    cli.close()

if __name__ == "__main__":
    main()
