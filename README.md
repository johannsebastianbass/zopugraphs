# Painel Comercial ZOPU — Bitrix24 (multi-empresa)

Sistema web em **Python + Streamlit** que lê **Leads** e **Deals** do Bitrix24 de
**várias empresas clientes** (multi-tenant), com **login**, **sincronização
incremental de hora em hora**, **metas por vendedor** e **comparação mês a mês**.

## Arquitetura

```
Bitrix24 (1 webhook por empresa)
        │  sync.py (incremental, DATE_MODIFY das últimas 1h30)
        ▼
   SQLite (zopu.db)  ──►  app.py (Streamlit: login + dashboards)
```

- `bitrix.py` — cliente REST (paginação + filtro por data de modificação).
- `db.py` — SQLite: tenants, usuários, cache de leads/deals, metadados, metas, log de sync.
- `auth.py` — login com senha em PBKDF2-HMAC-SHA256.
- `sync.py` — sincronização incremental (agendada) / completa.
- `manage.py` — CLI de administração (criar master, tenants, usuários).
- `app.py` — dashboard Streamlit (login → ambiente → KPIs/metas/MoM/admin).
- `run_sync.bat` — chamado pelo Agendador de Tarefas do Windows a cada 1h.

## Perfis de acesso
- **master (ZOPU):** vê **todos** os ambientes (seletor na barra lateral) e tem o
  painel de **Administração** (criar empresas/usuários, sincronizar).
- **client (empresa):** vê **somente** o ambiente vinculado ao seu usuário.

## Credenciais

As senhas **não** ficam no repositório. Crie/altere usuários localmente:

```bash
python manage.py init --master-pass SUA_SENHA_MASTER
python manage.py add-user --username cliente --password SENHA --role client --tenant 1
python manage.py passwd --username master --password NOVA   # trocar senha
```

> As senhas reais desta instalação foram entregues à parte (fora do Git).

## Como rodar o painel
```bash
pip install -r requirements.txt
streamlit run app.py
```
Acesse `http://localhost:8501` e faça login.

## Sincronização

### Automática (já configurada)
Tarefa do Windows **`ZOPU_BitrixSync`** executa `run_sync.bat` **a cada 1 hora**.
A cada execução busca o que mudou (`DATE_MODIFY`) nas **últimas 1h30** (folga de
30 min para nunca perder atualização entre execuções). O upsert por
`(tenant, id)` torna re-buscas seguras. Log em `sync.log`.

Comandos úteis (PowerShell):
```powershell
schtasks /Run   /TN "ZOPU_BitrixSync"     # rodar agora
schtasks /Query /TN "ZOPU_BitrixSync" /V /FO LIST   # status
schtasks /Delete /TN "ZOPU_BitrixSync" /F  # remover
```

> Há também um **auto-sync de segurança** dentro do app: ao abrir, se a última
> sincronização foi há mais de 60 min, ele sincroniza o ambiente atual.

### Manual
```bash
python sync.py            # incremental, todos os ambientes ativos
python sync.py --full     # carga completa (backfill)
python sync.py --tenant 1 # só um ambiente
```

## Administrar (linha de comando)
```bash
python manage.py init --master-pass SENHA
python manage.py add-tenant --name "Empresa X" --webhook https://portal.bitrix24.com.br/rest/<id>/<token>/ --category 16
python manage.py add-user --username joao --password 123 --role client --tenant 2
python manage.py list
```
> Tudo isso também pode ser feito pela aba **Administração** do usuário master.

## KPIs disponíveis
- **Cartões:** valor ganho, pipeline aberto, win rate, ticket médio, leads,
  conversão de leads, negócios ganhos, ciclo de vendas (mediana).
- **Visão geral:** funil por estágio, valor por situação, forecast ponderado.
- **Pipeline:** valor por estágio, distribuição do ciclo, maiores negócios abertos,
  **motivo de fechamento/perda** e **segmento** dos negócios.
- **Leads:** convertidos, desqualificados, no-show, por **status de qualificação** e
  **fonte**, conversão por fonte, **segmento**, **cargo** e **motivos de desqualificação**.
- **Reuniões (SPA 1050):** total, agendadas, taxa de comparecimento e de no-show,
  por estágio, por mês, por responsável e por fonte.
- **Vendedores:** ranking por valor ganho e win rate.
- **Fontes:** negócios e valor ganho por origem.
- **Metas:** definição de meta por vendedor/mês + Meta × Realizado e atingimento.
- **Mês a mês:** variação MoM de receita, negócios, leads e conversão.
- **Dados:** tabelas completas + download CSV.

## Campos personalizados (mapa de campos por ambiente)
Dimensões como segmento, cargo, motivo de perda e o SPA de reuniões vivem em
**campos UF específicos de cada portal Bitrix**. Cada ambiente tem um `FIELD_MAP`
(JSON) que liga a dimensão lógica ao código real, resolvido para texto na
sincronização. Edite pela aba **Administração → Ambientes** (master). Mapa do
ambiente Coontrol:

```json
{
  "lead": {"segmento": "UF_CRM_1761827705633", "cargo": "POST", "motivo": "UF_CRM_1761828042253"},
  "deal": {"segmento": "UF_CRM_1753417862", "motivo": "UF_CRM_1769452594193"},
  "meetings_entity_type_id": 1050
}
```

> **Produto/Serviço** e **End User vs OEM/Integrador** não estão preenchidos/não
> existem como campo no Bitrix da Coontrol, então não há KPI para eles. Se um dia
> forem cadastrados, basta acrescentá-los ao `FIELD_MAP`.

## Escopo do webhook Bitrix
Precisa de **crm** (leads, deals, status). O escopo **user** é recomendado para
exibir o nome dos vendedores (sem ele aparece `ID {n}`).

## Observação sobre os dados atuais (Coontrol)
Quase todos os 498 negócios estão atribuídos a um único ID (carga "Legado"). Quando
os negócios passarem a ser distribuídos entre os vendedores reais, as abas
**Vendedores** e **Metas** ganham muito mais valor.
