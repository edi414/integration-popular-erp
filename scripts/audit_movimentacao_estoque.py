"""
Auditoria e sincronização histórica da tabela movimentacao_estoque.

Fontes:
  - Unico (PostgreSQL): dados pré 01/04/2026, tipodocumento IN (1, 2, 3)
  - G3 (MySQL):         dados a partir de 01/04/2026, tipodocumento IN (55, 65)

# ─── PREMISSAS DA TABELA ────────────────────────────────────────────────────
#
# P1. tipodocumento discrimina o sistema de origem e o tipo de documento:
#       1  = NFC-e PDV (Unico)   → tipo_movimentacao sempre 'S'
#       2  = NF-e entrada 55 (Unico) → tipo_movimentacao sempre 'E'
#       3  = CT-e/NF-e 57 (Unico)   → tipo_movimentacao sempre 'E'
#       55 = NF-e entrada (G3)      → tipo_movimentacao 'E' ou 'C'
#       65 = NFC-e PDV (G3)         → tipo_movimentacao 'S' ou 'C'
#       negativo (-2,-3,-4,-6,-15) = ajuste interno Unico, sem documento fiscal
#
# P2. tipo_movimentacao discrimina o efeito sobre o estoque:
#       'E' = entrada  → aumenta saldo
#       'S' = saída    → reduz saldo
#       'C' = cancelamento → NÃO entra no cálculo de saldo
#       NULL → aparece só junto a tipodocumento negativo (ajuste interno)
#
# P3. chave_nfe é obrigatória em entradas com NF-e eletrônica:
#       Esperado preenchido:  tipodocumento IN (2, 55) AND tipo_movimentacao = 'E'
#       Esperado NULL:        tipodocumento = 3 (CT-e — Unico não armazena chave)
#                             tipodocumento IN (1, 65) (NFC-e — venda, sem chave)
#                             tipo_movimentacao = 'C' (cancelamento)
#       Inesperado NULL:      tipodocumento IN (2, 55) AND tipo_movimentacao = 'E'
#                             → gaps reais que esta auditoria quantifica
#
# P4. id_documento não é globalmente único — é chave de negócio por período:
#       Unico (tipodocumento 1,2,3): operacao.id
#       G3 vendas (tipodocumento 65): ecf_venda_cabecalho.id
#       G3 entradas (tipodocumento 55): notas_entrada.id
#       Join correto: ON me.id_documento = v.id_documento
#                     AND me.tipodocumento IN (<conjunto por sistema>)
#
# P5. Vendas sem movimentação são normais para o Unico legado:
#       ~2.464 operações PDV registradas sem movimentoestoque correspondente no sistema
#       de origem — não é falha de ETL, é limitação do Unico.
#       No G3, vendas sem match são apenas cupons cancelados ou vendas do dia atual.
#
# ─── COMANDOS DISPONÍVEIS ────────────────────────────────────────────────────
#
#  python scripts/audit_movimentacao_estoque.py          # auditoria + sync de datas faltando
#  python scripts/audit_movimentacao_estoque.py --audit  # apenas relatório, sem modificar dados
#  python scripts/audit_movimentacao_estoque.py --fix-id-documento   # reprocessa todos Unico (id_documento)
#  python scripts/audit_movimentacao_estoque.py --fix-chave-nfe-g3   # reprocessa todos G3 (chave_nfe)
#  python scripts/audit_movimentacao_estoque.py --fix-chave-nfe-unico # reprocessa só datas Unico com chave_nfe NULL
"""

import sys
import argparse
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handlers.db_connection import DatabaseConnection
from handlers.query_loader import load_query_from_file
from handlers.log_handler import setup_logger
from settings.db_config import UNICO_POSTGRES, BANCO_MERCADO, G3_DATABASE
from services.movimentacao_estoque import MovimentacaoEstoqueETL
from utils.data_transformers import clean_dataframe_nans

CUTOFF_DATE = date(2026, 4, 1)

# Chave única do UPSERT — datahora e chave_nfe não fazem parte da chave de controle
UNIQUE_COLUMNS = [
    "tipodocumento", "id_documento", "currenttimemillis",
]

# tipodocumento de ENTRADA no Unico (NF-e 55 e CT-e/57). Essas linhas são
# reconciliadas por documento (tipodocumento + id_documento) em vez de upsert por
# linha, para eliminar órfãos de reedição. Vendas Unico (tipodoc 1) e entradas
# antigas sem id_documento (migradas antes do campo existir) seguem por upsert.
ENTRADA_TIPODOCS_UNICO = {2, 3}

logger = setup_logger("audit_movimentacao_estoque", log_file="logs/audit_movimentacao_estoque.log")


# ---------------------------------------------------------------------------
# Helpers de output
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    border = "=" * 60
    logger.info(border)
    logger.info(f"  {title}")
    logger.info(border)
    print(f"\n{border}\n  {title}\n{border}")


def _ok(msg: str) -> None:
    logger.info(f"[OK]   {msg}")
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    logger.warning(f"[WARN] {msg}")
    print(f"  [WARN] {msg}")


def _info(msg: str) -> None:
    logger.info(msg)
    print(f"  {msg}")


def _mercado_conn() -> DatabaseConnection:
    return DatabaseConnection(BANCO_MERCADO)


def _unico_pg_conn() -> DatabaseConnection:
    return DatabaseConnection(UNICO_POSTGRES)


def _g3_conn() -> DatabaseConnection:
    return DatabaseConnection(G3_DATABASE)


# ---------------------------------------------------------------------------
# Fase 1 — Volume geral e cobertura por sistema
# ---------------------------------------------------------------------------

def audit_volume(mercado: DatabaseConnection) -> None:
    _section("Fase 1 — Volume geral por sistema e tipo_movimentacao")

    # P1: operacionais = tipodocumento > 0
    q = """
        SELECT
            CASE
                WHEN tipodocumento IN (1,2,3)   THEN 'Unico'
                WHEN tipodocumento IN (55,65)    THEN 'G3'
                WHEN tipodocumento < 0           THEN 'Ajuste interno'
                ELSE 'Desconhecido'
            END AS sistema,
            tipodocumento,
            tipo_movimentacao,
            COUNT(*) AS total
        FROM movimentacao_estoque
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3;
    """
    df = mercado.get_data(q)
    _info(df.to_string(index=False))

    total = int(df["total"].sum())
    op = int(df.loc[df["tipodocumento"].isin([1, 2, 3, 55, 65]), "total"].sum())
    adj = int(df.loc[df["tipodocumento"] < 0, "total"].sum())
    _info(f"\nTotal geral: {total:,}  |  Operacionais: {op:,}  |  Ajustes internos: {adj:,}")

    # Premissa P1: ajustes internos têm tipo_movimentacao NULL
    df_adj = df[df["tipodocumento"] < 0]
    if df_adj.empty or df_adj["tipo_movimentacao"].isna().all() or (df_adj["tipo_movimentacao"] == "None").all():
        _ok("P1 confirmada: ajustes internos têm tipo_movimentacao NULL.")
    else:
        _warn("P1 VIOLADA: ajuste interno com tipo_movimentacao não-nulo encontrado!")
        _info(df_adj[df_adj["tipo_movimentacao"].notna()].to_string(index=False))


# ---------------------------------------------------------------------------
# Fase 2 — Saídas: vendas sem movimentação correspondente (P5)
# ---------------------------------------------------------------------------

def audit_saidas_sem_mov(mercado: DatabaseConnection) -> None:
    _section("Fase 2 — Saídas: vendas PDV sem movimentação correspondente (premissa P5)")

    # Unico: join por id_documento + tipodocumento IN (1,2,3)
    q_unico = """
        SELECT
            'Unico' AS sistema,
            DATE_TRUNC('year', v.emissao)::date AS ano,
            COUNT(*) AS vendas_sem_mov
        FROM uniplus_vendas_pdvs v
        LEFT JOIN movimentacao_estoque me
            ON ABS(v.id_documento) = me.id_documento
           AND me.tipo_movimentacao = 'S'
           AND me.tipodocumento IN (1, 2, 3)
        WHERE me.id_documento IS NULL
          AND v.canc = false
          AND v.emissao < '2026-04-01'
        GROUP BY 1, 2
        ORDER BY 2;
    """
    df_unico = mercado.get_data(q_unico)

    # G3: join por id_documento + tipodocumento IN (55, 65)
    q_g3 = """
        SELECT
            'G3' AS sistema,
            DATE_TRUNC('year', v.emissao)::date AS ano,
            COUNT(*) AS vendas_sem_mov
        FROM uniplus_vendas_pdvs v
        LEFT JOIN movimentacao_estoque me
            ON v.id_documento = me.id_documento
           AND me.tipo_movimentacao = 'S'
           AND me.tipodocumento IN (55, 65)
        WHERE me.id_documento IS NULL
          AND v.canc = false
          AND v.emissao >= '2026-04-01'
        GROUP BY 1, 2
        ORDER BY 2;
    """
    df_g3 = mercado.get_data(q_g3)

    df = pd.concat([df_unico, df_g3], ignore_index=True)

    if df.empty or df["vendas_sem_mov"].sum() == 0:
        _ok("Todas as vendas têm movimentação de saída correspondente.")
        return

    total_unico = int(df_unico["vendas_sem_mov"].sum()) if not df_unico.empty else 0
    total_g3 = int(df_g3["vendas_sem_mov"].sum()) if not df_g3.empty else 0
    _info(df.to_string(index=False))
    _info(f"\nTotal Unico sem mov: {total_unico:,}  |  Total G3 sem mov: {total_g3:,}")

    if total_unico <= 2464:
        _ok(f"P5 confirmada: {total_unico:,} vendas Unico sem mov — dentro do limite esperado (~2.464).")
    else:
        _warn(f"P5 VIOLADA: {total_unico:,} vendas Unico sem mov excedem o esperado (~2.464).")

    if total_g3 > 0:
        # G3 gaps precisam ser explicados
        q_g3_detail = """
            SELECT v.canc, me.tipo_movimentacao,
                   COUNT(*) AS cnt
            FROM uniplus_vendas_pdvs v
            LEFT JOIN movimentacao_estoque me
                ON v.id_documento = me.id_documento
               AND me.tipodocumento IN (55, 65)
            WHERE v.emissao >= '2026-04-01'
            GROUP BY 1, 2
            ORDER BY 3 DESC;
        """
        df_detail = mercado.get_data(q_g3_detail)
        _info("\nDetalhamento G3 (vendas sem saída):")
        _info(df_detail.to_string(index=False))


# ---------------------------------------------------------------------------
# Fase 3 — Entradas: cobertura de chave_nfe (P3)
# ---------------------------------------------------------------------------

def audit_chave_nfe(mercado: DatabaseConnection) -> None:
    _section("Fase 3 — Entradas: cobertura de chave_nfe (premissa P3)")

    q = """
        SELECT
            CASE
                WHEN tipodocumento IN (2,3) THEN 'Unico'
                WHEN tipodocumento = 55     THEN 'G3'
            END AS sistema,
            tipodocumento,
            tipo_movimentacao,
            COUNT(*) AS total,
            COUNT(chave_nfe) AS com_chave,
            COUNT(*) - COUNT(chave_nfe) AS sem_chave,
            ROUND(100.0 * COUNT(chave_nfe) / COUNT(*), 1) AS pct_cobertura
        FROM movimentacao_estoque
        WHERE tipodocumento IN (2, 3, 55)
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3;
    """
    df = mercado.get_data(q)
    _info(df.to_string(index=False))

    # P3: tipodocumento=3 (CT-e) deve ter chave_nfe NULL — isso é normal
    df_cte = df[(df["tipodocumento"] == 3) & (df["tipo_movimentacao"] == "E")]
    if not df_cte.empty:
        cte_sem = int(df_cte["sem_chave"].sum())
        cte_total = int(df_cte["total"].sum())
        _ok(f"P3 CT-e: {cte_sem:,}/{cte_total:,} registros tipodocumento=3 sem chave_nfe — estruturalmente correto (Unico não armazena chave CT-e).")

    # Gaps reais: tipodocumento=2 ou 55 com tipo_movimentacao='E' sem chave
    df_gaps = df[
        (df["tipodocumento"].isin([2, 55])) &
        (df["tipo_movimentacao"] == "E") &
        (df["sem_chave"] > 0)
    ]
    if df_gaps.empty:
        _ok("P3 confirmada: todas as entradas NF-e (tipodoc 2 e 55) têm chave_nfe preenchida.")
    else:
        for _, row in df_gaps.iterrows():
            _warn(f"P3 GAP: tipodocumento={row['tipodocumento']} sistema={row['sistema']} — {int(row['sem_chave']):,} entradas sem chave_nfe ({row['pct_cobertura']}% cobertura)")

    # Investiga os gaps detalhadamente
    q_gap_detail = """
        SELECT
            tipodocumento,
            tipo_movimentacao,
            id_documento IS NOT NULL AS tem_id_documento,
            COUNT(*) AS cnt
        FROM movimentacao_estoque
        WHERE tipodocumento IN (2, 55)
          AND tipo_movimentacao = 'E'
          AND chave_nfe IS NULL
        GROUP BY 1, 2, 3
        ORDER BY 1, 3;
    """
    df_detail = mercado.get_data(q_gap_detail)
    if not df_detail.empty:
        _info("\nDetalhamento dos gaps (tipodoc 2 e 55 sem chave_nfe):")
        _info(df_detail.to_string(index=False))

    # Entradas canceladas (tipo_movimentacao='C') sem chave_nfe — P3 aceita NULL aqui
    q_cancel = """
        SELECT tipodocumento, tipo_movimentacao, COUNT(*) AS cnt,
               COUNT(chave_nfe) AS com_chave
        FROM movimentacao_estoque
        WHERE tipodocumento IN (2, 3, 55)
          AND tipo_movimentacao = 'C'
        GROUP BY 1, 2;
    """
    df_cancel = mercado.get_data(q_cancel)
    if not df_cancel.empty:
        _info("\nEntradas canceladas (tipo_movimentacao='C') — chave_nfe esperada mas não crítica:")
        _info(df_cancel.to_string(index=False))


# ---------------------------------------------------------------------------
# Fase 4 — id_documento: integridade e cobertura (P4)
# ---------------------------------------------------------------------------

def audit_id_documento(mercado: DatabaseConnection) -> None:
    _section("Fase 4 — id_documento: integridade por sistema (premissa P4)")

    q = """
        SELECT
            CASE
                WHEN tipodocumento IN (1,2,3) THEN 'Unico'
                WHEN tipodocumento IN (55,65) THEN 'G3'
            END AS sistema,
            tipodocumento,
            tipo_movimentacao,
            COUNT(*) AS total,
            COUNT(id_documento) AS com_id,
            COUNT(*) - COUNT(id_documento) AS sem_id,
            ROUND(100.0 * COUNT(id_documento) / COUNT(*), 1) AS pct_cobertura
        FROM movimentacao_estoque
        WHERE tipodocumento IN (1, 2, 3, 55, 65)
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3;
    """
    df = mercado.get_data(q)
    _info(df.to_string(index=False))

    # Registros históricos Unico sem id_documento — esperado (~1.7M pré-backfill)
    df_unico_sem_id = df[
        (df["tipodocumento"].isin([1, 2, 3])) & (df["sem_id"] > 0)
    ]
    if not df_unico_sem_id.empty:
        total_sem = int(df_unico_sem_id["sem_id"].sum())
        _warn(f"P4 INFO: {total_sem:,} registros Unico sem id_documento (migrados antes do campo existir — esperado para datas < 2021-06-01).")
    else:
        _ok("P4: todos os registros Unico operacionais têm id_documento.")

    df_g3_sem_id = df[(df["tipodocumento"].isin([55, 65])) & (df["sem_id"] > 0)]
    if not df_g3_sem_id.empty:
        _warn(f"P4 INESPERADO: {int(df_g3_sem_id['sem_id'].sum()):,} registros G3 sem id_documento!")
    else:
        _ok("P4: todos os registros G3 têm id_documento.")


# ---------------------------------------------------------------------------
# Fase 5 — Duplicatas (P4 / chave única do UPSERT)
# ---------------------------------------------------------------------------

def audit_duplicates(mercado: DatabaseConnection, sync: bool) -> int:
    _section("Fase 5 — Duplicatas na chave única do UPSERT")

    q = """
        SELECT tipodocumento, id_documento, currenttimemillis, COUNT(*) AS cnt
        FROM movimentacao_estoque
        GROUP BY tipodocumento, id_documento, currenttimemillis
        HAVING COUNT(*) > 1
        ORDER BY tipodocumento, id_documento;
    """
    df = mercado.get_data(q)

    if df.empty:
        _ok("Nenhuma duplicata encontrada — chave única íntegra.")
        return 0

    total_dup_groups = len(df)
    total_dup_rows = int(df["cnt"].sum()) - total_dup_groups
    _warn(f"{total_dup_groups} grupos duplicados ({total_dup_rows} linhas extras).")
    _info(df.head(20).to_string(index=False))

    if sync:
        _info("Removendo duplicatas (DELETE cirúrgico — só grupos com cnt > 1)...")
        # ROW_NUMBER() sobre a tabela inteira — única forma segura com NULLs na chave.
        # (IN com tuplas contendo NULL retorna NULL, não TRUE — zero rows seriam deletadas.)
        dedup_q = """
            DELETE FROM movimentacao_estoque
            WHERE ctid IN (
                SELECT ctid FROM (
                    SELECT ctid,
                           ROW_NUMBER() OVER (
                               PARTITION BY tipodocumento, id_documento, currenttimemillis
                               ORDER BY ctid
                           ) AS rn
                    FROM movimentacao_estoque
                ) sub
                WHERE rn > 1
            );
        """
        mercado.connect()
        try:
            cursor = mercado.connection.cursor()
            cursor.execute(dedup_q)
            deleted = cursor.rowcount
            mercado.connection.commit()
            cursor.close()
            _ok(f"{deleted} linhas duplicadas removidas.")
        except Exception as e:
            mercado.connection.rollback()
            logger.error(f"Erro ao remover duplicatas: {e}")
            raise
        finally:
            mercado.disconnect()

    return total_dup_groups


# ---------------------------------------------------------------------------
# Fase 6 — Constraint UNIQUE no destino
# ---------------------------------------------------------------------------

def audit_unique_constraint(mercado: DatabaseConnection) -> bool:
    _section("Fase 6 — Constraint UNIQUE no banco destino")

    q = """
        SELECT tc.constraint_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_schema = kcu.table_schema
        WHERE tc.table_name = 'movimentacao_estoque'
          AND tc.table_schema = 'public'
          AND tc.constraint_type IN ('UNIQUE', 'PRIMARY KEY')
        ORDER BY tc.constraint_name, kcu.ordinal_position;
    """
    df = mercado.get_data(q)

    if df.empty:
        _warn("Nenhuma constraint UNIQUE encontrada!")
        _warn("Sugestão: CREATE UNIQUE INDEX movimentacao_estoque_uk")
        _warn("  ON public.movimentacao_estoque (tipodocumento, id_documento, currenttimemillis)")
        _warn("  NULLS NOT DISTINCT;")
        return False

    _info(df.to_string(index=False))
    constraint_cols = set(df["column_name"].tolist())
    expected = set(UNIQUE_COLUMNS)
    if expected.issubset(constraint_cols):
        _ok("Constraint cobre todas as colunas esperadas.")
        return True

    _warn(f"Colunas faltando na constraint: {expected - constraint_cols}")
    return False


# ---------------------------------------------------------------------------
# Fase 7 — Datas faltando no destino vs. fontes de origem
# ---------------------------------------------------------------------------

def get_missing_dates_unico(unico_pg: DatabaseConnection, mercado: DatabaseConnection) -> list:
    _section("Fase 7a — Datas faltando (fonte: Unico pré 01/04/2026)")

    q_unico = """
        SELECT DISTINCT DATE(m.datahora) AS data_disponivel
        FROM public.movimentoestoque m
        WHERE DATE(m.datahora) < '2026-04-01'
          AND m.cancelado = 0
        ORDER BY 1;
    """
    df_unico = unico_pg.get_data(q_unico)

    q_destino = """
        SELECT DISTINCT DATE(datahora) AS data_carregada
        FROM movimentacao_estoque
        WHERE datahora < '2026-04-01';
    """
    df_destino = mercado.get_data(q_destino)

    datas_unico = set(df_unico["data_disponivel"].astype(str).tolist()) if not df_unico.empty else set()
    datas_destino = set(df_destino["data_carregada"].astype(str).tolist()) if not df_destino.empty else set()
    faltando = sorted(datas_unico - datas_destino)

    _info(f"Datas disponíveis no Unico:   {len(datas_unico)}")
    _info(f"Datas no destino (pré corte): {len(datas_destino)}")

    if faltando:
        _warn(f"{len(faltando)} datas faltando no destino (Unico):")
        for d in faltando:
            _info(f"  - {d}")
    else:
        _ok("Todas as datas do Unico já estão no destino.")

    return faltando


def get_missing_dates_g3(g3: DatabaseConnection, mercado: DatabaseConnection) -> list:
    _section("Fase 7b — Datas faltando (fonte: G3 a partir de 01/04/2026)")

    q_vendas = """
        SELECT DISTINCT data_venda AS data_disponivel
        FROM ecf_venda_cabecalho
        WHERE data_venda >= '2026-04-01'
        ORDER BY 1;
    """
    q_notas = """
        SELECT DISTINCT COALESCE(data_chegada, data_emissao) AS data_disponivel
        FROM notas_entrada
        WHERE COALESCE(data_chegada, data_emissao) >= '2026-04-01'
        ORDER BY 1;
    """
    df_v = g3.get_data(q_vendas)
    df_n = g3.get_data(q_notas)

    datas_g3 = set()
    for df in [df_v, df_n]:
        if not df.empty:
            datas_g3.update(df["data_disponivel"].astype(str).tolist())

    q_destino = """
        SELECT DISTINCT DATE(datahora) AS data_carregada
        FROM movimentacao_estoque
        WHERE datahora >= '2026-04-01';
    """
    df_destino = mercado.get_data(q_destino)
    datas_destino = set(df_destino["data_carregada"].astype(str).tolist()) if not df_destino.empty else set()
    faltando = sorted(datas_g3 - datas_destino)

    _info(f"Datas disponíveis no G3:      {len(datas_g3)}")
    _info(f"Datas no destino (pós corte): {len(datas_destino)}")

    if faltando:
        _warn(f"{len(faltando)} datas faltando no destino (G3):")
        for d in faltando:
            _info(f"  - {d}")
    else:
        _ok("Todas as datas do G3 já estão no destino.")

    return faltando


# ---------------------------------------------------------------------------
# Sync — Unico histórico
# ---------------------------------------------------------------------------

def _transform_unico(df: pd.DataFrame) -> pd.DataFrame:
    if "filial" in df.columns:
        df["filial"] = pd.to_numeric(df["filial"], errors="coerce").round().astype("Int64")
    if "tipodocumento" in df.columns:
        df["tipodocumento"] = pd.to_numeric(df["tipodocumento"], errors="coerce").round().astype("Int64")

    numeric_cols = [
        "qtd", "valortotal", "precoultimacompra", "custoaquisicao", "customedio",
        "icms", "icms_st", "ippt", "pis_cofins", "ipi", "outros_impostos", "comissao",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "datahora" in df.columns:
        df["datahora"] = pd.to_datetime(df["datahora"], errors="coerce")

    text_cols = ["local_estoque", "documento", "codigo", "un", "tipo_movimentacao", "nome", "cfop", "chave_nfe"]
    for col in text_cols:
        if col in df.columns:
            df[col] = df[col].astype(str)

    if "currenttimemillis" in df.columns:
        df["currenttimemillis"] = pd.to_numeric(df["currenttimemillis"], errors="coerce")
    if "id_documento" in df.columns:
        df["id_documento"] = pd.to_numeric(df["id_documento"], errors="coerce").round().astype("Int64")

    columns = [
        "local_estoque", "filial", "documento", "codigo", "nome", "datahora",
        "currenttimemillis", "tipodocumento", "qtd", "tipo_movimentacao",
        "valortotal", "precoultimacompra", "custoaquisicao", "customedio",
        "icms", "icms_st", "ippt", "pis_cofins", "ipi", "outros_impostos",
        "comissao", "cfop", "un", "id_documento", "chave_nfe",
    ]
    existing = [c for c in columns if c in df.columns]
    return clean_dataframe_nans(df[existing])


def sync_unico(dates: list, unico_pg: DatabaseConnection, mercado: DatabaseConnection,
               dry_run: bool = False) -> dict:
    modo = " [DRY-RUN — nada será persistido]" if dry_run else ""
    _section(f"Sync — Unico (dados históricos pré 01/04/2026){modo}")
    query = load_query_from_file("movimentacao_estoque_unico.sql")
    processed, failed = [], []
    tot_del = tot_ins = 0

    for d in dates:
        try:
            _info(f"Processando {d} (Unico)...")
            df_raw = unico_pg.get_data(query, {"data": d})
            if df_raw.empty:
                _warn(f"  Nenhum dado no Unico para {d}.")
                continue
            df = _transform_unico(df_raw)

            # Entradas Unico com id_documento -> reconciliação por documento;
            # restante (vendas tipodoc 1 e entradas antigas sem id_documento) -> upsert.
            is_entrada = df["tipodocumento"].isin(ENTRADA_TIPODOCS_UNICO) & df["id_documento"].notna()
            df_entradas = df[is_entrada]
            df_resto = df[~is_entrada]

            if not df_entradas.empty:
                res = mercado.reconcile_by_document(
                    table_name="movimentacao_estoque",
                    data=df_entradas,
                    doc_columns=["tipodocumento", "id_documento"],
                    schema="public",
                    dry_run=dry_run,
                )
                tot_del += res["linhas_deletadas"]
                tot_ins += res["linhas_inseridas"]

            # No dry-run NÃO mexemos em vendas/resto (upsert persiste) — apenas validamos entradas.
            if not df_resto.empty and not dry_run:
                mercado.upsert(
                    table_name="movimentacao_estoque",
                    data=df_resto,
                    unique_columns=UNIQUE_COLUMNS,
                    schema="public",
                )

            if dry_run:
                _ok(f"  [DRY-RUN] {len(df_entradas)} entradas (deletaria {res['linhas_deletadas'] if not df_entradas.empty else 0}, "
                    f"reinseriria {res['linhas_inseridas'] if not df_entradas.empty else 0}); "
                    f"{len(df_resto)} de venda/resto IGNORADOS (não tocados).")
            else:
                _ok(f"  {len(df_entradas)} entradas reconciliadas + {len(df_resto)} via upsert para {d}.")
            processed.append(d)
        except Exception as e:
            logger.error(f"Erro ao processar {d} (Unico): {e}")
            _warn(f"  FALHOU: {e}")
            failed.append({"date": d, "error": str(e)})

    if dry_run:
        _info(f"\n[DRY-RUN] TOTAL: deletaria {tot_del}, reinseriria {tot_ins} linhas de ENTRADA. "
              f"Vendas intocadas. Nenhum TRUNCATE. Nada persistido (ROLLBACK).")
    return {"processed": processed, "failed": failed}


# ---------------------------------------------------------------------------
# Sync — G3
# ---------------------------------------------------------------------------

def sync_g3(dates: list, dry_run: bool = False) -> dict:
    modo = " [DRY-RUN — nada será persistido]" if dry_run else ""
    _section(f"Sync — G3 (dados a partir de 01/04/2026){modo}")
    etl = MovimentacaoEstoqueETL(source_config=G3_DATABASE, target_config=BANCO_MERCADO)
    processed, failed = [], []

    for d in dates:
        try:
            _info(f"Processando {d} (G3){' [DRY-RUN]' if dry_run else ''}...")
            etl._process_single_date(d, dry_run=dry_run)
            _ok(f"  Concluído para {d}.")
            processed.append(d)
        except Exception as e:
            logger.error(f"Erro ao processar {d} (G3): {e}")
            _warn(f"  FALHOU: {e}")
            failed.append({"date": d, "error": str(e)})

    return {"processed": processed, "failed": failed}


def fix_entradas_g3(g3: DatabaseConnection, dry_run: bool = False) -> dict:
    """Reprocessa TODAS as datas de entrada do G3 reconciliando entradas por documento
    (notas_entrada.id). Limpa órfãos de reedição (nep.id regenerado). Vendas seguem upsert.
    Com dry_run=True nada é persistido (reconcile em transação com ROLLBACK; vendas ignoradas)."""
    _section("Fix — Reconciliar entradas G3 por documento (todas as datas de entrada)")

    q = """
        SELECT DISTINCT COALESCE(data_chegada, data_emissao) AS data
        FROM notas_entrada
        WHERE COALESCE(data_chegada, data_emissao) >= '2026-04-01'
        ORDER BY 1;
    """
    df = g3.get_data(q)
    if df.empty:
        _warn("Nenhuma data de entrada G3 encontrada.")
        return {"processed": [], "failed": []}

    dates = [str(x) for x in df["data"].tolist()]
    _info(f"{len(dates)} datas de entrada G3 a reprocessar{' (DRY-RUN)' if dry_run else ''}.")
    return sync_g3(dates, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Fix id_documento — reprocessa todas as datas Unico
# ---------------------------------------------------------------------------

def fix_orfaos_unico(unico_pg: DatabaseConnection, mercado: DatabaseConnection,
                     dry_run: bool = False) -> dict:
    """Remove do destino as linhas de ENTRADA Unico (tipodoc 2,3) cujo par
    (tipodocumento, currenttimemillis) não existe mais na origem (movimentoestoque,
    cancelado=0). São órfãos legados (em geral pré-2021, sem id_documento) que o
    reconcile-by-document não alcança. NÃO usa TRUNCATE; DELETE escopado aos pares órfãos.
    Trava de segurança: aborta se a origem retornar um conjunto suspeitosamente pequeno."""
    from psycopg2.extras import execute_values
    _section(f"Fix — Remover órfãos legados Unico (entradas){' [DRY-RUN]' if dry_run else ''}")

    # 1) Pares (tipodoc, ctm) vigentes na ORIGEM
    df_src = unico_pg.get_data("""
        SELECT DISTINCT tipodocumento, currenttimemillis
        FROM public.movimentoestoque
        WHERE tipodocumento IN (2,3) AND cancelado = 0 AND currenttimemillis IS NOT NULL
    """)
    src_pairs = {(int(t), int(c)) for t, c in df_src.itertuples(index=False)}
    _info(f"Pares (tipodoc,ctm) vigentes na origem: {len(src_pairs)}")

    # TRAVA DE SEGURANÇA: origem precisa ser substancial (esperado ~135k)
    if len(src_pairs) < 50000:
        _warn(f"ABORTADO: origem retornou apenas {len(src_pairs)} pares — suspeito de falha. "
              f"Nenhum DELETE executado.")
        return {"orfaos": 0, "deletadas": 0, "abortado": True}

    # 2) Pares no DESTINO (entradas) e identificação dos órfãos
    df_dst = mercado.get_data("""
        SELECT tipodocumento, currenttimemillis, COUNT(*) AS n
        FROM movimentacao_estoque
        WHERE tipodocumento IN (2,3) AND currenttimemillis IS NOT NULL
        GROUP BY tipodocumento, currenttimemillis
    """)
    orfaos = [(int(t), int(c)) for t, c, n in df_dst.itertuples(index=False) if (int(t), int(c)) not in src_pairs]
    linhas_orfas = int(sum(n for t, c, n in df_dst.itertuples(index=False) if (int(t), int(c)) not in src_pairs))
    _info(f"Pares órfãos no destino (ctm sumiu da origem): {len(orfaos)} ({linhas_orfas} linhas)")

    if not orfaos:
        _ok("Nenhum órfão a remover.")
        return {"orfaos": 0, "deletadas": 0, "abortado": False}

    del_q = ("DELETE FROM public.movimentacao_estoque t USING (VALUES %s) AS v(tipodocumento, currenttimemillis) "
             "WHERE t.tipodocumento = v.tipodocumento AND t.currenttimemillis = v.currenttimemillis")
    deleted = 0
    mercado.connect()
    try:
        with mercado.connection.cursor() as cur:
            for i in range(0, len(orfaos), 500):
                execute_values(cur, del_q, orfaos[i:i+500], page_size=500)
                deleted += cur.rowcount
        if dry_run:
            mercado.connection.rollback()
            _info(f"[DRY-RUN] {deleted} linhas órfãs SERIAM removidas — ROLLBACK (nada persistido).")
        else:
            mercado.connection.commit()
            _ok(f"{deleted} linhas órfãs removidas.")
    except Exception as e:
        mercado.connection.rollback()
        logger.error(f"Erro no delete de órfãos Unico: {e}")
        raise
    finally:
        mercado.disconnect()

    return {"orfaos": len(orfaos), "deletadas": deleted, "abortado": False}


def fix_id_documento_unico(unico_pg: DatabaseConnection, mercado: DatabaseConnection,
                           dry_run: bool = False) -> dict:
    """Reprocessa todas as datas Unico para reconciliar as ENTRADAS por documento
    (m.idoriginal = operacao.id). Com dry_run=True nada é persistido."""
    _section("Fix — Reconciliar entradas Unico por documento (todas as datas)")

    q = """
        SELECT DISTINCT DATE(datahora) AS data
        FROM movimentacao_estoque
        WHERE tipodocumento IN (1, 2, 3)
        ORDER BY 1;
    """
    df = mercado.get_data(q)
    if df.empty:
        _warn("Nenhum dado Unico encontrado no destino.")
        return {"processed": [], "failed": []}

    dates = df["data"].astype(str).tolist()
    _info(f"{len(dates)} datas Unico a reprocessar{' (DRY-RUN)' if dry_run else ''}.")
    return sync_unico(dates, unico_pg, mercado, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Fix chave_nfe Unico — reprocessa só datas com entradas sem chave_nfe
# ---------------------------------------------------------------------------

def fix_chave_nfe_unico(unico_pg: DatabaseConnection, mercado: DatabaseConnection) -> dict:
    """Reprocessa apenas datas Unico com entradas (tipodocumento IN (2,3)) sem chave_nfe."""
    _section("Fix — Popular chave_nfe nas datas Unico com falhas")

    q = """
        SELECT DISTINCT DATE(datahora) AS data
        FROM movimentacao_estoque
        WHERE tipodocumento IN (2, 3)
          AND tipo_movimentacao = 'E'
          AND chave_nfe IS NULL
        ORDER BY 1;
    """
    df = mercado.get_data(q)
    if df.empty:
        _ok("Nenhuma data com chave_nfe faltando. Tudo OK.")
        return {"processed": [], "failed": []}

    dates = df["data"].astype(str).tolist()
    _info(f"{len(dates)} datas com entradas sem chave_nfe a reprocessar.")
    return sync_unico(dates, unico_pg, mercado)


# ---------------------------------------------------------------------------
# Fix chave_nfe G3 — reprocessa todas as datas G3
# ---------------------------------------------------------------------------

def fix_chave_nfe_g3(mercado: DatabaseConnection) -> dict:
    """Reprocessa apenas datas G3 com NF-e de entrada (tipodocumento=55) sem chave_nfe."""
    _section("Fix — Popular chave_nfe nas entradas G3 com falhas (tipodocumento=55)")

    q = """
        SELECT DISTINCT DATE(datahora) AS data
        FROM movimentacao_estoque
        WHERE tipodocumento = 55
          AND tipo_movimentacao = 'E'
          AND chave_nfe IS NULL
        ORDER BY 1;
    """
    df = mercado.get_data(q)
    if df.empty:
        _ok("Nenhuma data G3 com chave_nfe faltando. Tudo OK.")
        return {"processed": [], "failed": []}

    dates = df["data"].astype(str).tolist()
    _info(f"{len(dates)} datas G3 com entradas sem chave_nfe a reprocessar.")
    return sync_g3(dates)


# ---------------------------------------------------------------------------
# Resumo
# ---------------------------------------------------------------------------

def _print_summary(results: dict) -> None:
    _section("Resumo da Execução")
    for key, val in results.items():
        _info(f"{key}: {val}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auditoria e sync histórico da tabela movimentacao_estoque."
    )
    parser.add_argument("--audit", action="store_true",
                        help="Apenas audita e exibe relatório sem modificar dados.")
    parser.add_argument("--fix-id-documento", action="store_true", dest="fix_id_documento",
                        help="Reprocessa todas as datas Unico para reconciliar entradas por documento.")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Simula a reconciliação (DELETE+INSERT em transação com ROLLBACK); não persiste nada.")
    parser.add_argument("--fix-chave-nfe-g3", action="store_true", dest="fix_chave_nfe_g3",
                        help="Reprocessa todas as datas G3 para popular chave_nfe.")
    parser.add_argument("--fix-entradas-g3", action="store_true", dest="fix_entradas_g3",
                        help="Reconcilia entradas G3 por documento em todas as datas de entrada (limpa órfãos).")
    parser.add_argument("--fix-chave-nfe-unico", action="store_true", dest="fix_chave_nfe_unico",
                        help="Reprocessa só as datas Unico com entradas sem chave_nfe.")
    parser.add_argument("--fix-orfaos-unico", action="store_true", dest="fix_orfaos_unico",
                        help="Remove órfãos legados Unico (entradas cujo ctm sumiu da origem).")
    args = parser.parse_args()

    mercado = _mercado_conn()
    unico_pg = _unico_pg_conn()
    g3 = _g3_conn()

    if args.fix_id_documento:
        modo = "DRY-RUN (nada persistido)" if args.dry_run else "APLICANDO"
        _info(f"Modo: FIX reconciliar entradas Unico por documento — {modo}")
        r = fix_id_documento_unico(unico_pg, mercado, dry_run=args.dry_run)
        _print_summary({"datas_reprocessadas": len(r["processed"]), "datas_falhou": len(r["failed"]), **({} if not r["failed"] else {"falhas": r["failed"]})})
        return

    if args.fix_chave_nfe_unico:
        _info("Modo: FIX chave_nfe Unico (só datas com entradas sem chave_nfe)")
        r = fix_chave_nfe_unico(unico_pg, mercado)
        _print_summary({"datas_reprocessadas": len(r["processed"]), "datas_falhou": len(r["failed"]), **({} if not r["failed"] else {"falhas": r["failed"]})})
        return

    if args.fix_orfaos_unico:
        modo = "DRY-RUN (nada persistido)" if args.dry_run else "APLICANDO"
        _info(f"Modo: FIX remover órfãos legados Unico — {modo}")
        r = fix_orfaos_unico(unico_pg, mercado, dry_run=args.dry_run)
        _print_summary(r)
        return

    if args.fix_chave_nfe_g3:
        _info("Modo: FIX chave_nfe G3 (reprocessa todas as datas G3)")
        r = fix_chave_nfe_g3(mercado)
        _print_summary({"datas_reprocessadas": len(r["processed"]), "datas_falhou": len(r["failed"]), **({} if not r["failed"] else {"falhas": r["failed"]})})
        return

    if args.fix_entradas_g3:
        modo = "DRY-RUN (nada persistido)" if args.dry_run else "APLICANDO"
        _info(f"Modo: FIX reconciliar entradas G3 por documento — {modo}")
        r = fix_entradas_g3(g3, dry_run=args.dry_run)
        _print_summary({"datas_reprocessadas": len(r["processed"]), "datas_falhou": len(r["failed"]), **({} if not r["failed"] else {"falhas": r["failed"]})})
        return

    sync_mode = not args.audit
    _info("Modo: AUDITORIA + SYNC" if sync_mode else "Modo: APENAS AUDITORIA")

    results = {}

    # Fase 1-4: auditorias baseadas em premissas explícitas
    audit_volume(mercado)
    audit_saidas_sem_mov(mercado)
    audit_chave_nfe(mercado)
    audit_id_documento(mercado)

    # Fase 5-6: integridade estrutural
    dup_count = audit_duplicates(mercado, sync=sync_mode)
    results["duplicatas_encontradas"] = dup_count
    has_constraint = audit_unique_constraint(mercado)
    results["unique_constraint_ok"] = has_constraint

    # Fase 7: datas faltando + sync se necessário
    missing_unico = get_missing_dates_unico(unico_pg, mercado)
    missing_g3 = get_missing_dates_g3(g3, mercado)
    results["datas_faltando_unico"] = len(missing_unico)
    results["datas_faltando_g3"] = len(missing_g3)

    if sync_mode:
        if missing_unico:
            r = sync_unico(missing_unico, unico_pg, mercado)
            results["sync_unico_processed"] = len(r["processed"])
            results["sync_unico_failed"] = len(r["failed"])
        if missing_g3:
            r = sync_g3(missing_g3)
            results["sync_g3_processed"] = len(r["processed"])
            results["sync_g3_failed"] = len(r["failed"])

    _print_summary(results)


if __name__ == "__main__":
    main()
