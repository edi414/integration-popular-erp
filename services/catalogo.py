import pandas as pd
from handlers.db_connection import DatabaseConnection
from handlers.query_loader import get_etl_query, get_etl_config
from utils.data_transformers import clean_dataframe_nans
from handlers.log_handler import setup_logger
from services.embedding_service import EmbeddingService
from typing import Dict

class CatalogoETL:
    def __init__(self, source_config: Dict, target_config: Dict):
        self.source_connection = DatabaseConnection(source_config)
        self.target_connection = DatabaseConnection(target_config)
        self.embedding_service = EmbeddingService()
        self.logger = setup_logger("catalogo_etl", log_file="logs/catalogo_etl.log")
        
    def transform_data(self, df: pd.DataFrame) -> pd.DataFrame:
        try:
            self.logger.info(f"Transforming {len(df)} records")
            
            # Text columns to ensure they are strings
            text_columns = [
                'sku', 'ean', 'nome', 'nome_pdv', 'cean_no_fornecedor',
                'unidade_venda', 'imagem', 'cest', 'ncm', 'ippt', 'iat', 'paf_p_st'
            ]
            for col in text_columns:
                if col in df.columns:
                    df[col] = df[col].astype(str)
            
            # Numeric columns
            numeric_columns = [
                'preco_venda', 'preco_ultima_compra', 'stock', 
                'preco_custo', 'fator_multiplicativo', 'qtd_por_caixa'
            ]
            for col in numeric_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                    
            # Integer columns that might have nulls
            int_columns = [
                'balanca_integrada', 'id_grupo', 'id_regra_icms', 
                'id_grupo_ipi', 'id_grupo_pis', 'id_grupo_cofins', 'ecf_icms_st', 'desativado'
            ]
            for col in int_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').round().astype('Int64')

            # Datetime columns
            date_columns = ['cadastro_at', 'ultima_compra_at', 'ultima_venda_at', 'edited_at']
            for col in date_columns:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], errors='coerce').dt.date

            # Rename columns to match target table catalogo.produtos
            column_mapping = {
                'sku': 'codigo',
                'stock': 'estoque',
                'balanca_integrada': 'pesavel',
                'ultima_venda_at': 'ultima_venda',
                'ultima_compra_at': 'ultima_compra',
                'imagem': 'imagem_url'
            }
            df = df.rename(columns=column_mapping)
            
            # Map pesavel (1/0) to ('S'/'N')
            if 'pesavel' in df.columns:
                df['pesavel'] = df['pesavel'].map({1: 'S', 0: 'N'}).fillna('N')

            # List of all target columns to be sent to the database
            target_columns = [
                'codigo', 'ean', 'nome', 'nome_pdv', 'unidade_venda', 
                'preco_venda', 'estoque', 'ncm', 'pesavel',
                'ultima_venda', 'ultima_compra', 'cadastro_at', 'edited_at',
                'preco_ultima_compra', 'preco_custo', 'fator_multiplicativo',
                'cean_no_fornecedor', 'id_grupo', 'qtd_por_caixa', 'cest',
                'id_regra_icms', 'id_grupo_ipi', 'id_grupo_pis', 'id_grupo_cofins',
                'paf_p_st', 'ippt', 'iat', 'ecf_icms_st', 'desativado', 'imagem_url', 'embedding'
            ]
            
            # Generate embeddings for the products
            self.logger.info("Generating embeddings for products")
            df = self.embedding_service.process_dataframe(df, self.target_connection, text_col='nome')
            
            df = df[[c for c in target_columns if c in df.columns]]
            
            return clean_dataframe_nans(df)
            
        except Exception as e:
            self.logger.error(f"Error during transformation: {str(e)}")
            raise

    def extract_data(self) -> pd.DataFrame:
        try:
            self.logger.info("Starting data extraction")
            query = get_etl_query('catalogo')
            result = self.source_connection.get_data(query)
            self.logger.info(f"Extracted {len(result)} records")
            return result
            
        except Exception as e:
            self.logger.error(f"Error during extraction: {str(e)}")
            raise

    def load_data(self, df: pd.DataFrame) -> None:
        try:
            self.logger.info(f"Loading {len(df)} records")

            config = get_etl_config('catalogo')
            table_name = config.get('table', 'produtos')
            schema = config.get('schema', 'catalogo')

            # Remove products no longer in the source (EXCLUIDO=1 filtered out)
            current_codes = df['codigo'].dropna().tolist()
            self.target_connection.connect()
            with self.target_connection.connection.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {schema}.{table_name} WHERE codigo <> ALL(%s)",
                    (current_codes,)
                )
                deleted = cur.rowcount
                self.target_connection.connection.commit()
            self.target_connection.disconnect()
            if deleted:
                self.logger.info(f"Removed {deleted} discontinued products from {schema}.{table_name}")

            # Produtos com novo embedding: upsert completo
            df_new_emb = df[df['embedding'].notna()]
            # Produtos com embedding já no banco: upsert sem a coluna embedding
            df_cached_emb = df[df['embedding'].isna()].drop(columns=['embedding'])

            if not df_new_emb.empty:
                self.logger.info(f"Upserting {len(df_new_emb)} products with new embeddings")
                self.target_connection.upsert(table_name=table_name, data=df_new_emb, unique_columns=['codigo'], schema=schema)
            if not df_cached_emb.empty:
                self.logger.info(f"Upserting {len(df_cached_emb)} products preserving existing embeddings")
                self.target_connection.upsert(table_name=table_name, data=df_cached_emb, unique_columns=['codigo'], schema=schema)

            self.logger.info("Data load completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error during data load: {str(e)}")
            raise

    def run_etl(self) -> None:
        try:
            self.logger.info("Starting catalog ETL process")
            
            raw_data = self.extract_data()
            
            if raw_data.empty:
                self.logger.warning("No data found in source")
                return
            
            transformed_data = self.transform_data(raw_data)
            self.load_data(transformed_data)
            
            self.logger.info("ETL process completed successfully")
            
        except Exception as e:
            self.logger.error(f"ETL process failed: {str(e)}")
            raise