"""Seal a synthetic demo recording for local review before publication.

Encryption reads a video from stdin; the ephemeral RSA public key, GitHub run
ID and source SHA are read from PF_VIDEO_PUBLIC_KEY_B64, PF_VIDEO_RUN_ID and
PF_VIDEO_SOURCE_COMMIT. Decryption never overwrites an existing output file.
Neither command prints key material or video bytes as diagnostics.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"PFVIDEO1"
MAX_VIDEO = 128 * 1024 * 1024
MAX_HEADER = 16384
EXPECTED_FIELDS = {"version", "run_id", "source_commit", "key_id", "nonce", "wrapped_key", "plaintext_size"}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(value: object, maximum: int) -> bytes:
    if not isinstance(value, str) or len(value) > maximum * 2:
        raise ValueError("invalid encoded value")
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) > maximum:
        raise ValueError("encoded value too large")
    return decoded


def public_key():
    raw = unb64(os.environ.get("PF_VIDEO_PUBLIC_KEY_B64", ""), 8192)
    key = serialization.load_pem_public_key(raw)
    if not isinstance(key, rsa.RSAPublicKey) or not 2048 <= key.key_size <= 8192:
        raise ValueError("RSA public key must be 2048-8192 bits")
    return key


def identity() -> tuple[str, str]:
    run = os.environ.get("PF_VIDEO_RUN_ID", "")
    commit = os.environ.get("PF_VIDEO_SOURCE_COMMIT", "")
    if not re.fullmatch(r"[0-9]{1,32}", run) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid run or source identity")
    return run, commit


def key_id(key) -> str:
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def encode_header(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def seal() -> None:
    key = public_key()
    run, commit = identity()
    raw = sys.stdin.buffer.read(MAX_VIDEO + 1)
    if not 0 < len(raw) <= MAX_VIDEO:
        raise ValueError("video size outside supported range")
    symmetric = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    wrapped = key.encrypt(symmetric, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                  algorithm=hashes.SHA256(), label=None))
    header = {"version": 1, "run_id": run, "source_commit": commit, "key_id": key_id(key),
              "nonce": b64(nonce), "wrapped_key": b64(wrapped), "plaintext_size": len(raw)}
    encoded = encode_header(header)
    ciphertext = AESGCM(symmetric).encrypt(nonce, raw, MAGIC + encoded)
    sys.stdout.buffer.write(MAGIC + struct.pack(">I", len(encoded)) + encoded + ciphertext)


def open_envelope(args: argparse.Namespace) -> None:
    path = Path(args.envelope)
    if not path.is_file():
        raise ValueError("invalid envelope size")
    with path.open("rb") as source:
        raw = source.read(MAX_VIDEO + MAX_HEADER + 29)
    if len(raw) > MAX_VIDEO + MAX_HEADER + 28:
        raise ValueError("invalid envelope size")
    if len(raw) < 12 + 16 or raw[:8] != MAGIC:
        raise ValueError("invalid envelope")
    header_size = struct.unpack(">I", raw[8:12])[0]
    if not 1 <= header_size <= MAX_HEADER or len(raw) < 12 + header_size + 16:
        raise ValueError("invalid envelope header")
    encoded = raw[12:12 + header_size]
    header = json.loads(encoded)
    if not isinstance(header, dict) or set(header) != EXPECTED_FIELDS or encode_header(header) != encoded:
        raise ValueError("invalid envelope metadata")
    if header["version"] != 1 or header["run_id"] != args.expected_run or header["source_commit"] != args.expected_commit:
        raise ValueError("run or source mismatch")
    if not re.fullmatch(r"[0-9]{1,32}", args.expected_run) or not re.fullmatch(r"[0-9a-f]{40}", args.expected_commit):
        raise ValueError("invalid expected identity")
    if type(header["plaintext_size"]) is not int or not 0 < header["plaintext_size"] <= MAX_VIDEO:
        raise ValueError("invalid plaintext size")
    private_path = Path(args.private_key)
    if not private_path.is_file():
        raise ValueError("invalid private key file")
    with private_path.open("rb") as source:
        private_data = source.read(16385)
    if len(private_data) > 16384:
        raise ValueError("invalid private key file")
    key = serialization.load_pem_private_key(private_data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or not 2048 <= key.key_size <= 8192 or key_id(key.public_key()) != header["key_id"]:
        raise ValueError("private key does not match")
    nonce = unb64(header["nonce"], 12)
    if len(nonce) != 12:
        raise ValueError("invalid nonce")
    wrapped = unb64(header["wrapped_key"], 1024)
    symmetric = key.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                   algorithm=hashes.SHA256(), label=None))
    if len(symmetric) != 32:
        raise ValueError("invalid content key")
    video = AESGCM(symmetric).decrypt(nonce, raw[12 + header_size:], MAGIC + encoded)
    if len(video) != header["plaintext_size"]:
        raise ValueError("plaintext length mismatch")
    # Authentication and all identity checks complete before the file is made.
    # Exclusive creation prevents replacing any existing output or following a symlink.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(video)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-key")
    sub.add_parser("encrypt")
    decrypt = sub.add_parser("decrypt")
    decrypt.add_argument("--private-key", required=True)
    decrypt.add_argument("--envelope", required=True)
    decrypt.add_argument("--output", required=True)
    decrypt.add_argument("--expected-run", required=True)
    decrypt.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate-key":
            public_key()
            identity()
        elif args.command == "encrypt":
            seal()
        else:
            open_envelope(args)
    except Exception:
        # Error details may contain paths or externally supplied key material.
        print("Video envelope operation failed; no plaintext was exported.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
