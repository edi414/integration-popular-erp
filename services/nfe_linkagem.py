import pandas as pd
from handlers.db_connection import DatabaseConnection
from handlers.query_loader import get_etl_query, get_etl_config
from handlers.log_handler import setup_logger
from utils.data_transformers import clean_dataframe_nans
from typing import Dict

class NfeLinkagemETL:
    def __init__(self, source_config: Dict, target_config: Dict):
        self.source_connection = DatabaseConnection(source_config)
        self.target_connection = DatabaseConnection(target_config)
        self.logger = setup_logger("nfe_linkagem_etl", log_file="logs/nfe_linkagem_etl.log")

    def transform_data(self, df: pd.DataFrame) -> pd.DataFrame:
        try:
            self.logger.info(f"Transformando {len(df)} registros")

            df = df.rename(columns={'notLinkado': 'not_linkado'})

            # chave é obrigatória para a reconciliação por documento — filtrar antes
            # de qualquer cast para string (que transformaria NaN em "nan").
            df = df[df['chave'].notna()]

            # Colunas de texto
            text_columns = [
                'chave', 'descricao_produto_ne', 'ncm_produto_ne', 'cfop_produto_ne',
                'unidade_produto_ne', 'cfop_xml', 'codigo_produto_cad'
            ]
            for col in text_columns:
                if col in df.columns:
                    df[col] = df[col].astype(str)

            # Colunas numéricas (decimal(20,6) na origem)
            numeric_columns = [
                'quantidade_produto_ne', 'valor_icms_produto_ne', 'valor_total_produto_ne',
                'valor_unitario_produto_ne', 'fator_multiplicativo_ne',
                'quantidade_total_produto_fatorado_ne', 'valor_unitario_produto_fatorado_ne'
            ]
            for col in numeric_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            # Colunas inteiras nullable (int unsigned na origem)
            int_columns = ['id_tipo_pagamento', 'id_produto']
            for col in int_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').round().astype('Int64')

            # notLinkado (tinyint 0/1) -> boolean
            if 'not_linkado' in df.columns:
                df['not_linkado'] = df['not_linkado'].map({1: True, 0: False})

            columns = [
                'chave', 'id_tipo_pagamento', 'descricao_produto_ne', 'ncm_produto_ne',
                'cfop_produto_ne', 'unidade_produto_ne', 'quantidade_produto_ne',
                'valor_icms_produto_ne', 'valor_total_produto_ne', 'valor_unitario_produto_ne',
                'fator_multiplicativo_ne', 'quantidade_total_produto_fatorado_ne',
                'valor_unitario_produto_fatorado_ne', 'id_produto', 'not_linkado',
                'cfop_xml', 'codigo_produto_cad'
            ]

            return clean_dataframe_nans(df[columns])

        except Exception as e:
            self.logger.error(f"Erro na transformação de dados: {str(e)}")
            raise

    def extract_data(self) -> pd.DataFrame:
        try:
            query = get_etl_query('nfe_linkagem')
            result = self.source_connection.get_data(query)
            self.logger.info(f"Extraídos {len(result)} registros")
            return result
        except Exception as e:
            self.logger.error(f"Erro na extração de dados: {str(e)}")
            raise

    def load_data(self, df: pd.DataFrame) -> None:
        try:
            config = get_etl_config('nfe_linkagem')
            table_name = config.get('table', 'nfe_linkagem')
            schema = config.get('schema', 'public')

            self.logger.info(f"Reconciliando por documento {len(df)} registros na tabela {table_name}")
            self.target_connection.reconcile_by_document(
                table_name=table_name,
                data=df,
                doc_columns=['chave'],
                schema=schema,
            )
        except Exception as e:
            self.logger.error(f"Erro na carga de dados (reconcile_by_document): {str(e)}")
            raise

    def run_etl(self) -> None:
        try:
            self.logger.info("Iniciando processo ETL de NFe Linkagem...")

            raw_data = self.extract_data()
            if raw_data.empty:
                self.logger.warning("Nenhum dado capturado em notas_entrada/notas_entrada_produto.")
                return

            transformed_data = self.transform_data(raw_data)
            if transformed_data.empty:
                self.logger.warning("Nenhum registro válido após transformação (chave nula descartada).")
                return

            self.load_data(transformed_data)

            self.logger.info("Processo ETL de NFe Linkagem finalizado.")

        except Exception as e:
            self.logger.error(f"Falha crítica no ETL de NFe Linkagem: {str(e)}")
            raise
