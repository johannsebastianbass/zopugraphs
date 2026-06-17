"""Painel Comercial multi-empresa ZOPU — Bitrix24 (Leads & Deals).

Login -> seleção de ambiente (master vê todos) -> dashboard lido do SQLite.
Inclui metas/quota por vendedor e comparação mês a mês.

Execute com:  streamlit run app.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import auth
import db
from sync import sync_tenant

st.set_page_config(page_title="Painel Comercial ZOPU", page_icon="📊", layout="wide")

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


def build_deals_df(df, status_map, user_map, category_id) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["OPPORTUNITY"] = pd.to_numeric(df["OPPORTUNITY"], errors="coerce").fillna(0.0)
    df["DATE_CREATE"] = to_dt(df["DATE_CREATE"])
    df["CLOSEDATE"] = to_dt(df["CLOSEDATE"])
    df["ASSIGNED_BY_ID"] = df["ASSIGNED_BY_ID"].astype(str)
    df["Vendedor"] = df["ASSIGNED_BY_ID"].map(user_map).fillna("ID " + df["ASSIGNED_BY_ID"])
    stage_names = status_map.get(f"DEAL_STAGE_{category_id}", {})
    df["Estágio"] = df["STAGE_ID"].map(stage_names).fillna(df["STAGE_ID"])
    src = status_map.get("SOURCE", {})
    df["Fonte"] = df["SOURCE_ID"].map(src).fillna(df["SOURCE_ID"]).fillna("—")

    def situacao(stage: str) -> str:
        if str(stage).endswith(":WON") or stage == "WON":
            return "Ganho"
        if str(stage).endswith(":LOSE") or stage == "LOSE":
            return "Perdido"
        return "Aberto"

    df["Situação"] = df["STAGE_ID"].apply(situacao)
    df["Ciclo (dias)"] = (df["CLOSEDATE"] - df["DATE_CREATE"]).dt.days
    df["Mês criação"] = df["DATE_CREATE"].dt.to_period("M").astype(str)
    df["Mês fechamento"] = df["CLOSEDATE"].dt.to_period("M").astype(str)
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


def build_meetings_df(df, status_map, user_map) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["DATE_CREATE"] = to_dt(df["CREATED_TIME"])
    df["ASSIGNED_BY_ID"] = df["ASSIGNED_BY_ID"].astype(str)
    df["Vendedor"] = df["ASSIGNED_BY_ID"].map(user_map).fillna("ID " + df["ASSIGNED_BY_ID"])
    df["Fonte"] = df["SOURCE_ID"].map(status_map.get("SOURCE", {})).fillna(df["SOURCE_ID"]).fillna("—")
    smap = {}
    for k, v in status_map.items():
        if k.startswith("DYNAMIC_1050_STAGE_"):
            smap.update(v)
    df["Estágio"] = df["STAGE_ID"].map(smap).fillna(df["STAGE_ID"])

    def sit(s):
        s = str(s)
        if s.endswith(":SUCCESS"):
            return "Participou"
        if s.endswith(":FAIL"):
            return "No-show"
        if s.endswith(":NEW"):
            return "Agendada"
        return "Outros"

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


# ====================================================================== login
def render_login():
    st.title("📊 Painel Comercial ZOPU")
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
def render_dashboard(tenant: dict, user: dict):
    meta = db.get_meta(tenant["ID"])
    status_map, user_map = meta["status_map"], meta["user_map"]
    cat_id = tenant["SALES_CATEGORY_ID"]
    cat_name = meta["categories"].get(cat_id, f"Funil {cat_id}")

    deals_all = build_deals_df(db.deals_df(tenant["ID"]), status_map, user_map, cat_id)
    leads_all = build_leads_df(db.leads_df(tenant["ID"]), status_map, user_map)
    meet_all = build_meetings_df(db.meetings_df(tenant["ID"]), status_map, user_map)

    sync = db.get_sync(tenant["ID"])
    st.title(f"📊 {tenant['NAME']}")
    info = (f"Funil: **{cat_name}** · {len(deals_all)} negócios · {len(leads_all)} leads"
            f" · {len(meet_all)} reuniões")
    if sync and sync.get("LAST_RUN"):
        info += f" · última sync: {sync['LAST_RUN'].replace('T', ' ')}"
    st.caption(info)

    if deals_all.empty and leads_all.empty:
        st.info("Sem dados sincronizados ainda. Use **Sincronizar agora** na barra lateral.")
        return

    # ---------------- filtros ----------------
    st.sidebar.header("Filtros")
    all_dates = pd.concat([
        deals_all.get("DATE_CREATE", pd.Series(dtype="datetime64[ns]")),
        leads_all.get("DATE_CREATE", pd.Series(dtype="datetime64[ns]")),
    ]).dropna()
    if not all_dates.empty:
        dmin, dmax = all_dates.min().date(), all_dates.max().date()
        rng = st.sidebar.date_input("Período (criação)", value=(dmin, dmax),
                                    min_value=dmin, max_value=dmax)
        d0, d1 = rng if isinstance(rng, tuple) and len(rng) == 2 else (dmin, dmax)
    else:
        d0 = d1 = None

    vendedores = sorted(set(deals_all.get("Vendedor", [])) | set(leads_all.get("Vendedor", [])))
    sel_vend = st.sidebar.multiselect("Vendedor", vendedores, default=vendedores)
    fontes = sorted(set(deals_all.get("Fonte", [])) | set(leads_all.get("Fonte", [])))
    sel_fonte = st.sidebar.multiselect("Fonte", fontes, default=fontes)

    def flt(df, use_date=True):
        if df.empty:
            return df
        m = pd.Series(True, index=df.index)
        if use_date and d0 is not None:
            ds = df["DATE_CREATE"].dt.date
            m &= (ds >= d0) & (ds <= d1)
        if sel_vend:
            m &= df["Vendedor"].isin(sel_vend)
        if sel_fonte:
            m &= df["Fonte"].isin(sel_fonte)
        return df[m]

    deals_f, leads_f = flt(deals_all), flt(leads_all)
    deals_t, leads_t = flt(deals_all, use_date=False), flt(leads_all, use_date=False)
    meet_f = flt(meet_all)

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

    st.subheader("Indicadores-chave")
    r1 = st.columns(4)
    r1[0].metric("💰 Valor ganho", fmt_brl(valor_ganho))
    r1[1].metric("🟦 Pipeline aberto", fmt_brl(valor_aberto))
    r1[2].metric("🎯 Win rate", f"{win_rate:.1f}%")
    r1[3].metric("🧾 Ticket médio", fmt_brl(ticket))
    r2 = st.columns(4)
    r2[0].metric("📇 Leads", fmt_int(total_leads))
    r2[1].metric("✅ Conversão de leads", f"{taxa_conv:.1f}%", help=f"{conv_leads}/{total_leads}")
    r2[2].metric("🤝 Negócios ganhos", fmt_int(len(won)))
    r2[3].metric("⏱️ Ciclo (mediana)", f"{ciclo:.0f} dias" if ciclo else "—")
    st.divider()

    tabs = st.tabs(["📈 Visão geral", "🛒 Pipeline", "📇 Leads", "🤝 Reuniões", "👤 Vendedores",
                    "🌐 Fontes", "🎯 Metas", "📅 Mês a mês", "🗂️ Dados"])
    stage_names = status_map.get(f"DEAL_STAGE_{cat_id}", {})
    order = list(stage_names.values())

    # -------- visão geral --------
    with tabs[0]:
        c = st.columns(2)
        funnel = deals_f.groupby("Estágio")["OPPORTUNITY"].agg(["count", "sum"]).reset_index()
        funnel["ord"] = funnel["Estágio"].apply(lambda x: order.index(x) if x in order else 999)
        funnel = funnel.sort_values("ord")
        if not funnel.empty:
            fig = go.Figure(go.Funnel(y=funnel["Estágio"], x=funnel["count"],
                                      textinfo="value+percent initial",
                                      marker={"color": PALETTE[: len(funnel)]}))
            fig.update_layout(title="Funil de negócios (nº)", height=420,
                              margin=dict(l=10, r=10, t=50, b=10))
            c[0].plotly_chart(fig, width='stretch')
        sit = (deals_f.groupby("Situação")["OPPORTUNITY"].sum()
               .reindex(["Aberto", "Ganho", "Perdido"]).fillna(0).reset_index())
        fig2 = px.bar(sit, x="Situação", y="OPPORTUNITY", text_auto=".2s", color="Situação",
                      color_discrete_map={"Ganho": WON_COLOR, "Perdido": LOST_COLOR, "Aberto": OPEN_COLOR})
        fig2.update_layout(title="Valor por situação (R$)", height=420, showlegend=False,
                           yaxis_title="R$", margin=dict(l=10, r=10, t=50, b=10))
        c[1].plotly_chart(fig2, width='stretch')

        st.markdown("#### Forecast ponderado (pipeline aberto)")
        open_only = [s for s in order if s not in ("Negócios ganho", "Negócio perdido")]
        n = len(open_only)
        prob = {s: round((i + 1) / (n + 1), 2) for i, s in enumerate(open_only)} if n else {}
        fc = opend.copy()
        fc["Prob"] = fc["Estágio"].map(prob).fillna(0.3)
        fc["Ponderado"] = fc["OPPORTUNITY"] * fc["Prob"]
        st.metric("Previsão ponderada de fechamento", fmt_brl(fc["Ponderado"].sum()),
                  help="Σ (valor × probabilidade do estágio) dos negócios abertos.")
        if not fc.empty:
            g = (fc.groupby("Estágio").agg(Qtde=("ID", "count"), Prob=("Prob", "first"),
                 Aberto=("OPPORTUNITY", "sum"), Ponderado=("Ponderado", "sum")).reset_index())
            g["ord"] = g["Estágio"].apply(lambda x: order.index(x) if x in order else 999)
            g = g.sort_values("ord")
            g["Prob"] = (g["Prob"] * 100).map(lambda x: f"{x:.0f}%")
            g["Aberto"] = g["Aberto"].map(fmt_brl)
            g["Ponderado"] = g["Ponderado"].map(fmt_brl)
            g.columns = ["Estágio", "Qtde", "Probabilidade", "Valor aberto", "Valor ponderado", "ord"]
            st.dataframe(g.drop(columns="ord"), width='stretch', hide_index=True)

    # -------- pipeline --------
    with tabs[1]:
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

    # -------- leads --------
    with tabs[2]:
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

    # -------- reuniões --------
    with tabs[3]:
        render_meetings(meet_f, status_map)

    # -------- vendedores --------
    with tabs[4]:
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
            agg["Win rate %"] = (agg["Ganhos"] / fech.replace(0, pd.NA) * 100).round(1)
            agg = agg.sort_values("Valor ganho", ascending=False)
            fig = px.bar(agg, x="Vendedor", y="Valor ganho", text_auto=".2s")
            fig.update_traces(marker_color=WON_COLOR)
            fig.update_layout(title="Valor ganho por vendedor", height=400,
                              margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(fig, width='stretch')
            disp = agg.copy()
            disp["Valor ganho"] = disp["Valor ganho"].map(fmt_brl)
            disp["Pipeline aberto"] = disp["Pipeline aberto"].map(fmt_brl)
            st.dataframe(disp, width='stretch', hide_index=True)

    # -------- fontes --------
    with tabs[5]:
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

    # -------- metas --------
    with tabs[6]:
        render_metas(tenant, user, deals_t, user_map)

    # -------- mês a mês --------
    with tabs[7]:
        render_mom(deals_t, leads_t)

    # -------- dados --------
    with tabs[8]:
        st.download_button("⬇️ Deals (CSV)", deals_f.to_csv(index=False).encode("utf-8-sig"),
                           "deals.csv", "text/csv")
        st.dataframe(deals_f, width='stretch', hide_index=True)
        st.download_button("⬇️ Leads (CSV)", leads_f.to_csv(index=False).encode("utf-8-sig"),
                           "leads.csv", "text/csv")
        st.dataframe(leads_f, width='stretch', hide_index=True)
        if not meet_f.empty:
            st.download_button("⬇️ Reuniões (CSV)", meet_f.to_csv(index=False).encode("utf-8-sig"),
                               "reunioes.csv", "text/csv")
            st.dataframe(meet_f, width='stretch', hide_index=True)


def render_meetings(meet_f, status_map):
    st.markdown("### Reuniões (SPA 1050)")
    if meet_f.empty:
        st.info("Sem reuniões no filtro atual.")
        return
    total = len(meet_f)
    agend = int((meet_f["Situação"] == "Agendada").sum())
    part = int((meet_f["Situação"] == "Participou").sum())
    nosh = int((meet_f["Situação"] == "No-show").sum())
    base = part + nosh
    taxa_noshow = (nosh / base * 100) if base else 0
    taxa_comp = (part / base * 100) if base else 0
    k = st.columns(4)
    k[0].metric("Total de reuniões", fmt_int(total))
    k[1].metric("Agendadas (em aberto)", fmt_int(agend))
    k[2].metric("Comparecimento", f"{taxa_comp:.1f}%", help=f"{part} participaram")
    k[3].metric("No-show", f"{taxa_noshow:.1f}%", help=f"{nosh} faltas")

    cc = st.columns(2)
    by_sit = meet_f.groupby("Estágio").size().reset_index(name="Qtde").sort_values("Qtde")
    fig = px.bar(by_sit, x="Qtde", y="Estágio", orientation="h", text_auto=True)
    fig.update_layout(title="Reuniões por estágio", height=400, margin=dict(l=10, r=10, t=50, b=10))
    cc[0].plotly_chart(fig, width="stretch")

    by_mon = meet_f.groupby(["Mês criação", "Situação"]).size().reset_index(name="Qtde")
    by_mon = by_mon[by_mon["Mês criação"] != "NaT"]
    if not by_mon.empty:
        fig2 = px.bar(by_mon, x="Mês criação", y="Qtde", color="Situação", barmode="stack",
                      color_discrete_map={"Participou": WON_COLOR, "No-show": LOST_COLOR,
                                          "Agendada": OPEN_COLOR, "Outros": "#9ca3af"})
        fig2.update_layout(title="Reuniões por mês", height=400, margin=dict(l=10, r=10, t=50, b=10))
        cc[1].plotly_chart(fig2, width="stretch")

    cc2 = st.columns(2)
    dim_bar(cc2[0], meet_f, "Vendedor", "Reuniões por responsável")
    dim_bar(cc2[1], meet_f, "Fonte", "Reuniões por fonte")


def render_metas(tenant, user, deals_t, user_map):
    st.markdown("### Metas por vendedor")
    can_edit = user["ROLE"] in ("master", "client")  # gestores podem editar
    hoje = datetime(2026, 6, 17)  # data corrente do sistema
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
