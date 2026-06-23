"""Sincronização incremental Bitrix -> SQLite.

Regra (definida com o cliente):
- a cada execução (agendada de hora em hora) busca registros com DATE_MODIFY
  nas últimas 1h30 (janela de 30 min de folga garante que nada se perca entre
  execuções de 1h);
- o upsert por (TENANT_ID, ID) torna re-buscas idempotentes;
- na primeira vez de um tenant (cache vazio) faz uma carga completa (backfill).

Uso:
    python sync.py                # incremental em todos os tenants ativos
    python sync.py --full         # carga completa em todos
    python sync.py --tenant 1     # apenas um tenant
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import db
from bitrix import BitrixClient, BitrixError

WINDOW_HOURS = 1.5

# Mapa padrão de campos personalizados (portal Coontrol). Cada tenant pode ter o
# seu próprio mapa salvo (db.set_field_map); este é o fallback.
DEFAULT_FIELD_MAP = {
    "lead": {
        "segmento": "UF_CRM_1761827705633",
        "cargo": "POST",                       # campo padrão (texto livre)
        "motivo": "UF_CRM_1761828042253",      # motivo de desqualificação
    },
    "deal": {
        "segmento": "UF_CRM_1753417862",
        "motivo": "UF_CRM_1769452594193",      # motivo de perda/fechamento
    },
    # SPAs (Smart Processes) acompanhados neste ambiente
    "spas": [
        {"entity_type_id": 1050, "label": "Reuniões", "icon": "🤝"},
    ],
}


def _spa_list(fmap: dict) -> list:
    """Lê a lista de SPAs do mapa, com compatibilidade ao formato antigo."""
    spas = fmap.get("spas")
    if spas:
        return spas
    et = fmap.get("meetings_entity_type_id")
    return [{"entity_type_id": et, "label": "Reuniões", "icon": "🤝"}] if et else []


def _cutoff_iso(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _product_rows(raw):
    """Normaliza linhas de produto do Bitrix para o formato do banco."""
    out = []
    for r in raw:
        price = float(r.get("PRICE") or 0)
        qty = float(r.get("QUANTITY") or 0)
        out.append({
            "ID": str(r.get("ID")), "DEAL_ID": str(r.get("OWNER_ID")),
            "PRODUCT_ID": str(r.get("PRODUCT_ID")), "PRODUCT_NAME": r.get("PRODUCT_NAME"),
            "PRICE": price, "QUANTITY": qty, "TOTAL": price * qty,
        })
    return out


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "y", "true", "sim", "yes")


def enrich_deals(deals, fmap, deal_enums, stage_name_map, camp_map, company_estado=None):
    """Resolve dimensões extras por negócio e grava em d['EXTRA'] (JSON),
    além de SEGMENTO (via Objetivo) e Status do Cartão (Lead vs Negócio).
    Genérico: tudo é dirigido pelo FIELD_MAP do tenant."""
    dims = fmap.get("deal_dims", [])
    seg_obj = fmap.get("segmento_objetivo")
    card = fmap.get("card_status")
    estado_field = fmap.get("estado_company_field")
    company_estado = company_estado or {}
    for d in deals:
        extra = {}
        if estado_field:
            extra["Estado"] = company_estado.get(str(d.get("COMPANY_ID"))) or None
        for dim in dims:
            raw = d.get(dim["field"])
            t = dim.get("type", "string")
            if raw in (None, "", [], "0", 0):
                val = None
            elif t == "enum":
                val = _resolve(raw, deal_enums.get(dim["field"]))
            elif t == "bool":
                val = "Sim" if _truthy(raw) else "Não"
            elif t == "stage":
                val = stage_name_map.get(raw, raw)
            elif t == "campaign_spa":
                val = camp_map.get(str(raw), raw)
            elif t == "number":
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    val = None
            else:
                val = raw
            extra[dim["key"]] = val
        # segmento via Objetivo Inicial (Grupo A/B)
        if seg_obj:
            obj = str(d.get(seg_obj) or "").lower()
            if "consumo próprio" in obj or "consumo proprio" in obj or "corporativo" in obj:
                d["SEGMENTO"] = "Grupo A"
            elif "revenda" in obj or "distribuidor" in obj:
                d["SEGMENTO"] = "Grupo B"
            else:
                d["SEGMENTO"] = None
        # Status do Cartão (CRM com lead e negócio misturados nas fases)
        if card:
            stage = stage_name_map.get(d.get("STAGE_ID"), d.get("STAGE_ID"))
            qual = _truthy(d.get(card["qual_field"]))
            fant = extra.get(card.get("fase_anterior_key", "Fase Anterior"))
            won, lost, qual_st = card["won"], card["lost"], card["qualified"]
            if stage in won:
                cs = "Negócio Ganho"
            elif stage in lost and qual:
                cs = "Negócio (Lead Qualificado)"
            elif stage in lost:
                cs = "Lead Desqualificado"
            elif stage in qual_st or fant in qual_st:
                cs = "Negócio (Lead Qualificado)"
            else:
                cs = "Lead"
            extra["Status do Cartão"] = cs
        d["EXTRA"] = json.dumps(extra, ensure_ascii=False)
    return deals


def sync_products(tenant: dict) -> int:
    """Busca/atualiza as linhas de produto de todos os negócios já no banco."""
    tid = tenant["ID"]
    client = BitrixClient(tenant["WEBHOOK"])
    ids = db.deal_ids(tid)
    rows = _product_rows(client.get_deal_productrows(ids)) if ids else []
    db.replace_products(tid, ids, rows)
    return len(rows)


def _resolve(value, enum_map):
    """Converte ID(s) de enumeração em rótulo(s). Mantém texto livre como está."""
    if value in (None, "", 0, "0", [], "[]"):
        return None
    if isinstance(value, list):
        return ", ".join(str(enum_map.get(str(v), v)) for v in value) if enum_map else \
               ", ".join(str(v) for v in value)
    return enum_map.get(str(value), value) if enum_map else value


def sync_tenant(tenant: dict, full: bool = False, window_hours: float = WINDOW_HOURS,
                created_since: Optional[str] = None) -> dict:
    tid = tenant["ID"]
    client = BitrixClient(tenant["WEBHOOK"])
    fmap = db.get_field_map(tid) or DEFAULT_FIELD_MAP
    lmap = fmap.get("lead", {})
    dmap = fmap.get("deal", {})

    deal_dims = fmap.get("deal_dims", [])
    card = fmap.get("card_status")
    seg_obj = fmap.get("segmento_objetivo")
    # códigos UF extras a selecionar nos negócios
    extra_deal_codes = list(dmap.values()) + [d["field"] for d in deal_dims]
    if seg_obj:
        extra_deal_codes.append(seg_obj)
    if card:
        extra_deal_codes.append(card["qual_field"])
    extra_deal_codes = list(dict.fromkeys(c for c in extra_deal_codes if c))

    # metadados (pequenos) — sempre atualiza para refletir novos estágios/usuários
    try:
        status_map = client.get_status_map()
        user_map = client.get_user_map()
        categories = client.get_categories()
        db.save_meta(tid, status_map, user_map, categories)
        # mapas de enumeração dos campos personalizados (para traduzir IDs -> texto)
        lead_fields = client.get_fields("lead")
        deal_fields = client.get_fields("deal")
        lead_enums = client.enum_maps(lead_fields, [v for v in lmap.values()])
        enum_codes = [v for v in dmap.values()] + [d["field"] for d in deal_dims if d.get("type") == "enum"]
        deal_enums = client.enum_maps(deal_fields, enum_codes)
    except BitrixError as e:
        return {"tenant": tenant["NAME"], "ok": False, "error": str(e)}

    # mapa de estágios (para Fase Anterior e Status do Cartão) e de campanhas (SPA)
    stage_name_map = (status_map.get(f"DEAL_STAGE_{tenant['SALES_CATEGORY_ID']}")
                      or status_map.get("DEAL_STAGE", {}))
    camp_map = {}
    camp_dim = next((d for d in deal_dims if d.get("type") == "campaign_spa"), None)
    if camp_dim:
        try:
            items = client.get_spa_items(camp_dim["spa_entity"])
            cf = camp_dim["spa_code_field"]
            camp_map = {str(it.get(cf)): it.get("title") for it in items if it.get(cf)}
        except BitrixError:
            camp_map = {}

    have = db.count_records(tid)
    first_load = (have["deals"] == 0 and have["leads"] == 0)
    # created_since: carga limitada por data de criação (ex.: só mês passado e atual)
    if created_since:
        modified_since = None
    else:
        modified_since = None if (full or first_load) else _cutoff_iso(window_hours)

    spas = _spa_list(fmap)
    try:
        deals = client.get_deals(category_id=tenant["SALES_CATEGORY_ID"],
                                 modified_since=modified_since, created_since=created_since,
                                 extra_select=extra_deal_codes)
        leads = client.get_leads(modified_since=modified_since, created_since=created_since,
                                 extra_select=list(lmap.values()))
        spa_raw = {s["entity_type_id"]: client.get_spa_items(s["entity_type_id"], created_since=created_since)
                   for s in spas}
    except BitrixError as e:
        return {"tenant": tenant["NAME"], "ok": False, "error": str(e)}

    for d in deals:
        d["SEGMENTO"] = _resolve(d.get(dmap.get("segmento")), deal_enums.get(dmap.get("segmento")))
        d["MOTIVO"] = _resolve(d.get(dmap.get("motivo")), deal_enums.get(dmap.get("motivo")))
    # Estado (campo na empresa) — resolve company_id -> estado para os negócios carregados
    company_estado = {}
    estado_field = fmap.get("estado_company_field")
    if estado_field:
        cids = list({str(d.get("COMPANY_ID")) for d in deals if d.get("COMPANY_ID")})
        try:
            company_estado = client.get_companies_field(cids, estado_field)
        except BitrixError:
            company_estado = {}
    # dimensões extras + segmento por objetivo + Status do Cartão (dirigido pelo FIELD_MAP)
    if deal_dims or card or seg_obj or estado_field:
        enrich_deals(deals, fmap, deal_enums, stage_name_map, camp_map, company_estado)
    for l in leads:
        l["SEGMENTO"] = _resolve(l.get(lmap.get("segmento")), lead_enums.get(lmap.get("segmento")))
        l["CARGO"] = _resolve(l.get(lmap.get("cargo")), lead_enums.get(lmap.get("cargo")))
        l["MOTIVO"] = _resolve(l.get(lmap.get("motivo")), lead_enums.get(lmap.get("motivo")))

    nd = db.upsert_deals(tid, deals)
    nl = db.upsert_leads(tid, leads)
    # linhas de produto dos negócios que vieram nesta sincronização
    deal_ids_synced = [str(d.get("ID")) for d in deals]
    npr = 0
    if fmap.get("quote_products"):
        # produtos vêm dos orçamentos (crm.quote) ligados aos negócios
        try:
            quotes = client.get_quotes_for_deals(deal_ids_synced) if deal_ids_synced else []
            q2d = {str(q["ID"]): str(q["DEAL_ID"]) for q in quotes}
            raw = client.get_quote_productrows(list(q2d.keys())) if q2d else []
            rows = []
            for r in raw:
                price = float(r.get("PRICE") or 0)
                qty = float(r.get("QUANTITY") or 0)
                rows.append({"ID": str(r.get("ID")), "DEAL_ID": q2d.get(str(r.get("OWNER_ID"))),
                             "PRODUCT_ID": str(r.get("PRODUCT_ID")), "PRODUCT_NAME": r.get("PRODUCT_NAME"),
                             "PRICE": price, "QUANTITY": qty, "TOTAL": price * qty})
            rows = [r for r in rows if r["DEAL_ID"]]
            db.replace_products(tid, deal_ids_synced, rows)
            npr = len(rows)
        except BitrixError:
            npr = 0
    elif not fmap.get("skip_deal_products"):
        try:
            prod_rows = _product_rows(client.get_deal_productrows(deal_ids_synced)) if deal_ids_synced else []
            db.replace_products(tid, deal_ids_synced, prod_rows)
            npr = len(prod_rows)
        except BitrixError:
            npr = 0
    ns = 0
    for et, items in spa_raw.items():
        rows = [{
            "ID": str(m.get("id")), "TITLE": m.get("title"), "STAGE_ID": m.get("stageId"),
            "CATEGORY_ID": str(m.get("categoryId")), "ASSIGNED_BY_ID": str(m.get("assignedById")),
            "SOURCE_ID": m.get("sourceId"), "CREATED_TIME": m.get("createdTime"),
            "BEGINDATE": m.get("begindate"), "OPPORTUNITY": m.get("opportunity"),
        } for m in items]
        ns += db.upsert_spa_items(tid, et, rows)

    total = db.count_records(tid)
    if created_since:
        mode = f"desde {created_since[:10]}"
    else:
        mode = "completa" if modified_since is None else f"incremental({window_hours}h)"
    db.set_sync(tid, total["deals"], total["leads"],
                note=f"{mode}: +{nd} deals, +{nl} leads, {ns} itens SPA, {npr} produtos")
    return {
        "tenant": tenant["NAME"], "ok": True, "mode": mode,
        "deals_upsert": nd, "leads_upsert": nl, "spa_upsert": ns, "products_upsert": npr,
        "deals_total": total["deals"], "leads_total": total["leads"],
        "spa_total": total["spa"],
    }


def sync_all(full: bool = False, tenant_id: Optional[int] = None) -> list:
    db.init_db()
    tenants = [db.get_tenant(tenant_id)] if tenant_id else db.list_tenants(active_only=True)
    results = []
    for t in tenants:
        if not t:
            continue
        results.append(sync_tenant(t, full=full))
    return results


def main():
    ap = argparse.ArgumentParser(description="Sincroniza Bitrix24 -> SQLite")
    ap.add_argument("--full", action="store_true", help="carga completa (ignora janela)")
    ap.add_argument("--tenant", type=int, default=None, help="ID de um tenant específico")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] Iniciando sync (full={args.full}, tenant={args.tenant})")
    for r in sync_all(full=args.full, tenant_id=args.tenant):
        if r["ok"]:
            print(f"  [OK] {r['tenant']}: {r['mode']} | +{r['deals_upsert']} deals "
                  f"(+{r['leads_upsert']} leads, {r['spa_upsert']} itens SPA) | totais: "
                  f"{r['deals_total']} deals, {r['leads_total']} leads, {r['spa_total']} itens SPA")
        else:
            print(f"  [ERRO] {r['tenant']}: {r['error']}")


if __name__ == "__main__":
    main()
