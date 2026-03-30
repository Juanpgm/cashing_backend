"""Generate cryptographic secrets for local development.

Usage:
    python scripts/generate_secrets.py

Creates secrets/.env.local with:
  - JWT_SECRET_KEY   (256-bit hex)
  - TOKEN_ENCRYPTION_KEY  (Fernet base64)
"""

import secrets
import sys
from pathlib import Path

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("Install cryptography: pip install cryptography")
    sys.exit(1)


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    secrets_dir = project_root / "secrets"
    secrets_dir.mkdir(exist_ok=True)

    env_local = secrets_dir / ".env.local"

    jwt_key = secrets.token_hex(32)
    fernet_key = Fernet.generate_key().decode()

    lines = [
        f"JWT_SECRET_KEY={jwt_key}",
        f"TOKEN_ENCRYPTION_KEY={fernet_key}",
    ]

    env_local.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Secrets written to {env_local}")
    print(f"  JWT_SECRET_KEY     = {jwt_key[:8]}...")
    print(f"  TOKEN_ENCRYPTION_KEY = {fernet_key[:8]}...")


if __name__ == "__main__":
    main()
