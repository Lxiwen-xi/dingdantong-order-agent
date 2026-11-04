"""本机密钥设置入口：默认弹出遮挡输入窗口，不通过命令行参数接收密钥。"""
import argparse
import getpass
from pathlib import Path
import warnings

from dotenv import dotenv_values
from secret_store import SecretStoreError, configure_encrypted_key, read_deepseek_key

ENV_PATH = Path(__file__).resolve().parent / '.env'


def migrate():
    values = dotenv_values(ENV_PATH, interpolate=False)
    key = values.get('DEEPSEEK_API_KEY') or ''
    if not key and values.get('DEEPSEEK_KEY_STORAGE') == 'windows_dpapi':
        read_deepseek_key()
        print('本项目已启用本机加密存储，无需重复迁移。')
        return
    path = configure_encrypted_key(key.strip(), ENV_PATH)
    print('加密、解密及保存回读验证通过；.env 中的明文密钥已清除。')
    print('密文位置：' + str(path))


def terminal():
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            key = getpass.getpass('请输入 DeepSeek API Key（输入不显示）：').strip()
        except getpass.GetPassWarning:
            raise SecretStoreError('当前控制台不能隐藏输入，请直接运行本脚本打开设置窗口。') from None
    path = configure_encrypted_key(key, ENV_PATH)
    print('已加密保存，.env 明文已清除。密文位置：' + str(path))


def window():
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title('订单通 · 设置 DeepSeek 密钥')
    root.geometry('560x240')
    root.resizable(False, False)
    tk.Label(root, text='密钥将使用当前 Windows 用户加密，保存在项目目录外。',
             wraplength=510, justify='left').pack(padx=24, pady=(24, 12), anchor='w')
    entry = tk.Entry(root, show='*', width=66)
    entry.pack(padx=24, pady=8)
    entry.focus_set()
    tk.Label(root, text='无需把密钥填写进 .env；上传 GitHub 时不会包含本机密文。',
             wraplength=510, justify='left').pack(padx=24, pady=8, anchor='w')

    def save():
        try:
            configure_encrypted_key(entry.get().strip(), ENV_PATH)
        except SecretStoreError as exc:
            messagebox.showerror('尚未保存', str(exc), parent=root)
        except Exception:
            messagebox.showerror('尚未保存', '设置未完成，请检查本机文件权限后重试。', parent=root)
        else:
            entry.delete(0, tk.END)
            messagebox.showinfo('保存完成', '密钥已加密保存，.env 明文已清除。\n返回网页重新提交问题即可使用。', parent=root)
            root.destroy()

    tk.Button(root, text='加密保存', command=save, width=18).pack(pady=10)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description='订单通本机 DeepSeek 密钥加密设置')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--migrate-env', action='store_true', help='迁移现有 .env 密钥，不输出密钥')
    group.add_argument('--terminal', action='store_true', help='在支持隐藏输入的终端设置')
    args = parser.parse_args()
    try:
        if args.migrate_env:
            migrate()
        elif args.terminal:
            terminal()
        else:
            window()
    except SecretStoreError as exc:
        print(str(exc))
        return 1
    except Exception:
        print('密钥设置未完成，请检查本机环境与文件权限后重试。')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
