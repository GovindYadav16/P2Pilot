"""Load synthetic data into the local Supabase stack.

    python -m p2pilot.data_gen.seed

Creates demo users through the Supabase Auth admin API, then (re)loads the two demo
tenants. Safe to re-run: the demo tenants are deleted and recreated each time.
Ground truth for planted anomalies is written to data/ground_truth.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import psycopg

from p2pilot.config import REPO_ROOT, get_settings
from p2pilot.data_gen.generate import Dataset, generate

ROLES = ["ap_clerk", "approver", "auditor", "tenant_admin"]
EMAIL_PREFIX = {"ap_clerk": "clerk", "approver": "approver", "auditor": "auditor",
                "tenant_admin": "admin"}


def ensure_user(client: httpx.Client, base: str, email: str, password: str) -> str:
    resp = client.post(f"{base}/auth/v1/admin/users",
                       json={"email": email, "password": password, "email_confirm": True})
    if resp.status_code in (200, 201):
        return resp.json()["id"]
    # Already exists: look it up.
    page = client.get(f"{base}/auth/v1/admin/users", params={"per_page": 1000})
    page.raise_for_status()
    for user in page.json().get("users", []):
        if user.get("email") == email:
            return user["id"]
    raise RuntimeError(f"Could not create or find user {email}: {resp.status_code} {resp.text}")


def insert_rows(cur: psycopg.Cursor, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    sql = (f"insert into public.{table} ({', '.join(cols)}) "
           f"values ({', '.join(['%s'] * len(cols))})")
    cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def load(ds: Dataset, db_url: str, user_ids: dict[tuple[str, str], str]) -> None:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("delete from public.tenants where slug = any(%s)",
                    ([t["slug"] for t in ds.tenants],))
        insert_rows(cur, "tenants", ds.tenants)
        memberships = [
            {"tenant_id": t["id"], "user_id": user_ids[(t["slug"], role)], "role": role}
            for t in ds.tenants for role in ROLES
        ]
        insert_rows(cur, "memberships", memberships)
        for table in ["vendors", "purchase_orders", "po_lines", "goods_receipts", "grn_lines"]:
            insert_rows(cur, table, getattr(ds, table))
        conn.commit()


def main() -> None:
    # Defaults come from the environment or the repo's .env (same as the API).
    cfg = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-url", default=cfg.p2pilot_db_url
                   or "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
    p.add_argument("--supabase-url", default=cfg.supabase_url)
    p.add_argument("--service-key", default=cfg.supabase_service_role_key)
    p.add_argument("--password", default=cfg.seed_user_password)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default=str(REPO_ROOT / "data" / "ground_truth.json"))
    args = p.parse_args()
    if not args.service_key:
        raise SystemExit("Set SUPABASE_SERVICE_ROLE_KEY in .env (the Secret key from `supabase status`).")

    ds = generate(seed=args.seed)

    # Legacy service_role keys are JWTs and go in both headers; newer `sb_secret_...` keys
    # are not JWTs and go only in `apikey`.
    headers = {"apikey": args.service_key}
    if not args.service_key.startswith("sb_"):
        headers["Authorization"] = f"Bearer {args.service_key}"
    user_ids: dict[tuple[str, str], str] = {}
    with httpx.Client(headers=headers, timeout=20) as client:
        for t in ds.tenants:
            for role in ROLES:
                email = f"{EMAIL_PREFIX[role]}@{t['slug']}.test"
                user_ids[(t["slug"], role)] = ensure_user(client, args.supabase_url, email, args.password)

    load(ds, args.db_url, user_ids)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ds.ground_truth, indent=2))
    print(f"Seeded {len(ds.tenants)} tenants, {len(ds.vendors)} vendors, "
          f"{len(ds.purchase_orders)} POs, {len(ds.po_lines)} PO lines, "
          f"{len(ds.goods_receipts)} goods receipts.")
    print(f"Users: clerk|approver|auditor|admin @acme.test / @bharat.test  password: {args.password}")
    print(f"Ground truth written to {out.resolve()}")


if __name__ == "__main__":
    main()
