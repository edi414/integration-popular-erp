import time
import os
import sentry_sdk
from dotenv import load_dotenv
from services.vendas_daily import VendasDailyETL
from services.notas_fiscais import NotasFiscaisETL
from services.catalogo import CatalogoETL
from services.contas_a_pagar import ContasAPagarETL
from services.movimentacao_estoque import MovimentacaoEstoqueETL
from services.nfe_linkagem import NfeLinkagemETL
from services.nfe_processor import NFeProcessorETL
from services.produtos_similares import ProdutosSimilaresService
from settings.db_config import G3_DATABASE, get_target_config
from handlers.log_handler import app_logger

load_dotenv()
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    send_default_pii=True,
)


def run_vendas_daily_etl():
    etl = VendasDailyETL(G3_DATABASE, get_target_config())
    return etl.run_etl()


def run_notas_fiscais_etl():
    etl = NotasFiscaisETL(G3_DATABASE, get_target_config())
    etl.run_etl()


def run_nfe_processor_etl():
    etl = NFeProcessorETL(get_target_config())
    etl.run_etl()


def run_catalogo_etl():
    etl = CatalogoETL(G3_DATABASE, get_target_config())
    etl.run_etl()


def run_contas_a_pagar_etl():
    etl = ContasAPagarETL(G3_DATABASE, get_target_config())
    return etl.run_etl()


def run_movimentacao_estoque_etl():
    etl = MovimentacaoEstoqueETL(G3_DATABASE, get_target_config())
    return etl.run_etl()


def run_nfe_linkagem_etl():
    etl = NfeLinkagemETL(G3_DATABASE, get_target_config())
    etl.run_etl()

if __name__ == "__main__":
    app_logger.info("Iniciando Vendas Daily ETL...")
    v_summary = run_vendas_daily_etl()
    app_logger.info(f"   Processados: {v_summary['processed']}, Falhas: {v_summary['failed']}")

    app_logger.info("Iniciando Notas Fiscais ETL (Extracao G3 + Download SEFAZ)...")
    try:
        run_notas_fiscais_etl()
        app_logger.info("Aguardando 60 segundos antes de processar os XMLs...")
        time.sleep(60)
        app_logger.info("Iniciando Refinamento de Dados XML (NFe Processor)...")
        run_nfe_processor_etl()
    except Exception as e:
        app_logger.warning(f"   [WARN] Notas Fiscais/NFe Processor falhou: {e}")

    app_logger.info("Iniciando Catalogo ETL (Sincronizacao de Produtos)...")
    run_catalogo_etl()

    app_logger.info("Iniciando compute de Produtos Similares (FAISS)...")
    try:
        svc = ProdutosSimilaresService(get_target_config())
        n_pairs = svc.run()
        app_logger.info(f"   Pares similares armazenados: {n_pairs}")
    except Exception as e:
        app_logger.warning(f"   [WARN] Produtos Similares falhou: {e}")

    app_logger.info("Iniciando Contas a Pagar ETL...")
    cap_summary = run_contas_a_pagar_etl()
    app_logger.info(f"   Registros processados: {cap_summary['processed']}")

    app_logger.info("Iniciando Movimentacao de Estoque ETL...")
    est_summary = run_movimentacao_estoque_etl()
    app_logger.info(f"   Processados: {est_summary['processed']}, Falhas: {est_summary['failed']}")

    app_logger.info("Iniciando NFe Linkagem ETL (Itens de Notas de Entrada + Vinculo de Produto)...")
    try:
        run_nfe_linkagem_etl()
    except Exception as e:
        app_logger.warning(f"   [WARN] NFe Linkagem falhou: {e}")

    app_logger.info("Todos os processos ETL foram finalizados com sucesso.")