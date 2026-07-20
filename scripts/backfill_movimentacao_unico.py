"""
Backfill/reprocessamento de movimentações de estoque do Unico (pré-01/04/2026).

Lê o Unico com a query corrigida (agg_item em vez de ult_item) e faz UPSERT
no destino. O UPSERT sobrescreve icms, pis_cofins e demais campos fiscais nas
linhas já existentes sem precisar deletar nada — idempotente e seguro.

Chave única de upsert: (tipodocumento, id_documento, currenttimemillis)

Uso:
  # Reprocessa o Q1 completo (padrão)
  python scripts/backfill_movimentacao_unico.py

  # Cirurgia em período específico
  python scripts/backfill_movimentacao_unico.py --start-date 2026-03-01 --end-date 2026-03-31

  # Simulação — mostra quais datas seriam processadas sem gravar nada
  python scripts/backfill_movimentacao_unico.py --dry-run
"""

import sys
import argparse
from pathlib import Path
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handlers.db_connection import DatabaseConnection
from handlers.query_loader import load_query_from_file
from handlers.log_handler import setup_logger
from settings.db_config import UNICO_POSTGRES, BANCO_MERCADO
from utils.data_transformers import clean_dataframe_nans

# Período coberto pelo Unico (Q1 2026)
DEFAULT_START = date(2026, 1, 1)
DEFAULT_END   = date(2026, 3, 31)

TABLE          = "movimentacao_estoque"
SCHEMA         = "public"
UNIQUE_COLUMNS = ["tipodocumento", "id_documento", "currenttimemillis"]
QUERY_FILE     = "movimentacao_estoque_unico.sql"

logger = setup_logger(
    "backfill_movimentacao_unico",
    log_file="logs/backfill_movimentacao_unico.log",
)


def _info(msg: str) -> None:
    print(f"[INFO]  {msg}")
    logger.info(msg)


def _warn(msg: str) -> None:
    print(f"[WARN]  {msg}")
    logger.warning(msg)


def _error(msg: str) -> None:
    print(f"[ERROR] {msg}")
    logger.error(msg)


def get_dates_with_data(unico: DatabaseConnection, start: date, end: date) -> list[str]:
    """Retorna datas do Unico que têm movimentações de saída (tipodocumento=1) no range."""
    df = unico.get_data(
        """
        SELECT DISTINCT DATE(datahora) AS dt
        FROM public.movimentoestoque
        WHERE DATE(datahora) BETWEEN %(start)s AND %(end)s
          AND tipodocumento = 1
          AND cancelado = 0
        ORDER BY 1
        """,
        {"start": start, "end": end},
    )
    if df.empty:
        return []
    return df["dt"].astype(str).tolist()


def transform(df: pd.DataFrame) -> pd.DataFrame:
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

    text_cols = ["local_estoque", "documento", "codigo", "un", "tipo_movimentacao",
                 "nome", "cfop", "chave_nfe"]
    for col in text_cols:
        if col in df.columns:
            df[col] = df[col].astype(str)

    if "currenttimemillis" in df.columns:
        df["currenttimemillis"] = pd.to_numeric(df["currenttimemillis"], errors="coerce")

    if "id_documento" in df.columns:
        df["id_documento"] = pd.to_numeric(df["id_documento"], errors="coerce").round().astype("Int64")

    columns = [
        "local_estoque", "filial", "documento", "codigo", "nome", "datahora",
        "currenttimemillis", "tipodocumento", "qtd", "tipo_movimentacao", "valortotal",
        "precoultimacompra", "custoaquisicao", "customedio", "icms", "icms_st", "ippt",
        "pis_cofins", "ipi", "outros_impostos", "comissao", "cfop", "un",
        "id_documento", "chave_nfe",
    ]
    existing = [c for c in columns if c in df.columns]
    return clean_dataframe_nans(df[existing])


def process_date(
    date_str: str,
    unico: DatabaseConnection,
    mercado: DatabaseConnection,
    query: str,
    dry_run: bool,
) -> int:
    raw = unico.get_data(query, {"data": date_str})
    if raw.empty:
        return 0

    transformed = transform(raw)

    if dry_run:
        _info(f"  [DRY-RUN] {date_str}: {len(transformed)} linhas (não gravado)")
        return len(transformed)

    mercado.upsert(
        table_name=TABLE,
        data=transformed,
        unique_columns=UNIQUE_COLUMNS,
        schema=SCHEMA,
    )
    return len(transformed)


def run(start: date, end: date, dry_run: bool = False) -> None:
    unico   = DatabaseConnection(UNICO_POSTGRES)
    mercado = DatabaseConnection(BANCO_MERCADO)
    query   = load_query_from_file(QUERY_FILE)

    _info(f"Buscando datas no Unico ({start} → {end})...")
    dates = get_dates_with_data(unico, start, end)

    if not dates:
        _warn("Nenhuma data encontrada no Unico para o período.")
        return

    _info(f"{len(dates)} datas a processar ({dates[0]} → {dates[-1]})")
    if dry_run:
        _info("Modo dry-run ativo — nenhuma alteração será gravada.")

    processed, failed, total_records = [], [], 0

    for i, dt in enumerate(dates, 1):
        try:
            n = process_date(dt, unico, mercado, query, dry_run)
            total_records += n
            processed.append(dt)
            if i % 10 == 0 or i == len(dates):
                _info(f"  {i}/{len(dates)} datas | {total_records} linhas {'simuladas' if dry_run else 'upsertadas'}")
        except Exception as e:
            _error(f"  Falha em {dt}: {e}")
            failed.append({"date": dt, "error": str(e)})

    _info("=" * 60)
    _info(
        f"Concluído: {len(processed)} datas OK, {len(failed)} falhas, "
        f"{total_records} linhas {'simuladas' if dry_run else 'upsertadas'}."
    )
    if failed:
        _warn(f"Datas com falha: {[f['date'] for f in failed]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill movimentacao_estoque do Unico — UPSERT puro, corrige ICMS retroativamente"
    )
    parser.add_argument(
        "--start-date",
        default=str(DEFAULT_START),
        help=f"Data inicial (YYYY-MM-DD). Padrão: {DEFAULT_START}",
    )
    parser.add_argument(
        "--end-date",
        default=str(DEFAULT_END),
        help=f"Data final inclusiva (YYYY-MM-DD). Padrão: {DEFAULT_END}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula o processamento sem gravar nada no destino",
    )
    args = parser.parse_args()

    run(
        start=date.fromisoformat(args.start_date),
        end=date.fromisoformat(args.end_date),
        dry_run=args.dry_run,
    )
