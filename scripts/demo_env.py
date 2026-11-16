import os
from pathlib import Path
from secrets import token_hex


path = Path(__file__).resolve().parent.parent / ".env"
names = ("MYSQL_PASSWORD", "PROCUREFLOW_BUYER_TOKEN", "PROCUREFLOW_APPROVER_TOKEN", "PROCUREFLOW_ERP_TOKEN")
try:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env already exists and was not overwritten.") from None

try:
    environment = os.fdopen(descriptor, "w", encoding="utf-8")
except BaseException:
    os.close(descriptor)
    raise

with environment:
    for name in names:
        environment.write(f"{name}={token_hex(24)}\n")

print("Created .env with unique local demo tokens.")
