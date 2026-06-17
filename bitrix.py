"""Cliente do Bitrix24 via webhook REST.

Responsável por:
- chamar métodos REST com paginação automática (start/next, 50 por página);
- baixar Leads e Deals com os campos necessários para os KPIs;
- baixar metadados (estágios, status de lead, fontes, usuários) e montar
  dicionários de tradução de ID -> nome legível.

O escopo `user` é opcional: se o webhook não tiver permissão, os nomes dos
vendedores caem para "ID {n}" sem quebrar o app.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests


class BitrixError(RuntimeError):
    pass


class BitrixClient:
    # Bitrix aceita ~1 requisição a cada 0,5s. Espaçamos todas as chamadas.
    MIN_INTERVAL = 0.5

    def __init__(self, webhook: str, timeout: int = 30, min_interval: float = MIN_INTERVAL):
        # garante exatamente uma barra no final
        self.base = webhook.rstrip("/") + "/"
        self.timeout = timeout
        self.min_interval = min_interval
        self.session = requests.Session()
        self._last_call = 0.0

    def _throttle(self) -> None:
        """Garante o intervalo mínimo entre requisições (rate limit do Bitrix)."""
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Executa um método REST único e devolve o JSON completo."""
        self._throttle()
        url = self.base + method + ".json"
        resp = self.session.post(url, json=params or {}, timeout=self.timeout)
        try:
            data = resp.json()
        except ValueError:
            raise BitrixError(f"Resposta inválida de {method}: {resp.text[:200]}")
        if "error" in data:
            raise BitrixError(f"{method}: {data.get('error')} - {data.get('error_description')}")
        return data

    def call_list(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        max_pages: int = 200,
    ) -> List[Dict[str, Any]]:
        """Executa um método de listagem com paginação automática.

        O Bitrix devolve no máximo 50 itens por página e o índice da próxima
        página no campo `next`. Iteramos até esgotar.
        """
        params = dict(params or {})
        results: List[Dict[str, Any]] = []
        start = 0
        for _ in range(max_pages):
            params["start"] = start
            data = self.call(method, params)
            batch = data.get("result", []) or []
            results.extend(batch)
            nxt = data.get("next")
            if nxt is None:
                break
            start = nxt
        return results

    # ---------- Dados principais ----------

    def get_deals(
        self,
        category_id: Optional[str] = None,
        modified_since: Optional[str] = None,
        extra_select: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        select = [
            "ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "OPPORTUNITY",
            "CURRENCY_ID", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY",
            "BEGINDATE", "CLOSEDATE", "CLOSED", "SOURCE_ID",
        ] + list(extra_select or [])
        flt: Dict[str, Any] = {}
        if category_id is not None:
            flt["CATEGORY_ID"] = category_id
        if modified_since:
            flt[">=DATE_MODIFY"] = modified_since
        params: Dict[str, Any] = {"select": select, "order": {"DATE_MODIFY": "ASC"}}
        if flt:
            params["filter"] = flt
        return self.call_list("crm.deal.list", params)

    def get_leads(
        self,
        modified_since: Optional[str] = None,
        extra_select: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        select = [
            "ID", "TITLE", "STATUS_ID", "STATUS_SEMANTIC_ID", "OPPORTUNITY",
            "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY", "SOURCE_ID",
        ] + list(extra_select or [])
        params: Dict[str, Any] = {"select": select, "order": {"DATE_MODIFY": "ASC"}}
        if modified_since:
            params["filter"] = {">=DATE_MODIFY": modified_since}
        return self.call_list("crm.lead.list", params)

    def get_meetings(self, entity_type_id: int = 1050) -> List[Dict[str, Any]]:
        """Itens de um SPA (Smart Process), ex.: reuniões. Resposta vem em
        result.items (formato diferente das listas clássicas). Sempre carga
        completa (volume pequeno)."""
        results: List[Dict[str, Any]] = []
        start = 0
        for _ in range(200):
            data = self.call("crm.item.list", {"entityTypeId": entity_type_id, "start": start})
            items = (data.get("result") or {}).get("items", []) or []
            results.extend(items)
            nxt = data.get("next")
            if nxt is None:
                break
            start = nxt
        return results

    # ---------- Metadados de campos personalizados ----------

    def get_fields(self, entity: str) -> Dict[str, Any]:
        """entity: 'lead' ou 'deal'. Devolve o dicionário de campos."""
        return self.call(f"crm.{entity}.fields", {}).get("result", {})

    @staticmethod
    def enum_maps(fields_meta: Dict[str, Any], codes: List[str]) -> Dict[str, Dict[str, str]]:
        """Para cada código de campo enumeration, devolve {ID: VALUE}."""
        out: Dict[str, Dict[str, str]] = {}
        for code in codes:
            info = fields_meta.get(code) or {}
            if info.get("items"):
                out[code] = {str(o["ID"]): o["VALUE"] for o in info["items"]}
        return out

    # ---------- Metadados (dicionários de tradução) ----------

    def get_status_map(self) -> Dict[str, Dict[str, str]]:
        """Devolve {ENTITY_ID: {STATUS_ID: NAME}} para todos os status/estágios."""
        rows = self.call_list("crm.status.list", {})
        out: Dict[str, Dict[str, str]] = {}
        for r in rows:
            out.setdefault(r["ENTITY_ID"], {})[r["STATUS_ID"]] = r["NAME"]
        return out

    def get_user_map(self) -> Dict[str, str]:
        """Devolve {ID: 'Nome Sobrenome'}. Vazio se faltar escopo `user`."""
        try:
            rows = self.call_list("user.get", {})
        except BitrixError:
            return {}
        out: Dict[str, str] = {}
        for u in rows:
            nome = " ".join(p for p in [u.get("NAME"), u.get("LAST_NAME")] if p).strip()
            out[str(u.get("ID"))] = nome or f"ID {u.get('ID')}"
        return out

    def get_categories(self) -> Dict[str, str]:
        """Devolve {CATEGORY_ID: NAME} dos funis de Deal."""
        rows = self.call_list("crm.dealcategory.list", {"select": ["ID", "NAME"]})
        return {str(r["ID"]): r["NAME"] for r in rows}
