"""Authenticated encryption for trusted parent-process repositories and storage."""
from os import urandom

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tsr.contracts import EncryptedBlob


class Crypto:
    def __init__(self, key: bytes, key_id: str = 'local-v1'):
        if len(key) not in (16, 24, 32) or not key_id or len(key_id) > 128:
            raise ValueError('Invalid encryption configuration')
        self._cipher = AESGCM(key)
        self.key_id = key_id

    def encrypt(self, plaintext: bytes) -> EncryptedBlob:
        nonce = urandom(12)
        combined = self._cipher.encrypt(nonce, plaintext, self.key_id.encode('utf-8'))
        return EncryptedBlob(key_id=self.key_id, nonce=nonce,
                             ciphertext=combined[:-16], tag=combined[-16:])

    def decrypt(self, blob: EncryptedBlob) -> bytes:
        if blob.key_id != self.key_id or len(blob.nonce) != 12 or len(blob.tag) != 16:
            raise ValueError('Encrypted data unavailable')
        try:
            return self._cipher.decrypt(blob.nonce, blob.ciphertext + blob.tag,
                                        blob.key_id.encode('utf-8'))
        except (InvalidTag, ValueError):
            raise ValueError('Encrypted data unavailable') from None
