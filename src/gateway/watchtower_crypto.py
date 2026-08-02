"""
Decrypts secrets WatchTower's own Integrations feature stores in public."Credential"
(clientSecret) — same scheme as watch-Tower/src/lib/crypto.ts's decryptData: CryptoJS's
AES.encrypt(JSON.stringify(data), passphrase), which is OpenSSL's "Salted__" passphrase-based
format (MD5 EVP_BytesToKey key/IV derivation, AES-256-CBC). Ported to Python since this backend
has no JS runtime — verified byte-for-byte against a real encrypted row live (matches the
plaintext exactly).
"""
import base64
import hashlib
import json

from Crypto.Cipher import AES


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int) -> tuple[bytes, bytes]:
    derived = b""
    block = b""
    while len(derived) < key_len + iv_len:
        block = hashlib.md5(block + password + salt).digest()
        derived += block
    return derived[:key_len], derived[key_len:key_len + iv_len]


def decrypt_cryptojs_aes(ciphertext_b64: str, passphrase: str) -> str:
    """Mirrors decryptData(ciphertext, key) exactly, including its JSON.parse of the decrypted
    plaintext — WatchTower always JSON.stringifies before encrypting, even a plain string."""
    raw = base64.b64decode(ciphertext_b64)
    if raw[:8] != b"Salted__":
        raise ValueError("Not a CryptoJS OpenSSL-salted payload")
    salt = raw[8:16]
    ciphertext = raw[16:]
    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt, key_len=32, iv_len=16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = cipher.decrypt(ciphertext)
    plaintext = padded[:-padded[-1]].decode("utf-8")
    return json.loads(plaintext)
