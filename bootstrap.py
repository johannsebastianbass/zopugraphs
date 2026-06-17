"""Setup automatizado do painel ZOPU (idempotente).

Lê configuração de um arquivo .env (ou variáveis de ambiente) e:
  1. cria o schema do banco;
  2. cria o usuário master (se ainda não existir);
  3. cria o ambiente/tenant (se ainda não existir) + mapa de campos padrão;
  4. cria um usuário cliente opcional;
  5. roda a carga completa do Bitrix para o ambiente.

Pode ser rodado quantas vezes quiser: o que já existe é preservado.

Uso:
    cp .env.example .env   # e preencha
    python bootstrap.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import auth
import db
from sync import DEFAULT_FIELD_MAP, sync_tenant


def load_env(path: str = ".env") -> None:
    """Carrega KEY=VALUE de um .env simples para os.environ (sem sobrescrever)."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def main() -> int:
    load_env()
    db.init_db()

    master_user = os.environ.get("MASTER_USER", "master").strip()
    master_pass = os.environ.get("MASTER_PASS", "").strip()
    tenant_name = os.environ.get("TENANT_NAME", "").strip()
    webhook = os.environ.get("BITRIX_WEBHOOK", "").strip()
    category = os.environ.get("SALES_CATEGORY_ID", "16").strip() or "16"
    client_user = os.environ.get("CLIENT_USER", "").strip()
    client_pass = os.environ.get("CLIENT_PASS", "").strip()

    # 1) usuário master
    if db.get_user(master_user):
        print(f"[=] Usuario master '{master_user}' ja existe.")
    elif master_pass:
        auth.create_user(master_user, master_pass, role="master", tenant_id=None, name="ZOPU Master")
        print(f"[+] Usuario master criado: {master_user}")
    else:
        print("[!] MASTER_PASS vazio e master inexistente. Defina MASTER_PASS no .env.")
        return 1

    # 2) ambiente/tenant
    tenant = None
    if tenant_name:
        existing = [t for t in db.list_tenants(active_only=False) if t["NAME"] == tenant_name]
        if existing:
            tenant = existing[0]
            print(f"[=] Ambiente '{tenant_name}' ja existe (ID {tenant['ID']}).")
        elif webhook:
            tid = db.add_tenant(tenant_name, webhook, category)
            db.set_field_map(tid, DEFAULT_FIELD_MAP)
            tenant = db.get_tenant(tid)
            print(f"[+] Ambiente criado: ID {tid} | {tenant_name} (mapa de campos padrao aplicado)")
        else:
            print("[!] TENANT_NAME definido mas BITRIX_WEBHOOK vazio. Pulei a criacao do ambiente.")
    else:
        print("[=] TENANT_NAME vazio: nenhum ambiente criado nesta execucao.")

    # 3) usuário cliente opcional
    if client_user and client_pass and tenant:
        if db.get_user(client_user):
            print(f"[=] Usuario cliente '{client_user}' ja existe.")
        else:
            auth.create_user(client_user, client_pass, role="client",
                             tenant_id=tenant["ID"], name=client_user)
            print(f"[+] Usuario cliente criado: {client_user} -> {tenant['NAME']}")

    # 4) carga completa
    if tenant:
        print(f"[*] Sincronizando '{tenant['NAME']}' (carga completa)...")
        r = sync_tenant(tenant, full=True)
        if r.get("ok"):
            print(f"[+] OK: {r['deals_total']} deals, {r['leads_total']} leads, "
                  f"{r['meetings_total']} reunioes.")
        else:
            print(f"[!] Erro na sincronizacao: {r.get('error')}")
            return 1

    print("\nSetup concluido. Rode:  streamlit run app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
