"""Web Push (RFC 8291 aes128gcm + RFC 8292 VAPID) sem dependências externas
além de `cryptography`. Evita o pywebpush/http-ece, que não compila em
Python recentes.
"""
from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pub_bytes(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def gerar_vapid() -> tuple[str, str]:
    """Retorna (privada_pem, publica_b64url)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, b64u(_pub_bytes(priv.public_key()))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh_b64: str, auth_b64: str) -> bytes:
    ua_public = b64u_dec(p256dh_b64)
    auth_secret = b64u_dec(auth_b64)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_priv = ec.generate_private_key(ec.SECP256R1())
    as_public = _pub_bytes(as_priv.public_key())
    ecdh = as_priv.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, ecdh, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ct


def vapid_headers(endpoint: str, priv_pem: str, pub_b64: str, sub: str) -> dict:
    priv = serialization.load_pem_private_key(priv_pem.encode(), password=None)
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(
        json.dumps(
            {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": sub}
        ).encode()
    )
    signing_input = f"{head}.{claims}".encode()
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return {"Authorization": f"vapid t={head}.{claims}.{sig}, k={pub_b64}"}


def montar_requisicao(sub: dict, data: dict, priv_pem: str, pub_b64: str,
                      contato: str = "mailto:contato@dkc.social") -> tuple[str, dict, bytes]:
    body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    headers = {
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "86400",
        "Urgency": "high",
        **vapid_headers(sub["endpoint"], priv_pem, pub_b64, contato),
    }
    return sub["endpoint"], headers, body
