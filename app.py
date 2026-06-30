"""Painel Comercial multi-empresa ZOPU — Bitrix24 (Leads & Deals).

Login -> seleção de ambiente (master vê todos) -> dashboard lido do SQLite.
Inclui metas/quota por vendedor e comparação mês a mês.

Execute com:  streamlit run app.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import auth
import db
from sync import sync_tenant, _spa_list

LOGO = str(Path(__file__).parent / "assets" / "fluidz_logo.svg")
APP_NAME = "Fluidz Graphs"

st.set_page_config(page_title=APP_NAME, page_icon=LOGO, layout="wide")
st.logo(LOGO, size="large")

PALETTE = px.colors.qualitative.Bold
WON_COLOR, LOST_COLOR, OPEN_COLOR, META_COLOR = "#16a34a", "#dc2626", "#2563eb", "#f59e0b"
MESES_PT = ["", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

db.init_db()


# ====================================================================== utils
def fmt_brl(v) -> str:
    try:
        s = f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "R$ 0,00"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def fmt_int(v) -> str:
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def to_dt(series: pd.Series) -> pd.Series:
    s = pd.to_datetime(series, errors="coerce", utc=True)
    return s.dt.tz_convert("America/Sao_Paulo").dt.tz_localize(None)


def build_deals_df(df, status_map, user_map, category_id, won_stages=None) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["OPPORTUNITY"] = pd.to_numeric(df["OPPORTUNITY"], errors="coerce").fillna(0.0)
    df["DATE_CREATE"] = to_dt(df["DATE_CREATE"])
    df["CLOSEDATE"] = to_dt(df["CLOSEDATE"])
    df["ASSIGNED_BY_ID"] = df["ASSIGNED_BY_ID"].astype(str)
    df["Vendedor"] = df["ASSIGNED_BY_ID"].map(user_map).fillna("ID " + df["ASSIGNED_BY_ID"])
    # estágio resolvido por categoria do negócio (suporta múltiplos pipelines)
    df["CATEGORY_ID"] = df["CATEGORY_ID"].astype(str)
    df["Estágio"] = df["STAGE_ID"]
    for cat in df["CATEGORY_ID"].unique():
        m = status_map.get(f"DEAL_STAGE_{cat}") or status_map.get("DEAL_STAGE", {})
        mask = df["CATEGORY_ID"] == cat
        df.loc[mask, "Estágio"] = df.loc[mask, "STAGE_ID"].map(m).fillna(df.loc[mask, "STAGE_ID"])
    src = status_map.get("SOURCE", {})
    df["Fonte"] = df["SOURCE_ID"].map(src).fillna(df["SOURCE_ID"]).fillna("—")

    def situacao(stage: str) -> str:
        if str(stage).endswith(":WON") or stage == "WON":
            return "Ganho"
        if str(stage).endswith(":LOSE") or stage == "LOSE":
            return "Perdido"
        return "Aberto"

    df["Situação"] = df["STAGE_ID"].apply(situacao)
    # estágios extras tratados como ganho (ex.: TS Shara conta "Excluídos SSA")
    if won_stages:
        df.loc[df["Estágio"].isin(won_stages), "Situação"] = "Ganho"
    # data de finalização real = última modificação dos negócios fechados
    # (o CLOSEDATE do Bitrix costuma ser uma data PREVISTA, às vezes anterior à criação)
    df["DATE_MODIFY"] = to_dt(df.get("DATE_MODIFY"))
    closed = df["Situação"].isin(["Ganho", "Perdido"])
    df["Fechamento"] = df["DATE_MODIFY"].where(closed)
    df["Ciclo (dias)"] = (df["Fechamento"] - df["DATE_CREATE"]).dt.days.clip(lower=0)
    df["Mês criação"] = df["DATE_CREATE"].dt.to_period("M").astype(str)
    df["Mês fechamento"] = df["Fechamento"].dt.to_period("M").astype(str)
    # dimensões extras (JSON em EXTRA) viram colunas: Canal, Campanha, Status do Cartão...
    if "EXTRA" in df.columns:
        parsed = df["EXTRA"].apply(lambda s: json.loads(s) if isinstance(s, str) and s else {})
        keys = set()
        for d in parsed:
            keys.update(d.keys())
        for k in keys:
            df[k] = parsed.apply(lambda d: d.get(k))
    return df


def build_leads_df(df, status_map, user_map) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["DATE_CREATE"] = to_dt(df["DATE_CREATE"])
    df["ASSIGNED_BY_ID"] = df["ASSIGNED_BY_ID"].astype(str)
    df["Vendedor"] = df["ASSIGNED_BY_ID"].map(user_map).fillna("ID " + df["ASSIGNED_BY_ID"])
    df["Status"] = df["STATUS_ID"].map(status_map.get("STATUS", {})).fillna(df["STATUS_ID"])
    src = status_map.get("SOURCE", {})
    df["Fonte"] = df["SOURCE_ID"].map(src).fillna(df["SOURCE_ID"]).fillna("—")
    sem = df.get("STATUS_SEMANTIC_ID", pd.Series(index=df.index, dtype=str)).fillna("")
    df["Convertido"] = (df["STATUS_ID"] == "CONVERTED") | (sem == "S")
    df["Desqualificado"] = (df["STATUS_ID"] == "JUNK") | (sem == "F")
    df["Mês criação"] = df["DATE_CREATE"].dt.to_period("M").astype(str)
    return df


def build_spa_df(df, status_map, user_map, entity_type_id) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["OPPORTUNITY"] = pd.to_numeric(df.get("OPPORTUNITY"), errors="coerce").fillna(0.0)
    df["DATE_CREATE"] = to_dt(df["CREATED_TIME"])
    df["ASSIGNED_BY_ID"] = df["ASSIGNED_BY_ID"].astype(str)
    df["Vendedor"] = df["ASSIGNED_BY_ID"].map(user_map).fillna("ID " + df["ASSIGNED_BY_ID"])
    df["Fonte"] = df["SOURCE_ID"].map(status_map.get("SOURCE", {})).fillna(df["SOURCE_ID"]).fillna("—")
    smap = {}
    for k, v in status_map.items():
        if k.startswith(f"DYNAMIC_{entity_type_id}_STAGE_"):
            smap.update(v)
    df["Estágio"] = df["STAGE_ID"].map(smap).fillna(df["STAGE_ID"])

    def sit(s):
        s = str(s)
        if s.endswith(":SUCCESS"):
            return "Concluído"
        if s.endswith(":FAIL"):
            return "Insucesso"
        if s.endswith(":NEW"):
            return "Novo"
        return "Em andamento"

    df["Situação"] = df["STAGE_ID"].apply(sit)
    df["Mês criação"] = df["DATE_CREATE"].dt.to_period("M").astype(str)
    return df


def dim_bar(container, df, col, title, color=None, top=15):
    """Barra horizontal de uma dimensão categórica, ignorando vazios/não informados."""
    if df.empty or col not in df.columns:
        container.caption(f"{title}: sem dados.")
        return
    s = df[col].dropna()
    s = s[s.astype(str).str.strip().ne("") & s.astype(str).str.strip().ne("—")]
    if s.empty:
        container.caption(f"{title}: sem dados informados no Bitrix.")
        return
    vc = s.value_counts().head(top).reset_index()
    vc.columns = [col, "Qtde"]
    fig = px.bar(vc.sort_values("Qtde"), x="Qtde", y=col, orientation="h", text_auto=True)
    if color:
        fig.update_traces(marker_color=color)
    fig.update_layout(title=title, height=420, margin=dict(l=10, r=10, t=50, b=10))
    container.plotly_chart(fig, width="stretch")


def won_donut(container, df, col, title, top=10):
    """Donut do valor GANHO (R$) por uma dimensão."""
    if df.empty or col not in df.columns:
        container.caption(f"{title}: sem dados.")
        return
    w = df[df["Situação"] == "Ganho"].copy()
    w = w[w[col].notna() & w[col].astype(str).str.strip().ne("") & w[col].astype(str).str.strip().ne("—")]
    if w.empty or w["OPPORTUNITY"].sum() <= 0:
        container.caption(f"{title}: sem ganhos informados.")
        return
    g = w.groupby(col)["OPPORTUNITY"].sum().sort_values(ascending=False)
    if len(g) > top:
        g = pd.concat([g.iloc[:top], pd.Series({"Outros": g.iloc[top:].sum()})])
    gg = g.reset_index()
    gg.columns = [col, "Ganho"]
    fig = px.pie(gg, names=col, values="Ganho", hole=0.5)
    fig.update_layout(title=f"Ganho por {title} (R$)", height=360, margin=dict(l=10, r=10, t=50, b=10))
    container.plotly_chart(fig, width="stretch")


def count_donut(container, df, col, title, top=8):
    """Donut por contagem de uma dimensão."""
    if df.empty or col not in df.columns:
        container.caption(f"{title}: sem dados.")
        return
    s = df[col].dropna()
    s = s[s.astype(str).str.strip().ne("") & s.astype(str).str.strip().ne("—")]
    if s.empty:
        container.caption(f"{title}: sem dados.")
        return
    g = s.value_counts()
    if len(g) > top:
        g = pd.concat([g.iloc[:top], pd.Series({"Outros": g.iloc[top:].sum()})])
    gg = g.reset_index()
    gg.columns = [col, "Qtde"]
    fig = px.pie(gg, names=col, values="Qtde", hole=0.5)
    fig.update_layout(title=title, height=340, margin=dict(l=10, r=10, t=50, b=10))
    container.plotly_chart(fig, width="stretch")


def conversion_table(df, dim):
    """Tabela de conversão por dimensão: Criados, Ganhos, Perdidos, Valor ganho, Conversão %."""
    b = df.copy()
    b["_g"] = (b["Situação"] == "Ganho").astype(int)
    b["_p"] = (b["Situação"] == "Perdido").astype(int)
    b["_vg"] = b["OPPORTUNITY"].where(b["Situação"] == "Ganho", 0.0)
    g = b.groupby(dim).agg(Criados=("ID", "count"), Ganhos=("_g", "sum"),
                           Perdidos=("_p", "sum"), Valor=("_vg", "sum")).reset_index()
    g["Conversão %"] = (g["Ganhos"] / g["Criados"].replace(0, float("nan")) * 100).round(1)
    return g.sort_values("Criados", ascending=False)


def _conv_table_display(t, dim, label):
    show = t.copy()
    show["Valor"] = show["Valor"].map(fmt_brl)
    show["Conversão %"] = show["Conversão %"].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
    show.columns = [label, "Criados", "Ganhos", "Perdidos", "Valor ganho", "% Conversão"]
    return show


def render_conversao(df):
    st.markdown("### Conversão (criados → ganho)")
    if df.empty:
        st.info("Sem negócios no filtro atual.")
        return
    total = len(df)
    ganho = int((df["Situação"] == "Ganho").sum())
    st.metric("Conversão geral", f"{(ganho/total*100) if total else 0:.1f}%", help=f"{ganho}/{total}")
    m = df.copy()
    m["_g"] = (m["Situação"] == "Ganho").astype(int)
    bym = m.groupby("Mês criação").agg(Criados=("ID", "count"), Ganhos=("_g", "sum")).reset_index()
    bym = bym[bym["Mês criação"] != "NaT"]
    bym["Conversão %"] = (bym["Ganhos"] / bym["Criados"].replace(0, float("nan")) * 100).round(1)
    if not bym.empty:
        figl = px.line(bym, x="Mês criação", y="Conversão %", markers=True)
        figl.update_traces(line_color=WON_COLOR)
        figl.update_layout(title="Conversão por mês (%)", height=320, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(figl, width="stretch")
    for dim, label in [("Vendedor", "Responsável"), ("Fonte", "Fonte"), ("Campanha", "Campanha")]:
        if dim in df.columns and df[dim].notna().any():
            t = conversion_table(df, dim)
            t = t[t[dim].notna() & t[dim].astype(str).str.strip().ne("")]
            if not t.empty:
                st.markdown(f"#### Conversão por {label.lower()}")
                st.dataframe(_conv_table_display(t, dim, label).head(40),
                             width="stretch", hide_index=True)


def _vendor_table(d):
    agg = d.groupby("Vendedor").apply(lambda g: pd.Series({
        "Criados": len(g),
        "Ganhos": int((g["Situação"] == "Ganho").sum()),
        "Perdidos": int((g["Situação"] == "Perdido").sum()),
        "Valor": g.loc[g["Situação"] == "Ganho", "OPPORTUNITY"].sum(),
    }), include_groups=False).reset_index()
    agg["Conversão %"] = (agg["Ganhos"] / agg["Criados"].replace(0, float("nan")) * 100).round(1)
    agg["Ticket Méd."] = (agg["Valor"] / agg["Ganhos"].replace(0, float("nan")))
    agg = agg.sort_values("Valor", ascending=False)
    disp = agg.copy()
    disp["Valor"] = disp["Valor"].map(fmt_brl)
    disp["Ticket Méd."] = disp["Ticket Méd."].map(lambda v: fmt_brl(v) if pd.notna(v) else "—")
    disp["Conversão %"] = disp["Conversão %"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
    return disp


def _val_table(d, dim, situ=None):
    b = d if situ is None else d[d["Situação"] == situ]
    g = b.groupby(dim)["OPPORTUNITY"].agg(["count", "sum"]).reset_index()
    g.columns = [dim, "Qtde", "Valor"]
    g = g[g[dim].notna() & g[dim].astype(str).str.strip().ne("")].sort_values("Valor", ascending=False)
    g["Valor"] = g["Valor"].map(fmt_brl)
    return g


def _yoy_revenue(won):
    w = won.dropna(subset=["Fechamento"]).copy()
    if w.empty:
        return None
    w["Ano"] = w["Fechamento"].dt.year.astype(int)
    w["MesNum"] = w["Fechamento"].dt.month.astype(int)
    yoy = w.groupby(["Ano", "MesNum"])["OPPORTUNITY"].sum().reset_index()
    yoy["Mês"] = yoy["MesNum"].map(lambda m: MESES_PT[m])
    fig = px.line(yoy.sort_values("MesNum"), x="Mês", y="OPPORTUNITY", color="Ano",
                  markers=True, category_orders={"Mês": MESES_PT[1:]})
    fig.update_layout(title="Receita anual (R$) — ano a ano", height=380, yaxis_title="R$",
                      margin=dict(l=10, r=10, t=50, b=10))
    return fig


def render_tsshara_bi(d, prod_f):
    """Layout fiel ao BI (Superset) do cliente: RD Station, Em andamento,
    Fechado, Conversão e Vendas brutas — mesma disposição das informações."""
    won = d[d["Situação"] == "Ganho"]
    lost = d[d["Situação"] == "Perdido"]
    opend = d[d["Situação"] == "Aberto"]
    total = len(d)
    vc = d["Classe"].value_counts() if "Classe" in d.columns else pd.Series(dtype=int)
    em_qual = int(vc.get("Lead", 0))
    qualif = int(vc.get("Negócio aberto", 0) + vc.get("Negócio perdido", 0) + vc.get("Negócio ganho", 0))
    desq = int(vc.get("Lead desqualificado", 0))
    neg_and = int(vc.get("Negócio aberto", 0))
    neg_g, neg_p = len(won), int(vc.get("Negócio perdido", 0))
    tcol = next((c for c in d.columns if c.startswith("Tempo")), None)
    ciclo = won["Ciclo (dias)"].median() if len(won) else 0

    t = st.tabs(["📡 RD Station", "⏳ Em andamento", "✅ Fechado", "🔄 Conversão", "💰 Vendas brutas"])

    # ---------------- RD Station ----------------
    with t[0]:
        r = st.columns(6)
        r[0].metric("Leads criados", fmt_int(total))
        r[1].metric("Em qualificação", fmt_int(em_qual))
        r[2].metric("Leads qualificados", fmt_int(qualif))
        r[3].metric("Qualificados %", f"{(qualif/total*100) if total else 0:.1f}%")
        r[4].metric("Desqualificados", fmt_int(desq))
        tval = "—"
        if tcol:
            ts = pd.to_numeric(d[tcol], errors="coerce")
            ts = ts[ts > 0].dropna()
            tval = f"{ts.median():.0f}" if len(ts) else "—"
        r[5].metric("Tempo atend. (mediana)", tval)
        r2 = st.columns(5)
        r2[0].metric("Negócios em andamento", fmt_int(neg_and))
        r2[1].metric("Negócios ganhos", fmt_int(neg_g))
        r2[2].metric("Negócios perdidos", fmt_int(neg_p))
        r2[3].metric("Valor ganho", fmt_brl(won["OPPORTUNITY"].sum()))
        r2[4].metric("Win rate", f"{(neg_g/(neg_g+neg_p)*100) if (neg_g+neg_p) else 0:.1f}%")
        st.divider()
        cc = st.columns(2)
        count_donut(cc[0], d, "Canal", "Leads criados por canal")
        count_donut(cc[1], d, "Fonte/Origem", "Leads criados por fonte/origem")
        if "Campanha" in d.columns and d["Campanha"].notna().any():
            ct = conversion_table(d, "Campanha")
            ct = ct[ct["Campanha"].notna() & ct["Campanha"].astype(str).str.strip().ne("")]
            if not ct.empty:
                st.markdown("#### Leads criados por campanha")
                st.dataframe(_conv_table_display(ct, "Campanha", "Campanha").head(40),
                             width="stretch", hide_index=True)
        cc2 = st.columns(2)
        count_donut(cc2[0], lost, "MOTIVO", "Motivos de perda")
        count_donut(cc2[1], d, "SEGMENTO", "Negócios criados por segmento")
        st.markdown("#### Valor ganho por dimensão (R$)")
        dd = st.columns(3)
        won_donut(dd[0], d, "Campanha", "Campanha")
        won_donut(dd[1], d, "Estado", "Estado")
        won_donut(dd[2], d, "SEGMENTO", "Segmento")
        dd2 = st.columns(2)
        count_donut(dd2[0], won, "Estado", "Negócios ganhos por Estado")
        count_donut(dd2[1], lost, "SEGMENTO", "Negócios perdidos por segmento")
        st.markdown("#### Vendas por vendedor")
        st.dataframe(_vendor_table(d), width="stretch", hide_index=True)
        if prod_f is not None and not prod_f.empty:
            render_produtos(prod_f)
        if not lost.empty and "MOTIVO" in lost.columns:
            lp = lost[lost["MOTIVO"].notna() & lost["MOTIVO"].astype(str).str.strip().ne("")]
            if not lp.empty:
                st.markdown("#### Motivos de perda por vendedor")
                piv = pd.crosstab(lp["MOTIVO"], lp["Vendedor"])
                piv = piv[piv.sum(axis=0).sort_values(ascending=False).head(12).index]
                piv["Total"] = piv.sum(axis=1)
                st.dataframe(piv.sort_values("Total", ascending=False), width="stretch")

    # ---------------- Em andamento ----------------
    with t[1]:
        k = st.columns(4)
        k[0].metric("Em andamento (R$)", fmt_brl(opend["OPPORTUNITY"].sum()))
        k[1].metric("Em andamento (qtd)", fmt_int(len(opend)))
        k[2].metric("Média das negociações", fmt_brl(opend["OPPORTUNITY"].mean() if len(opend) else 0))
        k[3].metric("Tempo médio (dias)", f"{ciclo:.0f}" if ciclo else "—")
        st.markdown("#### Negócios criados por fonte")
        st.dataframe(_val_table(d, "Fonte"), width="stretch", hide_index=True)
        op = opend.dropna(subset=["CLOSEDATE"]).copy()
        if not op.empty:
            op["Mês prev."] = op["CLOSEDATE"].dt.to_period("M").astype(str)
            fc = (op.groupby("Mês prev.").agg(Negócios=("ID", "count"),
                  Valor=("OPPORTUNITY", "sum")).reset_index())
            fc = fc[fc["Mês prev."] != "NaT"].sort_values("Mês prev.")
            figf = go.Figure()
            figf.add_bar(x=fc["Mês prev."], y=fc["Negócios"], name="Negócios", marker_color=OPEN_COLOR)
            figf.add_trace(go.Scatter(x=fc["Mês prev."], y=fc["Valor"], name="Valor (R$)", yaxis="y2",
                                      mode="lines+markers", line=dict(color=WON_COLOR)))
            figf.update_layout(title="Negócios por data de fechamento prevista", height=380,
                               yaxis=dict(title="Negócios"),
                               yaxis2=dict(title="R$", overlaying="y", side="right", showgrid=False),
                               margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(figf, width="stretch")
        st.markdown("#### Informações gerais (em andamento)")
        info = opend[["TITLE", "OPPORTUNITY", "Vendedor", "Fonte", "Estágio"]].copy()
        info["OPPORTUNITY"] = info["OPPORTUNITY"].map(fmt_brl)
        info.columns = ["Título", "Valor", "Responsável", "Fonte", "Fase"]
        st.dataframe(info.head(200), width="stretch", hide_index=True)

    # ---------------- Fechado ----------------
    with t[2]:
        fechados = d[d["Situação"].isin(["Ganho", "Perdido"])]
        k = st.columns(4)
        k[0].metric("Negócios ganhos (R$)", fmt_brl(won["OPPORTUNITY"].sum()))
        k[1].metric("Negócios ganhos (qtd)", fmt_int(len(won)))
        k[2].metric("Média de ganhos", fmt_brl(won["OPPORTUNITY"].mean() if len(won) else 0))
        k[3].metric("Tempo médio negociação (dias)", f"{ciclo:.0f}" if ciclo else "—")
        cc = st.columns(2)
        cc[0].markdown("**Negócios fechados por fonte**")
        cc[0].dataframe(_val_table(fechados, "Fonte"), width="stretch", hide_index=True)
        if "Campanha" in d.columns:
            cc[1].markdown("**Negócios fechados por campanha**")
            cc[1].dataframe(_val_table(fechados, "Campanha").head(30), width="stretch", hide_index=True)

    # ---------------- Conversão ----------------
    with t[3]:
        render_conversao(d)

    # ---------------- Vendas brutas ----------------
    with t[4]:
        cc = st.columns(2)
        cc[0].markdown("**Vendas por responsável (R$)**")
        cc[0].dataframe(_val_table(won, "Vendedor"), width="stretch", hide_index=True)
        cc[1].markdown("**Negócios ganhos por fonte (R$)**")
        cc[1].dataframe(_val_table(won, "Fonte"), width="stretch", hide_index=True)
        if "Campanha" in d.columns:
            st.markdown("**Negócios ganhos por campanha (R$)**")
            st.dataframe(_val_table(won, "Campanha").head(30), width="stretch", hide_index=True)
        figy = _yoy_revenue(won)
        if figy is not None:
            st.plotly_chart(figy, width="stretch")
        st.markdown("#### Negócios (ganhos)")
        ng = won[["ID", "Fonte", "Estágio", "OPPORTUNITY", "TITLE"]].copy()
        ng["OPPORTUNITY"] = ng["OPPORTUNITY"].map(fmt_brl)
        ng.columns = ["id", "Fonte", "Fase", "Valor", "Título"]
        st.dataframe(ng.head(300), width="stretch", hide_index=True)


# ====================================================================== login
def render_login():
    col = st.columns([1, 5])
    col[0].image(LOGO, width=84)
    col[1].title(APP_NAME)
    st.caption("Acesse com as credenciais fornecidas pela ZOPU.")
    with st.form("login"):
        username = st.text_input("Usuário")
        password = st.text_input("Senha", type="password")
        ok = st.form_submit_button("Entrar", type="primary")
    if ok:
        user = auth.authenticate(username.strip(), password)
        if user:
            st.session_state.user = dict(user)
            st.rerun()
        else:
            st.error("Usuário ou senha inválidos.")
    st.stop()


def maybe_autosync(tenant: dict):
    """Auto-sync de segurança: se a última sync foi há mais de 60 min, roda agora."""
    s = db.get_sync(tenant["ID"])
    stale = True
    if s and s.get("LAST_RUN"):
        try:
            last = datetime.fromisoformat(s["LAST_RUN"])
            stale = (datetime.now() - last) > timedelta(minutes=60)
        except ValueError:
            stale = True
    if stale:
        with st.spinner("Sincronizando com o Bitrix24…"):
            try:
                sync_tenant(tenant)
            except Exception as e:  # noqa: BLE001
                st.warning(f"Não foi possível sincronizar automaticamente: {e}")


# ====================================================================== dashboard
def build_products_df(prod, deals, family_map=None) -> pd.DataFrame:
    """Junta as linhas de produto com os dados do negócio (situação, vendedor,
    fonte, data) para permitir filtros e análise de produtos ganhos."""
    if prod.empty:
        return prod
    prod = prod.copy()
    prod["TOTAL"] = pd.to_numeric(prod["TOTAL"], errors="coerce").fillna(0.0)
    prod["QUANTITY"] = pd.to_numeric(prod["QUANTITY"], errors="coerce").fillna(0.0)
    fm = family_map or {}
    prod["Família"] = prod["PRODUCT_ID"].astype(str).map(fm).fillna("N/A")
    cols = ["ID", "Situação", "Vendedor", "Fonte", "DATE_CREATE"]
    info = deals[cols].rename(columns={"ID": "DEAL_ID"}) if not deals.empty else \
        pd.DataFrame(columns=["DEAL_ID", "Situação", "Vendedor", "Fonte", "DATE_CREATE"])
    return prod.merge(info, on="DEAL_ID", how="left")


@st.cache_data(ttl=600, show_spinner="Carregando dados…")
def load_frames(tenant_id: int, cat_id: str, spa_ids: tuple, cache_key: str):
    """Carrega e processa deals/leads/SPAs/produtos do banco. Cacheado por
    (tenant, sync) para que filtros e troca de abas fiquem rápidos."""
    meta = db.get_meta(tenant_id)
    sm, um = meta["status_map"], meta["user_map"]
    won_stages = db.get_field_map(tenant_id).get("won_extra_stages") or []
    deals = build_deals_df(db.deals_df(tenant_id), sm, um, cat_id, won_stages=won_stages)
    leads = build_leads_df(db.leads_df(tenant_id), sm, um)
    spa = {et: build_spa_df(db.spa_items_df(tenant_id, et), sm, um, et) for et in spa_ids}
    products = build_products_df(db.products_df(tenant_id), deals, db.get_family_map(tenant_id))
    return deals, leads, spa, products


def render_dashboard(tenant: dict, user: dict):
    meta = db.get_meta(tenant["ID"])
    status_map, user_map = meta["status_map"], meta["user_map"]
    cat_id = tenant["SALES_CATEGORY_ID"]
    cat_name = meta["categories"].get(cat_id, f"Funil {cat_id}")

    fmap = db.get_field_map(tenant["ID"])
    spas = _spa_list(fmap)

    sync = db.get_sync(tenant["ID"])
    cache_key = (sync or {}).get("LAST_RUN") or "none"
    deals_all, leads_all, spa_all, prod_all = load_frames(
        tenant["ID"], cat_id, tuple(s["entity_type_id"] for s in spas), cache_key)
    n_spa = sum(len(v) for v in spa_all.values())

    # ---------------- modo Comercial x SAC (escopo do usuário) ----------------
    sac_cfg = fmap.get("sac")
    scope = (user.get("SCOPE") or "all")
    mode = "Comercial"
    if sac_cfg:
        if scope == "sac":
            mode = "SAC"
        elif scope == "comercial":
            mode = "Comercial"
        else:
            mode = st.sidebar.radio("Visão", ["Comercial", "SAC"], horizontal=True)
    if mode == "SAC":
        render_sac(deals_all, prod_all, status_map, sac_cfg, tenant)
        return
    # Comercial: restringe aos negócios do funil comercial (exclui SAC e outros)
    if not deals_all.empty:
        deals_all = deals_all[deals_all["CATEGORY_ID"].astype(str) == str(cat_id)]

    st.title(f"📊 {tenant['NAME']}")
    info = f"Funil: **{cat_name}** · {len(deals_all)} negócios"
    if not fmap.get("leads_from_deals"):
        info += f" · {len(leads_all)} leads"
    info += f" · {n_spa} itens SPA"
    if sync and sync.get("LAST_RUN"):
        info += f" · última sync: {sync['LAST_RUN'].replace('T', ' ')}"
    st.caption(info)

    if deals_all.empty and leads_all.empty:
        st.info("Sem dados sincronizados ainda. Use **Sincronizar agora** na barra lateral.")
        return

    # ---------------- filtros ----------------
    st.sidebar.header("Filtros")
    date_base = st.sidebar.radio("Base da data", ["Criação", "Fechamento"], horizontal=True,
                                 help="Período pela data de criação ou de finalização (fechamento real) do negócio")
    date_col = "Fechamento" if date_base == "Fechamento" else "DATE_CREATE"
    base_dates = deals_all.get(date_col, pd.Series(dtype="datetime64[ns]"))
    all_dates = pd.concat([base_dates,
                           leads_all.get("DATE_CREATE", pd.Series(dtype="datetime64[ns]"))]).dropna()
    if not all_dates.empty:
        dmin, dmax = all_dates.min().date(), all_dates.max().date()
        rng = st.sidebar.date_input(f"Período ({date_base.lower()})", value=(dmin, dmax),
                                    min_value=dmin, max_value=dmax)
        d0, d1 = rng if isinstance(rng, tuple) and len(rng) == 2 else (dmin, dmax)
    else:
        d0 = d1 = None

    vendedores = sorted(set(deals_all.get("Vendedor", [])) | set(leads_all.get("Vendedor", [])))
    sel_vend = st.sidebar.multiselect("Vendedor", vendedores, default=vendedores)
    fontes = sorted(set(deals_all.get("Fonte", [])) | set(leads_all.get("Fonte", [])))
    sel_fonte = st.sidebar.multiselect("Fonte", fontes, default=fontes)

    # filtros adicionais (aplicam só aos negócios/produtos, não a leads/SPAs)
    def _opts(col):
        if col in deals_all.columns:
            return sorted(deals_all[col].dropna().astype(str).unique().tolist())
        return []

    sit_opts = _opts("Situação")
    sel_sit = st.sidebar.multiselect("Situação", sit_opts, default=sit_opts) if sit_opts else None
    est_opts = _opts("Estágio")
    with st.sidebar.expander("Mais filtros de negócios"):
        sel_est = st.multiselect("Estágio", est_opts, default=est_opts) if est_opts else None
        seg_opts = _opts("SEGMENTO")
        sel_seg = st.multiselect("Segmento", seg_opts, default=seg_opts) if seg_opts else None
        card_opts = _opts("Status do Cartão")
        sel_card = st.multiselect("Status do Cartão", card_opts, default=card_opts) if card_opts else None
        canal_opts = _opts("Canal")
        sel_canal = st.multiselect("Canal", canal_opts, default=canal_opts) if canal_opts else None
        estado_opts = _opts("Estado")
        sel_estado = st.multiselect("Estado", estado_opts, default=estado_opts) if estado_opts else None
        # família de produto (atributo do produto -> filtra negócios que têm produto dessa família)
        fam_opts = (sorted(prod_all["Família"].dropna().astype(str).unique().tolist())
                    if (not prod_all.empty and "Família" in prod_all.columns) else [])
        sel_fam = st.multiselect("Família de produto", fam_opts, default=fam_opts) if fam_opts else None
        # faixa de valor
        vmax = float(deals_all["OPPORTUNITY"].max()) if "OPPORTUNITY" in deals_all.columns and len(deals_all) else 0.0
        val_range = None
        if vmax > 0:
            val_range = st.slider("Valor (R$)", 0.0, vmax, (0.0, vmax))

    # negócios que têm produto nas famílias selecionadas (None = sem filtro de família)
    fam_deal_ids = None
    if sel_fam is not None and fam_opts and set(sel_fam) != set(fam_opts):
        fam_deal_ids = set(prod_all.loc[prod_all["Família"].astype(str).isin(sel_fam),
                                        "DEAL_ID"].astype(str))

    def flt(df, use_date=True):
        """Filtros comuns (data, vendedor, fonte) — seguros para qualquer entidade.
        A data usa a base escolhida (criação/fechamento); entidades sem CLOSEDATE
        caem para DATE_CREATE."""
        if df.empty:
            return df
        m = pd.Series(True, index=df.index)
        if use_date and d0 is not None:
            col = date_col if date_col in df.columns else "DATE_CREATE"
            if col in df.columns:
                ds = df[col].dt.date
                m &= (ds >= d0) & (ds <= d1)
        if sel_vend and "Vendedor" in df.columns:
            m &= df["Vendedor"].isin(sel_vend)
        if sel_fonte and "Fonte" in df.columns:
            m &= df["Fonte"].isin(sel_fonte)
        return df[m]

    def deal_filter(df):
        """Filtros específicos de negócios/produtos (situação, estágio, segmento...)."""
        if df.empty:
            return df
        m = pd.Series(True, index=df.index)
        if sel_sit is not None and "Situação" in df.columns:
            m &= df["Situação"].astype(str).isin(sel_sit)
        if sel_est is not None and "Estágio" in df.columns:
            m &= df["Estágio"].astype(str).isin(sel_est)
        if sel_seg is not None and "SEGMENTO" in df.columns:
            m &= df["SEGMENTO"].astype(str).isin(sel_seg)
        if sel_card is not None and "Status do Cartão" in df.columns:
            m &= df["Status do Cartão"].astype(str).isin(sel_card)
        if sel_canal is not None and "Canal" in df.columns:
            m &= df["Canal"].astype(str).isin(sel_canal)
        if sel_estado is not None and "Estado" in df.columns:
            m &= df["Estado"].astype(str).isin(sel_estado)
        if val_range is not None and "OPPORTUNITY" in df.columns:
            m &= df["OPPORTUNITY"].between(val_range[0], val_range[1])
        # família: produtos filtram pela própria coluna; negócios pelos IDs com a família
        if sel_fam is not None and "Família" in df.columns:
            m &= df["Família"].astype(str).isin(sel_fam)
        elif fam_deal_ids is not None and "ID" in df.columns:
            m &= df["ID"].astype(str).isin(fam_deal_ids)
        return df[m]

    deals_f = deal_filter(flt(deals_all))
    leads_f = flt(leads_all)
    deals_t = deal_filter(flt(deals_all, use_date=False))
    leads_t = flt(leads_all, use_date=False)
    spa_f = {et: flt(df) for et, df in spa_all.items()}
    prod_f = deal_filter(flt(prod_all))

    won = deals_f[deals_f["Situação"] == "Ganho"]
    lost = deals_f[deals_f["Situação"] == "Perdido"]
    opend = deals_f[deals_f["Situação"] == "Aberto"]
    valor_ganho, valor_aberto = won["OPPORTUNITY"].sum(), opend["OPPORTUNITY"].sum()
    ticket = won["OPPORTUNITY"].mean() if len(won) else 0
    fechados = len(won) + len(lost)
    win_rate = (len(won) / fechados * 100) if fechados else 0
    ciclo = won["Ciclo (dias)"].median() if len(won) else 0
    total_leads = len(leads_f)
    conv_leads = int(leads_f["Convertido"].sum()) if total_leads else 0
    taxa_conv = (conv_leads / total_leads * 100) if total_leads else 0

    # CRMs onde o lead é o negócio nas fases iniciais (ex.: TS Shara) — recalcula
    lfd = fmap.get("leads_from_deals")
    leads_in_deals = bool(lfd) and not deals_f.empty
    if leads_in_deals:
        deals_f = deals_f.copy()
        deals_f["Classe"] = classify_funnel(deals_f, lfd.get("lead_stages", []))
        # alinha o Status do Cartão (usado na aba Pipeline) com a nova fronteira
        deals_f["Status do Cartão"] = deals_f["Classe"].map({
            "Lead": "Lead", "Lead desqualificado": "Lead Desqualificado",
            "Negócio aberto": "Negócio (Lead Qualificado)",
            "Negócio perdido": "Negócio (Lead Qualificado)",
            "Negócio ganho": "Negócio Ganho"})
        vc = deals_f["Classe"].value_counts()
        neg_ganho = int(vc.get("Negócio ganho", 0))
        neg_perd = int(vc.get("Negócio perdido", 0))
        qualificados = neg_ganho + neg_perd + int(vc.get("Negócio aberto", 0))
        total_leads = len(deals_f)
        taxa_conv = (qualificados / total_leads * 100) if total_leads else 0   # lead -> negócio
        taxa_geral = (neg_ganho / total_leads * 100) if total_leads else 0     # lead -> ganho
        win_rate = (neg_ganho / (neg_ganho + neg_perd) * 100) if (neg_ganho + neg_perd) else 0
        valor_aberto = deals_f.loc[deals_f["Classe"] == "Negócio aberto", "OPPORTUNITY"].sum()

    # layout fiel ao BI do cliente (mesma disposição das informações)
    if fmap.get("bi_layout") and leads_in_deals:
        render_tsshara_bi(deals_f, prod_f)
        return

    st.subheader("Indicadores-chave")
    r1 = st.columns(4)
    r1[0].metric("💰 Valor ganho", fmt_brl(valor_ganho))
    r1[1].metric("🟦 Pipeline aberto", fmt_brl(valor_aberto))
    r1[2].metric("🎯 Win rate", f"{win_rate:.1f}%",
                 help="Ganhos / (ganhos + negócios perdidos)" if leads_in_deals else None)
    r1[3].metric("🧾 Ticket médio", fmt_brl(ticket))
    r2 = st.columns(4)
    if leads_in_deals:
        r2[0].metric("📇 Leads gerados", fmt_int(total_leads))
        r2[1].metric("✅ Taxa geral de conversão", f"{taxa_geral:.1f}%",
                     delta=f"{taxa_conv:.1f}% viram negócio", delta_color="off",
                     help="Negócios ganhos / leads gerados (lead → ganho)")
    else:
        r2[0].metric("📇 Leads", fmt_int(total_leads))
        r2[1].metric("✅ Conversão de leads", f"{taxa_conv:.1f}%", help=f"{conv_leads}/{total_leads}")
    r2[2].metric("🤝 Negócios ganhos", fmt_int(len(won)))
    r2[3].metric("⏱️ Ciclo (mediana)", f"{ciclo:.0f} dias" if ciclo else "—")
    st.divider()

    spa_labels = [f"{s.get('icon', '🧩')} {s['label']}" for s in spas]
    tab_names = (["📈 Visão geral", "📊 Gestão", "🛒 Pipeline", "📇 Leads", "🛍️ Produtos"] + spa_labels +
                 ["👤 Vendedores", "🌐 Fontes", "🔄 Conversão", "🎯 Metas", "📅 Mês a mês", "🗂️ Dados"])
    tabs = st.tabs(tab_names)
    # índices das abas fixas após as SPAs dinâmicas (5 abas iniciais)
    base = 5 + len(spas)
    tab_vend, tab_font, tab_conv, tab_meta, tab_mom, tab_dados = (
        tabs[base], tabs[base + 1], tabs[base + 2], tabs[base + 3], tabs[base + 4], tabs[base + 5])
    # estágios do funil de vendas (cat 0 usa DEAL_STAGE; demais usam DEAL_STAGE_<cat>)
    stage_names = status_map.get(f"DEAL_STAGE_{cat_id}") or status_map.get("DEAL_STAGE", {})
    order = list(stage_names.values())

    # -------- visão geral --------
    with tabs[0]:
        render_funnel(deals_f, stage_names)
        st.markdown("#### Valor por situação")
        sit = (deals_f.groupby("Situação")["OPPORTUNITY"].sum()
               .reindex(["Aberto", "Ganho", "Perdido"]).fillna(0).reset_index())
        figs = px.bar(sit, x="OPPORTUNITY", y="Situação", orientation="h", text_auto=".2s",
                      color="Situação",
                      color_discrete_map={"Ganho": WON_COLOR, "Perdido": LOST_COLOR, "Aberto": OPEN_COLOR})
        figs.update_layout(height=300, showlegend=False, xaxis_title="R$",
                           margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(figs, width="stretch")

    # -------- gestão --------
    with tabs[1]:
        render_gestao(deals_t, leads_t, lfd if leads_in_deals else None)

    # -------- pipeline --------
    with tabs[2]:
        c = st.columns(3)
        c[0].metric("Abertos", fmt_int(len(opend)), help=fmt_brl(valor_aberto))
        c[1].metric("Ganhos", fmt_int(len(won)), help=fmt_brl(valor_ganho))
        c[2].metric("Perdidos", fmt_int(len(lost)), help=fmt_brl(lost["OPPORTUNITY"].sum()))
        cc = st.columns(2)
        by_stage = deals_f.groupby("Estágio").agg(Valor=("OPPORTUNITY", "sum")).reset_index()
        by_stage["ord"] = by_stage["Estágio"].apply(lambda x: order.index(x) if x in order else 999)
        by_stage = by_stage.sort_values("ord")
        if not by_stage.empty:
            fig = px.bar(by_stage, x="Valor", y="Estágio", orientation="h", text_auto=".2s")
            fig.update_layout(title="Valor (R$) por estágio", height=420,
                              margin=dict(l=10, r=10, t=50, b=10))
            cc[0].plotly_chart(fig, width='stretch')
        if len(won):
            figh = px.histogram(won, x="Ciclo (dias)", nbins=20)
            figh.update_traces(marker_color=WON_COLOR)
            figh.update_layout(title="Ciclo de vendas (ganhos)", height=420,
                               margin=dict(l=10, r=10, t=50, b=10))
            cc[1].plotly_chart(figh, width='stretch')
        st.markdown("#### Maiores negócios em aberto")
        top = opend.sort_values("OPPORTUNITY", ascending=False).head(15)
        if not top.empty:
            t = top[["TITLE", "Estágio", "Vendedor", "Fonte", "OPPORTUNITY", "DATE_CREATE"]].copy()
            t["OPPORTUNITY"] = t["OPPORTUNITY"].map(fmt_brl)
            t["DATE_CREATE"] = t["DATE_CREATE"].dt.strftime("%d/%m/%Y")
            t.columns = ["Negócio", "Estágio", "Vendedor", "Fonte", "Valor", "Criado em"]
            st.dataframe(t, width='stretch', hide_index=True)
        st.markdown("#### Motivos de perda e segmento")
        cc2 = st.columns(2)
        dim_bar(cc2[0], lost, "MOTIVO", "Motivo de fechamento (perdidos)", LOST_COLOR)
        dim_bar(cc2[1], deals_f, "SEGMENTO", "Negócios por segmento")
        render_extra_dims(deals_f)

        # forecast: negócios em aberto por data de fechamento prevista (CLOSEDATE planejado)
        st.markdown("#### Negócios em aberto por data de fechamento prevista")
        op = opend.dropna(subset=["CLOSEDATE"]).copy()
        if not op.empty:
            op["Mês prev."] = op["CLOSEDATE"].dt.to_period("M").astype(str)
            fc = (op.groupby("Mês prev.").agg(Negócios=("ID", "count"),
                  Valor=("OPPORTUNITY", "sum")).reset_index().sort_values("Mês prev."))
            fc = fc[fc["Mês prev."] != "NaT"]
            figf = go.Figure()
            figf.add_bar(x=fc["Mês prev."], y=fc["Negócios"], name="Negócios", marker_color=OPEN_COLOR)
            figf.add_trace(go.Scatter(x=fc["Mês prev."], y=fc["Valor"], name="Valor (R$)",
                                      yaxis="y2", mode="lines+markers", line=dict(color=WON_COLOR)))
            figf.update_layout(title="Previsão de fechamento (nº e R$)", height=380,
                               yaxis=dict(title="Negócios"),
                               yaxis2=dict(title="R$", overlaying="y", side="right", showgrid=False),
                               margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(figf, width="stretch")

        # motivos de perda por vendedor (pivot)
        if not lost.empty and "MOTIVO" in lost.columns:
            lp = lost[lost["MOTIVO"].notna() & lost["MOTIVO"].astype(str).str.strip().ne("")]
            if not lp.empty:
                st.markdown("#### Motivos de perda por vendedor")
                piv = pd.crosstab(lp["MOTIVO"], lp["Vendedor"])
                top_v = piv.sum(axis=0).sort_values(ascending=False).head(12).index
                piv = piv[top_v]
                piv["Total"] = piv.sum(axis=1)
                piv = piv.sort_values("Total", ascending=False)
                st.dataframe(piv, width="stretch")

    # -------- leads --------
    with tabs[3]:
      if leads_in_deals:
        render_leads_from_deals(deals_f)
      else:
        c = st.columns(4)
        junk = int(leads_f["Desqualificado"].sum()) if total_leads else 0
        noshow = int((leads_f["Status"] == "No-show/Cancelada/Regendamento").sum()) if total_leads else 0
        c[0].metric("Total", fmt_int(total_leads))
        c[1].metric("Convertidos", fmt_int(conv_leads), f"{taxa_conv:.1f}%")
        c[2].metric("Desqualificados", fmt_int(junk),
                    f"{(junk/total_leads*100) if total_leads else 0:.1f}%")
        c[3].metric("No-show/cancel.", fmt_int(noshow))
        cc = st.columns(2)
        bs = leads_f.groupby("Status").size().reset_index(name="Qtde").sort_values("Qtde")
        if not bs.empty:
            fig = px.bar(bs, x="Qtde", y="Status", orientation="h", text_auto=True)
            fig.update_traces(marker_color=META_COLOR)
            fig.update_layout(title="Leads por status", height=420, margin=dict(l=10, r=10, t=50, b=10))
            cc[0].plotly_chart(fig, width='stretch')
        bsr = leads_f.groupby("Fonte").size().reset_index(name="Qtde").sort_values("Qtde")
        if not bsr.empty:
            fig = px.bar(bsr, x="Qtde", y="Fonte", orientation="h", text_auto=True)
            fig.update_layout(title="Leads por fonte", height=420, margin=dict(l=10, r=10, t=50, b=10))
            cc[1].plotly_chart(fig, width='stretch')
        if total_leads:
            conv = (leads_f.groupby("Fonte").agg(Leads=("ID", "count"),
                    Convertidos=("Convertido", "sum")).reset_index())
            conv["Taxa %"] = (conv["Convertidos"] / conv["Leads"] * 100).round(1)
            st.markdown("#### Conversão por fonte")
            st.dataframe(conv.sort_values("Leads", ascending=False),
                         width='stretch', hide_index=True)
        st.markdown("#### Segmento, cargo e motivos de desqualificação")
        cc3 = st.columns(2)
        dim_bar(cc3[0], leads_f, "SEGMENTO", "Leads por segmento")
        dim_bar(cc3[1], leads_f, "CARGO", "Leads por cargo")
        dim_bar(st, leads_f, "MOTIVO", "Motivos de desqualificação (leads)", LOST_COLOR)

    # -------- produtos --------
    with tabs[4]:
        render_produtos(prod_f)

    # -------- SPAs dinâmicas (reuniões / processamento / pós-vendas / diárias...) --------
    for i, s in enumerate(spas):
        with tabs[5 + i]:
            render_spa(spa_f.get(s["entity_type_id"], pd.DataFrame()), s["label"])

    # -------- vendedores --------
    with tab_vend:
        if deals_f.empty:
            st.info("Sem negócios no filtro atual.")
        else:
            agg = deals_f.groupby("Vendedor").apply(lambda g: pd.Series({
                "Negócios": len(g),
                "Ganhos": int((g["Situação"] == "Ganho").sum()),
                "Perdidos": int((g["Situação"] == "Perdido").sum()),
                "Valor ganho": g.loc[g["Situação"] == "Ganho", "OPPORTUNITY"].sum(),
                "Pipeline aberto": g.loc[g["Situação"] == "Aberto", "OPPORTUNITY"].sum(),
            }), include_groups=False).reset_index()
            fech = agg["Ganhos"] + agg["Perdidos"]
            agg["Win rate %"] = (agg["Ganhos"] / fech.replace(0, float("nan")) * 100).round(1)
            agg["Conversão %"] = (agg["Ganhos"] / agg["Negócios"].replace(0, float("nan")) * 100).round(1)
            agg["Ticket médio"] = (agg["Valor ganho"] / agg["Ganhos"].replace(0, float("nan")))
            agg = agg.sort_values("Valor ganho", ascending=False)
            fig = px.bar(agg, x="Vendedor", y="Valor ganho", text_auto=".2s")
            fig.update_traces(marker_color=WON_COLOR)
            fig.update_layout(title="Valor ganho por vendedor", height=400,
                              margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(fig, width='stretch')
            disp = agg.copy()
            disp["Valor ganho"] = disp["Valor ganho"].map(fmt_brl)
            disp["Pipeline aberto"] = disp["Pipeline aberto"].map(fmt_brl)
            disp["Ticket médio"] = disp["Ticket médio"].map(lambda v: fmt_brl(v) if pd.notna(v) else "—")
            disp = disp[["Vendedor", "Negócios", "Ganhos", "Perdidos", "Conversão %", "Win rate %",
                         "Ticket médio", "Valor ganho", "Pipeline aberto"]]
            st.dataframe(disp, width='stretch', hide_index=True)

    # -------- fontes --------
    with tab_font:
        cc = st.columns(2)
        if not deals_f.empty:
            sd = deals_f.copy()
            sd["GanhoVal"] = sd.apply(lambda r: r["OPPORTUNITY"] if r["Situação"] == "Ganho" else 0, axis=1)
            grp = sd.groupby("Fonte").agg(Negócios=("ID", "count"), Ganho=("GanhoVal", "sum")).reset_index()
            grp = grp.sort_values("Negócios", ascending=False)
            fig = px.bar(grp, x="Negócios", y="Fonte", orientation="h", text_auto=True)
            fig.update_layout(title="Negócios por fonte", height=460, margin=dict(l=10, r=10, t=50, b=10))
            cc[0].plotly_chart(fig, width='stretch')
            gw = grp[grp["Ganho"] > 0]
            if not gw.empty:
                fig2 = px.bar(gw, x="Ganho", y="Fonte", orientation="h", text_auto=".2s")
                fig2.update_traces(marker_color=WON_COLOR)
                fig2.update_layout(title="Valor ganho por fonte (R$)", height=460,
                                   margin=dict(l=10, r=10, t=50, b=10))
                cc[1].plotly_chart(fig2, width='stretch')
            # ganhos em R$ por Estado / Segmento / Campanha (donuts)
            st.markdown("#### Valor ganho por dimensão (R$)")
            dd = st.columns(3)
            for i, dimc in enumerate(["Estado", "SEGMENTO", "Campanha"]):
                won_donut(dd[i], deals_f, dimc, {"SEGMENTO": "Segmento"}.get(dimc, dimc))

    # -------- conversão --------
    with tab_conv:
        render_conversao(deals_f)

    # -------- metas --------
    with tab_meta:
        render_metas(tenant, user, deals_t, user_map)

    # -------- mês a mês --------
    with tab_mom:
        render_mom(deals_t, leads_t)

    # -------- dados --------
    with tab_dados:
        st.download_button("⬇️ Deals (CSV)", deals_f.to_csv(index=False).encode("utf-8-sig"),
                           "deals.csv", "text/csv")
        st.dataframe(deals_f, width='stretch', hide_index=True)
        st.download_button("⬇️ Leads (CSV)", leads_f.to_csv(index=False).encode("utf-8-sig"),
                           "leads.csv", "text/csv")
        st.dataframe(leads_f, width='stretch', hide_index=True)
        for s in spas:
            df = spa_f.get(s["entity_type_id"], pd.DataFrame())
            if not df.empty:
                st.download_button(f"⬇️ {s['label']} (CSV)", df.to_csv(index=False).encode("utf-8-sig"),
                                   f"spa_{s['entity_type_id']}.csv", "text/csv",
                                   key=f"dl_spa_{s['entity_type_id']}")
                st.dataframe(df, width='stretch', hide_index=True)
        if not prod_f.empty:
            st.download_button("⬇️ Produtos (CSV)", prod_f.to_csv(index=False).encode("utf-8-sig"),
                               "produtos.csv", "text/csv", key="dl_prod")
            st.dataframe(prod_f, width='stretch', hide_index=True)


EXTRA_CAT_DIMS = ["Status do Cartão", "Canal", "Fonte/Origem", "Campanha", "Estado",
                  "Qualificação", "Fase Anterior"]
CARD_ORDER = ["Lead", "Negócio (Lead Qualificado)", "Negócio Ganho", "Lead Desqualificado"]


def render_extra_dims(deals_f):
    """Dimensões extras (TS Shara): Status do Cartão, Canal, Campanha, Fonte/Origem etc.
    Aparece só quando o ambiente tem essas dimensões no EXTRA."""
    present = [c for c in EXTRA_CAT_DIMS if c in deals_f.columns]
    num_dims = [c for c in deals_f.columns if c.startswith("Tempo")]
    if deals_f.empty or not (present or num_dims):
        return
    st.markdown("#### Status do cartão e conversão")
    if "Status do Cartão" in deals_f.columns:
        sc = deals_f["Status do Cartão"].value_counts()
        cols = st.columns(len(CARD_ORDER))
        for i, k in enumerate(CARD_ORDER):
            cols[i].metric(k, fmt_int(sc.get(k, 0)))
        # conversão (conceito de funil de growth: Lead -> Qualificado -> Ganho)
        total = int(sc.sum())
        ganho = int(sc.get("Negócio Ganho", 0))
        qualif = int(sc.get("Negócio (Lead Qualificado)", 0)) + ganho
        taxa_qual = (qualif / total * 100) if total else 0
        taxa_ganho_qual = (ganho / qualif * 100) if qualif else 0
        cc0 = st.columns(3)
        cc0[0].metric("Taxa de qualificação", f"{taxa_qual:.1f}%",
                      help="(Negócios qualificados + ganhos) / total de cartões")
        cc0[1].metric("Conversão p/ ganho (qualificados)", f"{taxa_ganho_qual:.1f}%",
                      help="Ganhos / negócios qualificados — win rate sobre quem virou negócio real")
        cc0[2].metric("Conversão geral (cartão→ganho)", f"{(ganho/total*100) if total else 0:.1f}%")
    for nd in num_dims:
        # ignora valores negativos/zerados (campo de tempo do CRM tem dados inconsistentes)
        s = pd.to_numeric(deals_f[nd], errors="coerce")
        s = s[s > 0].dropna()
        if len(s):
            st.metric(f"{nd} (mediana, válidos)", f"{s.median():.0f}",
                      help=f"{len(s)} registros com valor positivo")
    cat = [c for c in present if c != "Status do Cartão"]
    cc = st.columns(2)
    for i, dim in enumerate(cat):
        dim_bar(cc[i % 2], deals_f, dim, dim)


def classify_funnel(df, lead_stages):
    """Para CRMs que tratam lead como fases iniciais do negócio: classifica cada
    negócio em Lead / Lead desqualificado / Negócio aberto / Negócio perdido /
    Negócio ganho. Usa o Status do Cartão já calculado na sync (que considera
    qualificação + fase anterior) e aplica a fronteira de fases de lead: um
    negócio EM ABERTO numa fase de lead volta a contar como Lead."""
    lead_set = set(lead_stages)
    status = (df["Status do Cartão"].astype(str) if "Status do Cartão" in df.columns
              else pd.Series(["Lead"] * len(df), index=df.index))
    out = []
    for stt, situ, stage in zip(status, df["Situação"], df["Estágio"]):
        if situ == "Aberto" and stage in lead_set:
            out.append("Lead")
        elif stt == "Negócio Ganho":
            out.append("Negócio ganho")
        elif stt == "Lead Desqualificado":
            out.append("Lead desqualificado")
        elif stt == "Lead":
            out.append("Lead")
        else:  # Negócio (Lead Qualificado)
            out.append("Negócio perdido" if situ == "Perdido" else "Negócio aberto")
    return out


CLASSE_COLORS = {"Lead": "#f59e0b", "Lead desqualificado": "#9ca3af",
                 "Negócio aberto": OPEN_COLOR, "Negócio perdido": LOST_COLOR,
                 "Negócio ganho": WON_COLOR}


def render_leads_from_deals(df):
    st.markdown("### Leads (originados nos negócios)")
    st.caption("Neste CRM o lead é o negócio nas fases iniciais (Nova Oportunidade → "
               "Entrando em Contato). A partir de Diagnóstico vira lead qualificado / "
               "negócio (tag RD Station). A tabela de Leads do Bitrix não é usada.")
    if df.empty or "Classe" not in df.columns:
        st.info("Sem negócios no filtro atual.")
        return
    total = len(df)
    vc = df["Classe"].value_counts()
    qual = int(vc.get("Negócio aberto", 0) + vc.get("Negócio perdido", 0) + vc.get("Negócio ganho", 0))
    desq = int(vc.get("Lead desqualificado", 0))
    leadab = int(vc.get("Lead", 0))
    ganho = int(vc.get("Negócio ganho", 0))
    k = st.columns(4)
    k[0].metric("Leads gerados", fmt_int(total))
    k[1].metric("Viraram negócio", fmt_int(qual), f"{(qual/total*100) if total else 0:.1f}%")
    k[2].metric("Desqualificados", fmt_int(desq), f"{(desq/total*100) if total else 0:.1f}%")
    k[3].metric("Ainda em lead (aberto)", fmt_int(leadab))

    fdf = pd.DataFrame({"Etapa": ["Leads gerados", "Viraram negócio", "Ganhos"],
                        "Qtde": [total, qual, ganho]})
    fig = go.Figure(go.Funnel(y=fdf["Etapa"], x=fdf["Qtde"], textinfo="value+percent initial",
                              marker={"color": [OPEN_COLOR, "#6366f1", WON_COLOR]}))
    fig.update_layout(title="Funil Lead → Negócio → Ganho", height=340,
                      margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, width="stretch")

    cc = st.columns(2)
    dim_bar(cc[0], df, "Canal", "Leads por canal")
    dim_bar(cc[1], df, "Fonte/Origem", "Leads por fonte/origem")
    cc2 = st.columns(2)
    dim_bar(cc2[0], df, "Campanha", "Leads por campanha")
    dim_bar(cc2[1], df[df["Classe"] == "Lead desqualificado"], "MOTIVO",
            "Motivos de desqualificação", LOST_COLOR)
    # tabela de campanha (criados / ganhos / valor / % conversão)
    if "Campanha" in df.columns and df["Campanha"].notna().any():
        t = conversion_table(df, "Campanha")
        t = t[t["Campanha"].notna() & t["Campanha"].astype(str).str.strip().ne("")]
        if not t.empty:
            st.markdown("#### Leads criados por campanha")
            st.dataframe(_conv_table_display(t, "Campanha", "Campanha").head(40),
                         width="stretch", hide_index=True)
    bym = df.groupby(["Mês criação", "Classe"]).size().reset_index(name="Qtde")
    bym = bym[bym["Mês criação"] != "NaT"]
    if not bym.empty:
        figm = px.bar(bym, x="Mês criação", y="Qtde", color="Classe", barmode="stack",
                      color_discrete_map=CLASSE_COLORS)
        figm.update_layout(title="Leads/negócios criados por mês", height=360,
                           margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(figm, width="stretch")


SAC_SIT = {"Aberto": "Em andamento", "Ganho": "Resolvido", "Perdido": "Não resolvido"}
SAC_COLORS = {"Resolvido": WON_COLOR, "Não resolvido": LOST_COLOR, "Em andamento": OPEN_COLOR}


def render_sac(deals_all, prod_all, status_map, sac_cfg, tenant):
    cat = str(sac_cfg["category"])
    d = deals_all[deals_all["CATEGORY_ID"] == cat].copy() if not deals_all.empty else deals_all
    st.title(f"🔧 {tenant['NAME']} · SAC / Assistência Técnica")
    if d.empty:
        st.info("Sem chamados de assistência técnica sincronizados.")
        return
    dts = d["DATE_CREATE"].dropna()
    if not dts.empty:
        dmin, dmax = dts.min().date(), dts.max().date()
        rng = st.sidebar.date_input("Período (abertura)", value=(dmin, dmax),
                                    min_value=dmin, max_value=dmax)
        if isinstance(rng, tuple) and len(rng) == 2:
            ds = d["DATE_CREATE"].dt.date
            d = d[(ds >= rng[0]) & (ds <= rng[1])]
    st.caption(f"{len(d)} chamados · pipeline Assistência Técnica")

    total = len(d)
    resolvido = int((d["Situação"] == "Ganho").sum())
    naores = int((d["Situação"] == "Perdido").sum())
    andamento = int((d["Situação"] == "Aberto").sum())
    pct = (resolvido / (resolvido + naores) * 100) if (resolvido + naores) else 0
    tempo_col = next((c for c in d.columns if c.startswith("Tempo")), None)
    tempo = None
    if tempo_col:
        ts = pd.to_numeric(d[tempo_col], errors="coerce")
        ts = ts[ts > 0].dropna()  # ignora valores inconsistentes (negativos/zero)
        tempo = ts.median() if len(ts) else None

    r = st.columns(4)
    r[0].metric("📋 Criados", fmt_int(total))
    r[1].metric("⏳ Em andamento", fmt_int(andamento))
    r[2].metric("✅ Resolvidos", fmt_int(resolvido))
    r[3].metric("❌ Não resolvidos", fmt_int(naores))
    r2 = st.columns(4)
    r2[0].metric("🎯 % Resolução", f"{pct:.1f}%")
    r2[1].metric("⏱️ Tempo mediano (min)", f"{tempo:.0f}" if tempo is not None else "—")
    st.divider()

    tabs = st.tabs(["📊 Visão geral", "🔧 Defeitos & interação", "🛍️ Produtos", "🗂️ Dados"])
    with tabs[0]:
        cc = st.columns(2)
        sit = (d.groupby("Situação").size().reindex(["Aberto", "Ganho", "Perdido"])
               .fillna(0).reset_index(name="Qtde"))
        sit["Situação"] = sit["Situação"].map(SAC_SIT)
        fig = px.pie(sit, names="Situação", values="Qtde", hole=0.5, color="Situação",
                     color_discrete_map=SAC_COLORS)
        fig.update_layout(title="Resolução", height=380, margin=dict(l=10, r=10, t=50, b=10))
        cc[0].plotly_chart(fig, width="stretch")
        dim_bar(cc[1], d, "Estágio", "Chamados por estágio")
        bym = d.groupby(["Mês criação", "Situação"]).size().reset_index(name="Qtde")
        bym = bym[bym["Mês criação"] != "NaT"]
        bym["Situação"] = bym["Situação"].map(SAC_SIT)
        if not bym.empty:
            figm = px.bar(bym, x="Mês criação", y="Qtde", color="Situação", barmode="stack",
                          color_discrete_map=SAC_COLORS)
            figm.update_layout(title="Status por mês", height=360, margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(figm, width="stretch")
        # status por dia
        dd = d.copy()
        dd["Dia"] = dd["DATE_CREATE"].dt.date
        byd = dd.dropna(subset=["Dia"]).groupby(["Dia", "Situação"]).size().reset_index(name="Qtde")
        byd["Situação"] = byd["Situação"].map(SAC_SIT)
        if not byd.empty:
            figd = px.bar(byd, x="Dia", y="Qtde", color="Situação", barmode="stack",
                          color_discrete_map=SAC_COLORS)
            figd.update_layout(title="Status por dia", height=340, margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(figd, width="stretch")

    with tabs[1]:
        cc = st.columns(2)
        dim_bar(cc[0], d, "Tipo de Interação", "Tipo de interação")
        dim_bar(cc[1], d, "Modo de Falha", "Modo de falha de reclamação")
        cc2 = st.columns(2)
        dim_bar(cc2[0], d, "Defeitos", "Defeitos", top=21)
        dim_bar(cc2[1], d, "Sugestão", "Sugestão")
        dim_bar(st, d, "Resolução", "Resolução", top=12)

    with tabs[2]:
        ids = set(d["ID"])
        p = prod_all[prod_all["DEAL_ID"].isin(ids)] if not prod_all.empty else prod_all
        if p.empty:
            st.info("Sem produtos lançados nos chamados.")
        else:
            g = (p.groupby("PRODUCT_NAME").agg(
                Criados=("DEAL_ID", "nunique"),
                Resolvidos=("Situação", lambda s: int((s == "Ganho").sum())),
                Não_Resolvidos=("Situação", lambda s: int((s == "Perdido").sum()))).reset_index())
            g["Resolução %"] = (g["Resolvidos"] / g["Criados"] * 100).round(1)
            g = g.sort_values("Criados", ascending=False)
            g.columns = ["Produto", "Criados", "Resolvidos", "Não Resolvidos", "Resolução %"]
            st.markdown("#### Produtos em chamados")
            st.dataframe(g.head(50), width="stretch", hide_index=True)
            # quantidade de produtos por tipo de defeito
            if "Defeitos" in d.columns:
                defmap = d.set_index("ID")["Defeitos"]
                pp = p.copy()
                pp["Defeito"] = pp["DEAL_ID"].map(defmap)
                pp = pp[pp["Defeito"].notna() & pp["Defeito"].astype(str).str.strip().ne("")]
                if not pp.empty:
                    gd = (pp.groupby("Defeito")["QUANTITY"].sum().sort_values(ascending=False)
                          .head(21).reset_index())
                    figdef = px.bar(gd.sort_values("QUANTITY"), x="QUANTITY", y="Defeito",
                                    orientation="h", text_auto=True)
                    figdef.update_traces(marker_color=LOST_COLOR)
                    figdef.update_layout(title="Quantidade de produtos por tipo de defeito",
                                         height=480, margin=dict(l=10, r=10, t=50, b=10))
                    st.plotly_chart(figdef, width="stretch")
            # família de produto
            if "Família" in p.columns:
                st.markdown("#### Família de produto")
                gf = (p.groupby("Família").agg(
                    Criados=("DEAL_ID", "nunique"),
                    Resolvidos=("Situação", lambda s: int((s == "Ganho").sum())),
                    Qtd=("QUANTITY", "sum")).reset_index())
                gf["Resolução %"] = (gf["Resolvidos"] / gf["Criados"] * 100).round(1)
                st.dataframe(gf.sort_values("Criados", ascending=False), width="stretch", hide_index=True)
                # gráfico de pilha: família × modo de falha
                if "Modo de Falha" in d.columns:
                    fmap2 = d.set_index("ID")["Modo de Falha"]
                    pp2 = p.copy()
                    pp2["Modo de Falha"] = pp2["DEAL_ID"].map(fmap2).fillna("não selecionada")
                    gfm = pp2.groupby(["Família", "Modo de Falha"])["QUANTITY"].sum().reset_index()
                    if not gfm.empty:
                        figpil = px.bar(gfm, x="Família", y="QUANTITY", color="Modo de Falha",
                                        barmode="stack")
                        figpil.update_layout(title="Família × Modo de falha (pilha)", height=460,
                                             margin=dict(l=10, r=10, t=50, b=10))
                        st.plotly_chart(figpil, width="stretch")

    with tabs[3]:
        st.download_button("⬇️ Chamados (CSV)", d.to_csv(index=False).encode("utf-8-sig"),
                           "sac_chamados.csv", "text/csv")
        st.dataframe(d, width="stretch", hide_index=True)


def render_produtos(prod_f):
    st.markdown("### Análise de produtos")
    if prod_f.empty:
        st.info("Nenhuma linha de produto lançada nos negócios deste período. "
                "Quando os negócios tiverem produtos no Bitrix (crm.deal.productrows), "
                "os indicadores aparecem aqui automaticamente.")
        return
    won = prod_f[prod_f["Situação"] == "Ganho"]
    k = st.columns(4)
    k[0].metric("Negócios com produto", fmt_int(prod_f["DEAL_ID"].nunique()))
    k[1].metric("Produtos distintos", fmt_int(prod_f["PRODUCT_NAME"].nunique()))
    k[2].metric("Receita em produtos (ganhos)", fmt_brl(won["TOTAL"].sum()))
    k[3].metric("Qtd vendida (ganhos)", fmt_int(won["QUANTITY"].sum()))

    base = won if not won.empty else prod_f
    rotulo = "ganhos" if not won.empty else "todos os negócios"
    g = (base.groupby("PRODUCT_NAME").agg(Receita=("TOTAL", "sum"), Qtd=("QUANTITY", "sum"),
         Negócios=("DEAL_ID", "nunique")).reset_index())
    cc = st.columns(2)
    top_r = g.sort_values("Receita").tail(15)
    fig = px.bar(top_r, x="Receita", y="PRODUCT_NAME", orientation="h", text_auto=".2s")
    fig.update_traces(marker_color=WON_COLOR)
    fig.update_layout(title=f"Top produtos por receita ({rotulo})", height=460,
                      margin=dict(l=10, r=10, t=50, b=10), yaxis_title="")
    cc[0].plotly_chart(fig, width="stretch")
    top_q = g.sort_values("Qtd").tail(15)
    fig2 = px.bar(top_q, x="Qtd", y="PRODUCT_NAME", orientation="h", text_auto=True)
    fig2.update_traces(marker_color=OPEN_COLOR)
    fig2.update_layout(title=f"Top produtos por quantidade ({rotulo})", height=460,
                       margin=dict(l=10, r=10, t=50, b=10), yaxis_title="")
    cc[1].plotly_chart(fig2, width="stretch")

    disp = g.sort_values("Receita", ascending=False).copy()
    disp["Receita"] = disp["Receita"].map(fmt_brl)
    disp["Qtd"] = disp["Qtd"].map(lambda x: f"{x:.0f}")
    disp.columns = ["Produto", "Receita", "Qtd", "Negócios"]
    st.dataframe(disp, width="stretch", hide_index=True)

    # família de produto
    if "Família" in base.columns:
        st.markdown("#### Família de produto")
        gf = (base.groupby("Família").agg(Receita=("TOTAL", "sum"), Qtd=("QUANTITY", "sum"),
              Negócios=("DEAL_ID", "nunique")).reset_index().sort_values("Receita", ascending=False))
        cf = st.columns(2)
        figf = px.bar(gf.sort_values("Receita").tail(15), x="Receita", y="Família",
                      orientation="h", text_auto=".2s")
        figf.update_traces(marker_color=WON_COLOR)
        figf.update_layout(title="Receita por família", height=420, margin=dict(l=10, r=10, t=50, b=10))
        cf[0].plotly_chart(figf, width="stretch")
        figq = px.bar(gf.sort_values("Qtd").tail(15), x="Qtd", y="Família", orientation="h", text_auto=True)
        figq.update_traces(marker_color=OPEN_COLOR)
        figq.update_layout(title="Quantidade por família", height=420, margin=dict(l=10, r=10, t=50, b=10))
        cf[1].plotly_chart(figq, width="stretch")
        gfd = gf.copy()
        gfd["Receita"] = gfd["Receita"].map(fmt_brl)
        gfd["Qtd"] = gfd["Qtd"].map(lambda x: f"{x:.0f}")
        st.dataframe(gfd, width="stretch", hide_index=True)


def render_funnel(deals_f, stage_names):
    """Funil de conversão (esteira) em barras horizontais:
    - nº de negócios que PASSARAM por cada fase (cumulativo: quem está numa fase
      posterior ou ganhou também passou pelas anteriores);
    - valor (R$) em aberto por fase;
    - valor ponderado pela taxa de conversão histórica até o ganho (previsibilidade).
    """
    if deals_f.empty or not stage_names:
        st.caption("Sem negócios para o funil.")
        return

    def cls(sid):
        s = str(sid)
        if s.endswith(":WON") or s == "WON":
            return "Ganho"
        if s.endswith(":LOSE") or s == "LOSE":
            return "Perdido"
        return "Aberto"

    items = list(stage_names.items())  # (stage_id, nome) na ordem do funil
    open_names = [name for sid, name in items if cls(sid) == "Aberto"]
    won_name = next((name for sid, name in items if cls(sid) == "Ganho"), "Ganho")
    fo = open_names + [won_name]  # fases do funil, em ordem
    cur_cnt = deals_f.groupby("Estágio").size()
    cur_val = deals_f.groupby("Estágio")["OPPORTUNITY"].sum()
    nrm = len(fo)
    # quem passou pela fase i = quem está na fase i ou em qualquer fase posterior (+ ganhos)
    reached_cnt = [int(sum(cur_cnt.get(nm, 0) for nm in fo[i:])) for i in range(nrm)]
    reached_val = [float(sum(cur_val.get(nm, 0) for nm in fo[i:])) for i in range(nrm)]
    base = reached_cnt[0] if reached_cnt and reached_cnt[0] else 0
    won_reached = reached_cnt[-1] if reached_cnt else 0

    rows, prev_total = [], 0.0
    for i, nm in enumerate(fo):
        pwin = (won_reached / reached_cnt[i]) if reached_cnt[i] else 0.0
        aberto = float(cur_val.get(nm, 0.0)) if nm in open_names else 0.0
        previsto = aberto * pwin
        prev_total += previsto
        rows.append({"Fase": nm, "Alcançaram": reached_cnt[i], "Valor alcançado": reached_val[i],
                     "Conv. vs 1ª": (reached_cnt[i] / base * 100) if base else 0,
                     "Prob. ganho": pwin * 100, "Valor em aberto": aberto, "Previsão": previsto})
    fdf = pd.DataFrame(rows)
    order_rev = fo[::-1]  # plotly desenha de baixo p/ cima: 1ª fase no topo

    c = st.columns(2)
    # funil de verdade, ordenado do maior (fase inicial) ao menor (fase final)
    fsorted = fdf.sort_values("Alcançaram", ascending=False)
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(fsorted))]
    figA = go.Figure(go.Funnel(
        y=fsorted["Fase"], x=fsorted["Alcançaram"],
        textposition="inside", textinfo="value+percent initial",
        marker={"color": colors}))
    figA.update_layout(title="Funil — negócios que passaram por cada fase", height=440,
                       margin=dict(l=10, r=10, t=50, b=10))
    c[0].plotly_chart(figA, width="stretch")

    val_m = fdf.melt(id_vars="Fase", value_vars=["Valor em aberto", "Previsão"],
                     var_name="Tipo", value_name="R$")
    figB = px.bar(val_m, x="R$", y="Fase", orientation="h", color="Tipo", barmode="group",
                  text_auto=".2s", category_orders={"Fase": order_rev},
                  color_discrete_map={"Valor em aberto": OPEN_COLOR, "Previsão": WON_COLOR})
    figB.update_layout(title="Valor por fase e previsão (ponderada pela conversão)", height=440,
                       margin=dict(l=10, r=10, t=50, b=10), xaxis_title="R$")
    c[1].plotly_chart(figB, width="stretch")

    st.metric("🔮 Previsão ponderada do pipeline aberto", fmt_brl(prev_total),
              help="Σ (valor em aberto na fase × probabilidade histórica de ganho a partir da fase).")

    show = fdf[["Fase", "Alcançaram", "Conv. vs 1ª", "Prob. ganho", "Valor alcançado",
                "Valor em aberto", "Previsão"]].copy()
    show["Conv. vs 1ª"] = show["Conv. vs 1ª"].map(lambda x: f"{x:.0f}%")
    show["Prob. ganho"] = show["Prob. ganho"].map(lambda x: f"{x:.0f}%")
    for col in ["Valor alcançado", "Valor em aberto", "Previsão"]:
        show[col] = show[col].map(fmt_brl)
    st.dataframe(show, width="stretch", hide_index=True)


def _period(series: pd.Series, gran: str) -> pd.Series:
    """Converte datas em rótulo de período conforme a granularidade escolhida."""
    if gran == "Dia":
        return series.dt.date.astype(str)
    code = {"Semana": "W", "Mês": "M", "Trimestre": "Q", "Ano": "Y"}[gran]
    return series.dt.to_period(code).astype(str)


def render_gestao(deals_t, leads_t, lfd=None):
    st.markdown("### Indicadores de gestão")
    st.caption("Visão de alto nível: geração de demanda e saúde do funil. "
               "Respeita os filtros de vendedor/fonte/período da barra lateral.")
    won = deals_t[deals_t["Situação"] == "Ganho"]
    receita = won["OPPORTUNITY"].sum()
    # neste CRM (leads_from_deals) o lead é o próprio negócio
    if lfd is not None and not deals_t.empty:
        dd = deals_t.copy()
        dd["Classe"] = classify_funnel(dd, lfd.get("lead_stages", []))
        lead_src = dd
        tot_leads = len(dd)
        neg_mask = dd["Classe"].isin(["Negócio aberto", "Negócio perdido", "Negócio ganho"])
        neg_count = int(neg_mask.sum())
        neg_df = dd[neg_mask]
    else:
        lead_src = leads_t
        tot_leads = len(leads_t)
        neg_count = len(deals_t)
        neg_df = deals_t
    k = st.columns(5)
    k[0].metric("Leads gerados", fmt_int(tot_leads))
    k[1].metric("Conversão (lead→ganho)", f"{(len(won)/tot_leads*100) if tot_leads else 0:.1f}%")
    k[2].metric("Viraram negócio", fmt_int(neg_count))
    k[3].metric("Ganhos", fmt_int(len(won)))
    k[4].metric("Receita ganha", fmt_brl(receita))

    st.markdown("#### Geração de demanda")
    gran = st.radio("Granularidade", ["Dia", "Semana", "Mês", "Trimestre", "Ano"],
                    index=2, horizontal=True, key="gran_gestao")
    lead_s = lead_src.assign(p=_period(lead_src["DATE_CREATE"], gran)).groupby("p").size().rename("Leads")
    deal_s = neg_df.assign(p=_period(neg_df["DATE_CREATE"], gran)).groupby("p").size().rename("Negócios")
    won_s = won.assign(p=_period(won["DATE_CREATE"], gran)).groupby("p").size().rename("Ganhos")
    dem = pd.concat([lead_s, deal_s, won_s], axis=1).fillna(0).reset_index().rename(columns={"p": "Período"})
    dem = dem[dem["Período"] != "NaT"].sort_values("Período")
    if not dem.empty:
        dem["% vira negócio"] = (dem["Negócios"] / dem["Leads"].replace(0, float("nan")) * 100).round(1)
        dem["% Conversão"] = (dem["Ganhos"] / dem["Leads"].replace(0, float("nan")) * 100).round(1)
        fig = go.Figure()
        for tipo, cor in [("Leads", META_COLOR), ("Negócios", OPEN_COLOR), ("Ganhos", WON_COLOR)]:
            fig.add_bar(x=dem["Período"], y=dem[tipo], name=tipo, marker_color=cor)
        fig.add_trace(go.Scatter(x=dem["Período"], y=dem["% Conversão"], name="% Conversão",
                                 yaxis="y2", mode="lines+markers+text",
                                 text=dem["% Conversão"].map(lambda v: f"{v:.0f}%" if pd.notna(v) else ""),
                                 textposition="top center", line=dict(color="#dc2626")))
        ymax = dem["% Conversão"].max()
        fig.update_layout(title=f"Leads, negócios e ganhos por {gran.lower()}", height=440,
                          barmode="group", margin=dict(l=10, r=10, t=50, b=10),
                          yaxis2=dict(title="% Conversão", overlaying="y", side="right",
                                      showgrid=False, range=[0, max(1, (ymax or 0) * 1.4)]))
        st.plotly_chart(fig, width="stretch")
        show = dem.copy()
        for c in ["Leads", "Negócios", "Ganhos"]:
            show[c] = show[c].astype(int)
        show["% vira negócio"] = show["% vira negócio"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
        show["% Conversão"] = show["% Conversão"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
        st.dataframe(show, width="stretch", hide_index=True)

    st.markdown("#### Geração de leads por origem e conversão para ganho")
    lg = leads_t.groupby("Fonte").agg(Leads=("ID", "count"), Conv=("Convertido", "sum")).reset_index()
    dg = (deals_t.assign(_g=(deals_t["Situação"] == "Ganho").astype(int))
          .groupby("Fonte").agg(Negócios=("ID", "count"), Ganhos=("_g", "sum")).reset_index())
    org = lg.merge(dg, on="Fonte", how="outer").fillna(0)
    org["Conv. lead %"] = (org["Conv"] / org["Leads"].replace(0, float("nan")) * 100).round(1)
    org["Win %"] = (org["Ganhos"] / org["Negócios"].replace(0, float("nan")) * 100).round(1)
    org = org.sort_values("Leads", ascending=False)
    if not org.empty and org["Leads"].sum() > 0:
        fig2 = px.bar(org.sort_values("Leads").tail(15), x="Leads", y="Fonte",
                      orientation="h", text_auto=True)
        fig2.update_traces(marker_color=META_COLOR)
        fig2.update_layout(title="Leads por origem", height=460, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig2, width="stretch")
    disp = org.drop(columns=["Conv"]).copy()
    for c in ["Conv. lead %", "Win %"]:
        disp[c] = disp[c].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
    for c in ["Leads", "Negócios", "Ganhos"]:
        disp[c] = disp[c].map(fmt_int)
    st.dataframe(disp, width="stretch", hide_index=True)


SPA_SIT_COLORS = {"Concluído": WON_COLOR, "Insucesso": LOST_COLOR,
                  "Novo": OPEN_COLOR, "Em andamento": META_COLOR}


def render_spa(df, label):
    st.markdown(f"### {label}")
    if df.empty:
        st.info(f"Sem itens de '{label}' no filtro atual.")
        return
    total = len(df)
    novo = int((df["Situação"] == "Novo").sum())
    andamento = int((df["Situação"] == "Em andamento").sum())
    ok = int((df["Situação"] == "Concluído").sum())
    fail = int((df["Situação"] == "Insucesso").sum())
    fechados = ok + fail
    taxa_ok = (ok / fechados * 100) if fechados else 0
    k = st.columns(4)
    k[0].metric("Total", fmt_int(total))
    k[1].metric("Em aberto", fmt_int(novo + andamento), help="Novos + em andamento")
    k[2].metric("Concluídos", fmt_int(ok))
    k[3].metric("Taxa de sucesso", f"{taxa_ok:.1f}%", help=f"{ok} de {fechados} finalizados")

    cc = st.columns(2)
    by_st = df.groupby("Estágio").size().reset_index(name="Qtde").sort_values("Qtde")
    fig = px.bar(by_st, x="Qtde", y="Estágio", orientation="h", text_auto=True)
    fig.update_layout(title="Por estágio", height=420, margin=dict(l=10, r=10, t=50, b=10))
    cc[0].plotly_chart(fig, width="stretch")

    by_mon = df.groupby(["Mês criação", "Situação"]).size().reset_index(name="Qtde")
    by_mon = by_mon[by_mon["Mês criação"] != "NaT"]
    if not by_mon.empty:
        fig2 = px.bar(by_mon, x="Mês criação", y="Qtde", color="Situação", barmode="stack",
                      color_discrete_map=SPA_SIT_COLORS)
        fig2.update_layout(title="Por mês (criação)", height=420, margin=dict(l=10, r=10, t=50, b=10))
        cc[1].plotly_chart(fig2, width="stretch")

    cc2 = st.columns(2)
    dim_bar(cc2[0], df, "Vendedor", "Por responsável")
    dim_bar(cc2[1], df, "Fonte", "Por fonte")


def render_metas(tenant, user, deals_t, user_map):
    st.markdown("### Metas por vendedor")
    can_edit = user["ROLE"] in ("master", "client")  # gestores podem editar
    hoje = datetime.now()
    cols = st.columns(3)
    ano = cols[0].number_input("Ano", min_value=2020, max_value=2100, value=hoje.year, step=1)
    mes = cols[1].selectbox("Mês", list(range(1, 13)), index=hoje.month - 1,
                            format_func=lambda m: MESES_PT[m])
    periodo = f"{int(ano):04d}-{int(mes):02d}"

    # vendedores que têm negócios (id -> nome)
    ids = sorted(deals_t["ASSIGNED_BY_ID"].dropna().unique().tolist())
    if not ids:
        st.info("Sem vendedores com negócios para definir metas.")
        return
    id2name = {i: user_map.get(i, f"ID {i}") for i in ids}

    qdf = db.quotas_df(tenant["ID"], year=int(ano))
    qcur = qdf[qdf["MONTH"] == int(mes)] if not qdf.empty else qdf
    meta_map = dict(zip(qcur["ASSIGNED_BY_ID"].astype(str), qcur["TARGET_VALUE"])) if not qcur.empty else {}

    base = pd.DataFrame({
        "ASSIGNED_BY_ID": ids,
        "Vendedor": [id2name[i] for i in ids],
        "Meta (R$)": [float(meta_map.get(i, 0.0)) for i in ids],
    })

    if can_edit:
        st.caption("Edite as metas do mês e clique em salvar.")
        edited = st.data_editor(
            base, hide_index=True, width='stretch', key=f"meta_{periodo}",
            disabled=["ASSIGNED_BY_ID", "Vendedor"],
            column_config={"Meta (R$)": st.column_config.NumberColumn(format="%.2f", min_value=0.0)},
        )
        if st.button("💾 Salvar metas", type="primary"):
            for _, row in edited.iterrows():
                db.set_quota(tenant["ID"], row["ASSIGNED_BY_ID"], int(ano), int(mes),
                             float(row["Meta (R$)"] or 0))
            st.success("Metas salvas.")
            st.rerun()
        metas = dict(zip(edited["ASSIGNED_BY_ID"].astype(str), edited["Meta (R$)"]))
    else:
        metas = meta_map

    # realizado x meta no período (por data de fechamento, negócios ganhos)
    won_m = deals_t[(deals_t["Situação"] == "Ganho") & (deals_t["Mês fechamento"] == periodo)]
    real = won_m.groupby("ASSIGNED_BY_ID")["OPPORTUNITY"].sum().to_dict()

    rows = []
    for i in ids:
        m = float(metas.get(i, 0) or 0)
        r = float(real.get(i, 0) or 0)
        rows.append({"Vendedor": id2name[i], "Meta": m, "Realizado": r,
                     "Atingimento %": round(r / m * 100, 1) if m else None})
    comp = pd.DataFrame(rows)

    tot_meta, tot_real = comp["Meta"].sum(), comp["Realizado"].sum()
    k = st.columns(3)
    k[0].metric(f"Meta {MESES_PT[int(mes)]}/{int(ano)}", fmt_brl(tot_meta))
    k[1].metric("Realizado", fmt_brl(tot_real))
    k[2].metric("Atingimento", f"{(tot_real/tot_meta*100):.1f}%" if tot_meta else "—")

    plot = comp.melt(id_vars="Vendedor", value_vars=["Meta", "Realizado"],
                     var_name="Tipo", value_name="Valor")
    if plot["Valor"].sum() > 0:
        fig = px.bar(plot, x="Vendedor", y="Valor", color="Tipo", barmode="group",
                     color_discrete_map={"Meta": META_COLOR, "Realizado": WON_COLOR}, text_auto=".2s")
        fig.update_layout(title=f"Meta × Realizado — {MESES_PT[int(mes)]}/{int(ano)}",
                          height=420, yaxis_title="R$", margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, width='stretch')
    disp = comp.copy()
    disp["Meta"] = disp["Meta"].map(fmt_brl)
    disp["Realizado"] = disp["Realizado"].map(fmt_brl)
    disp["Atingimento %"] = disp["Atingimento %"].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
    st.dataframe(disp, width='stretch', hide_index=True)


def render_mom(deals_t, leads_t):
    st.markdown("### Comparação mês a mês")
    won = deals_t[deals_t["Situação"] == "Ganho"]
    rev = won.groupby("Mês fechamento")["OPPORTUNITY"].sum().rename("Receita ganha")
    cnt = won.groupby("Mês fechamento").size().rename("Negócios ganhos")
    created = deals_t.groupby("Mês criação").size().rename("Negócios criados")
    leadm = leads_t.groupby("Mês criação").size().rename("Leads criados")
    convm = leads_t.groupby("Mês criação")["Convertido"].mean().mul(100).round(1).rename("Conversão %")

    monthly = pd.concat([rev, cnt, created, leadm, convm], axis=1).fillna(0)
    monthly = monthly[monthly.index != "NaT"].sort_index()
    if monthly.empty:
        st.info("Sem histórico suficiente.")
        return

    if len(monthly) >= 2:
        cur, prev = monthly.iloc[-1], monthly.iloc[-2]
        st.caption(f"Comparando **{monthly.index[-1]}** com **{monthly.index[-2]}**")
        k = st.columns(4)

        def delta_pct(a, b):
            if b == 0:
                return None
            return (a - b) / b * 100

        d = delta_pct(cur["Receita ganha"], prev["Receita ganha"])
        k[0].metric("Receita ganha", fmt_brl(cur["Receita ganha"]),
                    f"{d:+.1f}%" if d is not None else None)
        d = delta_pct(cur["Negócios ganhos"], prev["Negócios ganhos"])
        k[1].metric("Negócios ganhos", fmt_int(cur["Negócios ganhos"]),
                    f"{d:+.1f}%" if d is not None else None)
        d = delta_pct(cur["Leads criados"], prev["Leads criados"])
        k[2].metric("Leads criados", fmt_int(cur["Leads criados"]),
                    f"{d:+.1f}%" if d is not None else None)
        k[3].metric("Conversão de leads", f"{cur['Conversão %']:.1f}%",
                    f"{cur['Conversão %'] - prev['Conversão %']:+.1f} p.p.")

    fig = go.Figure()
    fig.add_bar(x=monthly.index, y=monthly["Receita ganha"], name="Receita ganha", marker_color=WON_COLOR)
    fig.add_trace(go.Scatter(x=monthly.index, y=monthly["Negócios ganhos"], name="Negócios ganhos",
                             yaxis="y2", mode="lines+markers", line=dict(color=OPEN_COLOR)))
    fig.update_layout(title="Receita ganha e nº de negócios por mês", height=420,
                      yaxis=dict(title="R$"), yaxis2=dict(title="Negócios", overlaying="y", side="right"),
                      margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, width='stretch')

    show = monthly.copy()
    show["Receita ganha"] = show["Receita ganha"].map(fmt_brl)
    show["Receita MoM %"] = monthly["Receita ganha"].pct_change().mul(100).round(1).map(
        lambda x: f"{x:+.1f}%" if pd.notna(x) else "—")
    for col in ["Negócios ganhos", "Negócios criados", "Leads criados"]:
        show[col] = show[col].map(fmt_int)
    show["Conversão %"] = monthly["Conversão %"].map(lambda x: f"{x:.1f}%")
    st.dataframe(show.reset_index().rename(columns={"index": "Mês"}),
                 width='stretch', hide_index=True)

    # Receita anual (ano a ano) — uma linha por ano, eixo = mês
    st.markdown("#### Receita anual (ano a ano)")
    w = won.dropna(subset=["Fechamento"]).copy()
    if not w.empty:
        w["Ano"] = w["Fechamento"].dt.year.astype(int)
        w["MesNum"] = w["Fechamento"].dt.month.astype(int)
        yoy = w.groupby(["Ano", "MesNum"])["OPPORTUNITY"].sum().reset_index()
        yoy["Mês"] = yoy["MesNum"].map(lambda m: MESES_PT[m])
        figy = px.line(yoy.sort_values("MesNum"), x="Mês", y="OPPORTUNITY", color="Ano",
                       markers=True, category_orders={"Mês": MESES_PT[1:]})
        figy.update_layout(title="Receita ganha por mês, por ano (R$)", height=380,
                           yaxis_title="R$", margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(figy, width="stretch")


# ====================================================================== admin
def render_admin():
    st.markdown("### ⚙️ Administração (master)")
    t1, t2, t3 = st.tabs(["Ambientes", "Usuários", "Sincronização"])

    with t1:
        st.markdown("#### Novo ambiente (cliente)")
        with st.form("novo_tenant"):
            nome = st.text_input("Nome da empresa")
            webhook = st.text_input("Webhook Bitrix24", placeholder="https://portal.bitrix24.com.br/rest/<id>/<token>/")
            cat = st.text_input("CATEGORY_ID do funil de vendas", value="16")
            if st.form_submit_button("Criar ambiente", type="primary"):
                if nome and webhook:
                    try:
                        tid = db.add_tenant(nome.strip(), webhook.strip(), cat.strip() or "16")
                        st.success(f"Ambiente criado (ID {tid}). Rode a 1ª sincronização na aba Sincronização.")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Erro: {e}")
                else:
                    st.warning("Informe nome e webhook.")
        st.markdown("#### Ambientes cadastrados")
        for t in db.list_tenants(active_only=False):
            with st.expander(f"[{t['ID']}] {t['NAME']} {'' if t['ACTIVE'] else '(inativo)'}"):
                wh = st.text_input("Webhook", value=t["WEBHOOK"], key=f"wh_{t['ID']}")
                cc = st.columns(3)
                catv = cc[0].text_input("CATEGORY_ID", value=t["SALES_CATEGORY_ID"], key=f"cat_{t['ID']}")
                ativo = cc[1].checkbox("Ativo", value=bool(t["ACTIVE"]), key=f"act_{t['ID']}")
                if cc[2].button("Salvar", key=f"save_{t['ID']}"):
                    db.update_tenant(t["ID"], WEBHOOK=wh.strip(), SALES_CATEGORY_ID=catv.strip(),
                                     ACTIVE=1 if ativo else 0)
                    st.success("Atualizado.")
                    st.rerun()
                st.caption("Mapa de campos personalizados (JSON) — códigos UF deste portal "
                           "para segmento, cargo, motivo e o SPA de reuniões.")
                fm_txt = st.text_area(
                    "FIELD_MAP", value=json.dumps(db.get_field_map(t["ID"]), ensure_ascii=False, indent=2),
                    height=200, key=f"fm_{t['ID']}")
                if st.button("Salvar mapa de campos", key=f"savefm_{t['ID']}"):
                    try:
                        db.set_field_map(t["ID"], json.loads(fm_txt))
                        st.success("Mapa salvo. Rode uma sincronização completa para reprocessar.")
                    except ValueError as e:
                        st.error(f"JSON inválido: {e}")

    with t2:
        st.markdown("#### Novo usuário")
        tenants = db.list_tenants(active_only=False)
        topt = {f"[{t['ID']}] {t['NAME']}": t["ID"] for t in tenants}
        with st.form("novo_user"):
            u = st.text_input("Usuário (login)")
            p = st.text_input("Senha", type="password")
            nm = st.text_input("Nome")
            role = st.selectbox("Papel", ["client", "master"])
            tsel = st.selectbox("Ambiente (para client)", ["—"] + list(topt.keys()))
            if st.form_submit_button("Criar usuário", type="primary"):
                tid = topt.get(tsel) if role == "client" else None
                if not u or not p:
                    st.warning("Informe usuário e senha.")
                elif role == "client" and tid is None:
                    st.warning("Selecione o ambiente do usuário client.")
                elif db.get_user(u.strip()):
                    st.error("Usuário já existe.")
                else:
                    auth.create_user(u.strip(), p, role=role, tenant_id=tid, name=nm)
                    st.success(f"Usuário '{u}' criado.")
        st.markdown("#### Usuários")
        for usr in db.list_users():
            cc = st.columns([3, 2, 2, 2])
            cc[0].write(f"**{usr['USERNAME']}** ({usr['NAME'] or '—'})")
            cc[1].write(f"papel: {usr['ROLE']}")
            cc[2].write(f"tenant: {usr['TENANT_ID'] if usr['TENANT_ID'] else '—'}")
            if usr["ROLE"] != "master":
                lbl = "Inativar" if usr["ACTIVE"] else "Ativar"
                if cc[3].button(lbl, key=f"tog_{usr['ID']}"):
                    db.set_user_active(usr["USERNAME"], not usr["ACTIVE"])
                    st.rerun()

    with t3:
        st.markdown("#### Sincronizar agora")
        for t in db.list_tenants(active_only=True):
            s = db.get_sync(t["ID"])
            last = s["LAST_RUN"].replace("T", " ") if s and s.get("LAST_RUN") else "nunca"
            cc = st.columns([3, 3, 2, 2])
            cc[0].write(f"**{t['NAME']}**")
            cc[1].caption(f"última: {last}")
            if cc[2].button("Incremental", key=f"sinc_{t['ID']}"):
                with st.spinner("Sincronizando…"):
                    r = sync_tenant(t)
                st.success(r) if r.get("ok") else st.error(r)
            if cc[3].button("Completa", key=f"full_{t['ID']}"):
                with st.spinner("Carga completa…"):
                    r = sync_tenant(t, full=True)
                st.success(r) if r.get("ok") else st.error(r)


# ====================================================================== main
def main():
    if "user" not in st.session_state:
        render_login()

    user = st.session_state.user

    with st.sidebar:
        st.markdown(f"### {APP_NAME}")
        st.markdown(f"👤 **{user.get('NAME') or user['USERNAME']}**")
        st.caption("Master (ZOPU)" if user["ROLE"] == "master" else "Cliente")
        if st.button("Sair"):
            del st.session_state.user
            st.rerun()
        st.divider()

    # seleção de ambiente
    if user["ROLE"] == "master":
        tenants = db.list_tenants(active_only=False)
        if not tenants:
            st.warning("Nenhum ambiente cadastrado. Crie um na Administração abaixo.")
            render_admin()
            return
        opt = {f"{t['NAME']}": t["ID"] for t in tenants}
        with st.sidebar:
            sel = st.selectbox("🏢 Ambiente", list(opt.keys()))
            do_sync = st.button("🔄 Sincronizar agora")
        tenant = db.get_tenant(opt[sel])
        if do_sync:
            with st.spinner("Sincronizando…"):
                sync_tenant(tenant)
            st.rerun()
    else:
        tenant = db.get_tenant(user["TENANT_ID"])
        if not tenant:
            st.error("Seu usuário não está vinculado a um ambiente. Contate a ZOPU.")
            return
        with st.sidebar:
            if st.button("🔄 Sincronizar agora"):
                with st.spinner("Sincronizando…"):
                    sync_tenant(tenant)
                st.rerun()

    maybe_autosync(tenant)
    render_dashboard(tenant, user)

    if user["ROLE"] == "master":
        st.divider()
        with st.expander("⚙️ Administração (master)"):
            render_admin()


if __name__ == "__main__":
    main()
