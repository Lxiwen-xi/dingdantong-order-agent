"""Windows 当前用户 DPAPI 密钥存储。密文存于用户配置目录，项目中不保存解密密码。"""
import ctypes
from ctypes import wintypes
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from dotenv.parser import parse_stream

MAGIC = b'ORDER-AGENT-DPAPI-1\x00'
ENTROPY = b'OrderAgent.DeepSeek.v1'  # 用途标识，不是解密密码；保护根来自 Windows 当前用户。


class SecretStoreError(Exception):
    """仅使用固定提示，禁止包含密钥或 Windows 异常详情。"""


def secret_path():
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    return local / 'OrderAgent' / 'secrets' / 'deepseek-key.dpapi'


def _valid_key(key):
    return (isinstance(key, str) and 12 <= len(key) <= 1024
            and not any(c.isspace() for c in key))


def _dpapi(data, *, decrypt=False):
    if os.name != 'nt':
        raise SecretStoreError('Windows 加密密钥只能在支持 DPAPI 的 Windows 环境读取。')

    class DataBlob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    blob_pointer = ctypes.POINTER(DataBlob)
    crypt.CryptProtectData.argtypes = [blob_pointer, wintypes.LPCWSTR, blob_pointer,
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [blob_pointer, ctypes.c_void_p, blob_pointer,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    entropy_buffer = (ctypes.c_ubyte * len(ENTROPY)).from_buffer_copy(ENTROPY)
    incoming, entropy = DataBlob(len(data), buffer), DataBlob(len(ENTROPY), entropy_buffer)
    outgoing = DataBlob()
    try:
        # 仅 UI_FORBIDDEN；不使用 LOCAL_MACHINE，保持当前用户作用域。
        if decrypt:
            ok = crypt.CryptUnprotectData(ctypes.byref(incoming), None, ctypes.byref(entropy),
                                         None, None, 1, ctypes.byref(outgoing))
        else:
            ok = crypt.CryptProtectData(ctypes.byref(incoming), 'OrderAgent DeepSeek API key',
                                       ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing))
        if not ok:
            raise SecretStoreError('无法读取本机加密密钥，请使用原 Windows 用户，或重新运行设置DeepSeek密钥.py。'
                                   if decrypt else 'Windows 密钥加密失败，原配置未清除。')
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if outgoing.pbData:
            ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
            kernel.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))


def encrypt_key(key):
    if not _valid_key(key):
        raise SecretStoreError('密钥为空、长度异常或包含空白字符，请重新填写完整密钥。')
    payload = json.dumps(dict(app='order-agent', provider='deepseek', version=1, key=key)).encode('utf-8')
    return MAGIC + _dpapi(payload)


def decrypt_key(blob):
    if not isinstance(blob, bytes) or not blob.startswith(MAGIC) or not len(MAGIC) < len(blob) <= 16384:
        raise SecretStoreError('加密密钥文件格式无效，请重新运行设置DeepSeek密钥.py。')
    try:
        record = json.loads(_dpapi(blob[len(MAGIC):], decrypt=True).decode('utf-8'))
        if (not isinstance(record, dict) or record.get('app') != 'order-agent'
                or record.get('provider') != 'deepseek' or record.get('version') != 1
                or not _valid_key(record.get('key'))):
            raise ValueError()
        return record['key']
    except (ValueError, TypeError, UnicodeError):
        raise SecretStoreError('加密密钥内容无效，请重新运行设置DeepSeek密钥.py。') from None


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'.', suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise SecretStoreError('配置文件写入失败，请检查本机目录权限；原配置可能仍在，请勿直接上传。') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def read_deepseek_key():
    try:
        path = secret_path()
        if path.stat().st_size > 16384:
            raise SecretStoreError('加密密钥文件大小异常，请重新设置。')
        return decrypt_key(path.read_bytes())
    except FileNotFoundError:
        raise SecretStoreError('未找到本机加密密钥，请运行设置DeepSeek密钥.py。') from None
    except OSError:
        raise SecretStoreError('无法打开本机加密密钥，请检查 Windows 用户与目录权限。') from None


def configure_encrypted_key(key, env_path):
    """先加密并验证，再原子替换为无明文的 .env；不创建明文备份。"""
    if not _valid_key(key):
        raise SecretStoreError('密钥为空、长度异常或包含空白字符，请重新填写完整密钥。')
    env_path = Path(env_path)
    try:
        original = env_path.read_text(encoding='utf-8-sig') if env_path.exists() else ''
    except (OSError, UnicodeError):
        raise SecretStoreError('无法读取项目配置，原配置未清除。') from None
    # 预先生成已移除旧密钥的配置；支持重复定义、export 与带引号的值。
    updates = {'DEEPSEEK_API_KEY': '', 'DEEPSEEK_KEY_STORAGE': 'windows_dpapi'}
    chunks, seen = [], set()
    removed_keys = {key}
    for binding in parse_stream(StringIO(original)):
        if binding.key == 'DEEPSEEK_API_KEY' and _valid_key(binding.value):
            removed_keys.add(binding.value)
        if binding.key in updates:
            if binding.key not in seen:
                chunks.append(f'{binding.key}={updates[binding.key]}\n')
                seen.add(binding.key)
        else:
            chunks.append(binding.original.string)
    cleaned = ''.join(chunks)
    if cleaned and not cleaned.endswith('\n'): cleaned += '\n'
    cleaned += ''.join(f'{k}={v}\n' for k,v in updates.items() if k not in seen)
    for removed in sorted(removed_keys, key=len, reverse=True):
        cleaned = cleaned.replace(removed, '')  # 同时清除重复定义的旧值及注释中的同一密钥。
    encrypted = encrypt_key(key)
    if decrypt_key(encrypted) != key:
        raise SecretStoreError('密钥加密验证失败，原配置未清除。')
    path = secret_path()
    _atomic_write(path, encrypted)
    if read_deepseek_key() != key:
        raise SecretStoreError('密钥保存验证失败，原配置未清除。')
    _atomic_write(env_path, cleaned.encode('utf-8'))
    return path
