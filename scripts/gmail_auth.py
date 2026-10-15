"""One-time Gmail OAuth setup: prints the .env lines for headless Gmail access.

The server never opens a browser. Run this once on a machine with a browser, then copy the printed
lines into .env (and into your deployment's secrets). Re-run only if Google revokes the token.

    python scripts/gmail_auth.py                   # browser consent using credentials.json
    python scripts/gmail_auth.py --from-token-json # reuse the refresh token already in token.json

The output contains secrets: paste it into .env only, never into chat, tickets or git.
"""
import argparse
import json
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def _from_browser_consent(credentials_path: Path) -> dict:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not credentials_path.exists():
        sys.exit(f"{credentials_path} not found. Download the OAuth client (Desktop app) JSON from Google Cloud Console.")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    # offline + consent guarantees Google returns a refresh token even if the app was authorized before.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    return {"client_id": creds.client_id, "client_secret": creds.client_secret, "refresh_token": creds.refresh_token}


def _from_token_json(token_path: Path) -> dict:
    if not token_path.exists():
        sys.exit(f"{token_path} not found. Run without --from-token-json to sign in.")
    data = json.loads(token_path.read_text(encoding="utf-8"))
    return {k: data.get(k, "") for k in ("client_id", "client_secret", "refresh_token")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-token-json", action="store_true", help="read the refresh token from token.json")
    parser.add_argument("--credentials", default="credentials.json", help="OAuth client secrets file")
    parser.add_argument("--token", default="token.json", help="authorized-user token file")
    args = parser.parse_args()

    values = _from_token_json(Path(args.token)) if args.from_token_json else _from_browser_consent(Path(args.credentials))
    if not values.get("refresh_token"):
        sys.exit("Google did not return a refresh token. Re-run without --from-token-json to force consent.")

    print("\n# Add these lines to .env (secrets: do not commit or share)")
    print(f"GMAIL_CLIENT_ID={values['client_id']}")
    print(f"GMAIL_CLIENT_SECRET={values['client_secret']}")
    print(f"GMAIL_REFRESH_TOKEN={values['refresh_token']}")


if __name__ == "__main__":
    main()
