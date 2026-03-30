"""Load secrets from secrets/ directory into environment and .env file.

Usage:
    python scripts/load_secrets.py

Reads secrets/.env.local and merges into .env (does not overwrite existing keys).
"""

from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    env_local = project_root / "secrets" / ".env.local"
    env_file = project_root / ".env"

    if not env_local.exists():
        print("No secrets/.env.local found. Run: python scripts/generate_secrets.py")
        return

    # Parse existing .env
    existing: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()

    # Parse secrets/.env.local
    secrets_vars: dict[str, str] = {}
    for line in env_local.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            secrets_vars[key.strip()] = value.strip()

    # Merge (don't overwrite existing)
    merged = {**secrets_vars, **existing}

    # Write back
    lines = [f"{k}={v}" for k, v in sorted(merged.items())]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    new_keys = set(secrets_vars.keys()) - set(existing.keys())
    print(f"Merged {len(new_keys)} new secret(s) into .env")
    for key in sorted(new_keys):
        print(f"  + {key}")


if __name__ == "__main__":
    main()
