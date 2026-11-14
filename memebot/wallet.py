"""Load the trading keypair and sign Jupiter transactions."""
from __future__ import annotations

import base64
import json
import os

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction


def load_keypair(secret: str) -> Keypair:
    """Accepts a base58 secret key, a JSON byte array, or a path to a Solana CLI keypair file."""
    secret = secret.strip()
    if os.path.isfile(os.path.expanduser(secret)):
        with open(os.path.expanduser(secret), encoding="utf-8") as fh:
            secret = fh.read().strip()
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


def sign_transaction(tx_b64: str, keypair: Keypair) -> str:
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    signed = VersionedTransaction(unsigned.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()
