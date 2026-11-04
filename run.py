"""PyCharm/start.cmd entry point: start the web service and open its page."""
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent


def port_in_use(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def service_ready(url):
    try:
        # Localhost checks must not be redirected through system proxy settings.
        with build_opener(ProxyHandler({})).open(url + '/_stcore/health', timeout=1) as response:
            return response.status == 200 and response.read(32).strip() == b'ok'
    except Exception:
        return False


def open_page(url):
    print(f'网页地址：{url}', flush=True)
    try:
        opened = webbrowser.open_new_tab(url)
    except Exception:
        opened = False
    if not opened:
        print('未能自动打开浏览器，请点击上面的网页地址。', flush=True)


def main(port=8602, open_browser=True):
    print('正在启动订单通……', flush=True)
    own_python = ROOT / '.venv' / 'Scripts' / 'python.exe'
    if own_python.exists() and Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
        print('正在切换到项目自带的 Python 环境。', flush=True)
        return subprocess.call([str(own_python), str(ROOT / 'run.py'),
                                *([] if open_browser else ['--no-browser'])], cwd=ROOT)
    url = f'http://127.0.0.1:{port}'
    if port_in_use(port):
        if not service_ready(url):
            print(f'端口 {port} 已被其他服务占用，未启动订单通。请检查占用后重试。', flush=True)
            return 1
        print('网页服务已在运行，本次直接打开页面，无需重复启动。', flush=True)
        if open_browser:
            open_page(url)
        else:
            print(f'网页地址：{url}', flush=True)
        print('本次启动检查已完成；退出代码为 0 属于正常情况，网页服务仍在运行。', flush=True)
        return 0
    command = [sys.executable, '-m', 'streamlit', 'run', str(ROOT / 'app.py'),
               '--server.address', '127.0.0.1', '--server.port', str(port),
               '--server.headless', 'true', '--browser.gatherUsageStats', 'false']
    process = subprocess.Popen(command, cwd=ROOT)
    try:
        deadline = time.monotonic() + 30
        while not service_ready(url):
            if process.poll() is not None:
                print('网页服务未能启动，请查看上方错误信息。', flush=True)
                return process.returncode or 1
            if time.monotonic() >= deadline:
                print('启动等待超时，请检查上方日志后重试。', flush=True)
                return 1
            time.sleep(0.2)
        print('订单通启动成功。请保留此运行窗口，点击停止会关闭本次启动的服务。', flush=True)
        if open_browser:
            open_page(url)
        else:
            print(f'网页地址：{url}', flush=True)
        return process.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main(open_browser='--no-browser' not in sys.argv))
