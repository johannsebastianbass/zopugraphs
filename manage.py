"""CLI de administração do painel ZOPU.

Exemplos:
    python manage.py init --master-pass SENHA          # cria schema + usuário master
    python manage.py add-tenant --name "Coontrol" --webhook https://.../rest/36/token/
    python manage.py add-user --username joao --password 123 --role client --tenant 1
    python manage.py passwd --username master --password NOVA
    python manage.py list
"""

from __future__ import annotations

import argparse

import auth
import db


def cmd_init(args):
    db.init_db()
    if db.get_user(args.master_user):
        print(f"Usuário master '{args.master_user}' já existe.")
    else:
        auth.create_user(args.master_user, args.master_pass, role="master",
                         tenant_id=None, name="ZOPU Master")
        print(f"Usuário master criado: {args.master_user}")
    print("Banco inicializado:", db.DB_PATH)


def cmd_add_tenant(args):
    db.init_db()
    tid = db.add_tenant(args.name, args.webhook, args.category)
    print(f"Tenant criado: ID={tid} | {args.name}")


def cmd_add_user(args):
    db.init_db()
    tid = args.tenant
    auth.create_user(args.username, args.password, role=args.role,
                     tenant_id=tid, name=args.name or args.username)
    print(f"Usuário criado: {args.username} (role={args.role}, tenant={tid})")


def cmd_passwd(args):
    if not db.get_user(args.username):
        print("Usuário não encontrado.")
        return
    auth.change_password(args.username, args.password)
    print(f"Senha alterada para {args.username}")


def cmd_list(args):
    db.init_db()
    print("== Tenants ==")
    for t in db.list_tenants(active_only=False):
        flag = "" if t["ACTIVE"] else " (inativo)"
        print(f"  [{t['ID']}] {t['NAME']} -> {t['WEBHOOK']} (cat {t['SALES_CATEGORY_ID']}){flag}")
    print("== Usuários ==")
    for u in db.list_users():
        flag = "" if u["ACTIVE"] else " (inativo)"
        print(f"  {u['USERNAME']} | role={u['ROLE']} | tenant={u['TENANT_ID']}{flag}")


def main():
    ap = argparse.ArgumentParser(description="Administração do painel ZOPU")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--master-user", default="master")
    p.add_argument("--master-pass", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add-tenant")
    p.add_argument("--name", required=True)
    p.add_argument("--webhook", required=True)
    p.add_argument("--category", default="16")
    p.set_defaults(func=cmd_add_tenant)

    p = sub.add_parser("add-user")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--role", choices=["master", "client"], default="client")
    p.add_argument("--tenant", type=int, default=None)
    p.add_argument("--name", default="")
    p.set_defaults(func=cmd_add_user)

    p = sub.add_parser("passwd")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
