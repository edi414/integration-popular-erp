import os
import json
import psycopg2
from psycopg2.extras import execute_values
import mysql.connector
from mysql.connector import errorcode
import pandas as pd
from typing import Dict, List, Union, Optional
from tqdm import tqdm
from .log_handler import setup_logger

class DatabaseConnection:
    def __init__(self, connection_config: Union[str, Dict]):
        self.logger = setup_logger("database", log_file="logs/database.log")
        
        if isinstance(connection_config, str):
            try:
                self.config = json.loads(connection_config)
            except json.JSONDecodeError as e:
                self.logger.error(f"Invalid JSON configuration: {e}")
                raise
        else:
            self.config = connection_config
            
        self.connection = None
        self._validate_config()
        self.engine = 'mysql' if str(self.config.get('port')) == '3306' else 'postgres'
        self.logger.info(f"Database engine detected: {self.engine}")
        
    def _validate_config(self):
        required_fields = ['host', 'port', 'dbname', 'user', 'password']
        missing_fields = [field for field in required_fields if field not in self.config]
        
        if missing_fields:
            error_msg = f"Missing required configuration fields: {', '.join(missing_fields)}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
            
    def _get_connection_string(self) -> str:
        return (
            f"host={self.config['host']} "
            f"port={self.config['port']} "
            f"dbname={self.config['dbname']} "
            f"user={self.config['user']} "
            f"password={self.config['password']}"
        )
    
    def connect(self) -> None:
        try:
            if not self.connection or (self.engine == 'postgres' and self.connection.closed) or (self.engine == 'mysql' and not self.connection.is_connected()):
                if self.engine == 'postgres':
                    self.connection = psycopg2.connect(self._get_connection_string())
                else:
                    self.connection = mysql.connector.connect(
                        host=self.config['host'],
                        port=self.config['port'],
                        user=self.config['user'],
                        password=self.config['password'],
                        database=self.config['dbname']
                    )
                self.logger.info(f"{self.engine.capitalize()} database connection established successfully")
        except Exception as e:
            self.logger.error(f"Failed to connect to {self.engine} database: {e}")
            raise
            
    def disconnect(self) -> None:
        if self.connection:
            if self.engine == 'postgres' and not self.connection.closed:
                self.connection.close()
            elif self.engine == 'mysql' and self.connection.is_connected():
                self.connection.close()
            self.logger.info(f"{self.engine.capitalize()} database connection closed")
            
    def __enter__(self):
        self.connect()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        
    def get_data(self, query: str, params: Optional[tuple] = None) -> pd.DataFrame:
        """
        Execute a query and return results as a DataFrame.
        
        Args:
            query: SQL query to execute
            params: Optional parameters for the query
            
        Returns:
            DataFrame containing query results
        """
        try:
            self.connect()
            cursor = self.connection.cursor()
            try:
                # Use named parameters directly (pyformat) which is supported by both psycopg2 and mysql-connector
                processed_query = query
                
                cursor.execute(processed_query, params)
                
                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    data = cursor.fetchall()
                    return pd.DataFrame(data, columns=columns)
                return pd.DataFrame()
            finally:
                cursor.close()
        except Exception as e:
            self.logger.error(f"Error executing query: {e}")
            raise
        finally:
            self.disconnect()
            
    def insert_batch(self, table_name: str, data: Union[pd.DataFrame, List[Dict]], 
                    schema: str = 'public', batch_size: int = 1000) -> None:
        """
        Insert multiple records in batches.
        
        Args:
            table_name: Name of the target table
            data: DataFrame or list of dictionaries containing data
            schema: Database schema name
            batch_size: Number of records per batch
        """
        if isinstance(data, pd.DataFrame):
            data = data.to_dict('records')
            
        if not data:
            self.logger.warning("No data provided for batch insert")
            return
            
        try:
            self.connect()
            with self.connection.cursor() as cursor:
                # Get column names from first record
                columns = list(data[0].keys())
                values = [tuple(record[col] for col in columns) for record in data]
                
                # Prepare the query
                column_names = ','.join(columns)
                
                if self.engine == 'postgres':
                    placeholders = "%s"
                    query = f"INSERT INTO {schema}.{table_name} ({column_names}) VALUES {placeholders}"
                    # Execute in batches with progress bar
                    total_batches = (len(values) + batch_size - 1) // batch_size
                    with tqdm(total=total_batches, desc="Inserting batches") as pbar:
                        for i in range(0, len(values), batch_size):
                            batch = values[i:i + batch_size]
                            execute_values(cursor, query, batch)
                            pbar.update(1)
                else:
                    placeholders = ', '.join(['%s'] * len(columns))
                    query = f"INSERT INTO `{table_name}` ({column_names}) VALUES ({placeholders})"
                    total_batches = (len(values) + batch_size - 1) // batch_size
                    with tqdm(total=total_batches, desc="Inserting batches") as pbar:
                        for i in range(0, len(values), batch_size):
                            batch = values[i:i + batch_size]
                            cursor.executemany(query, batch)
                            pbar.update(1)
                    
                self.connection.commit()
                self.logger.info(f"Successfully inserted {len(data)} records into {schema}.{table_name}")
                
        except Exception as e:
            self.logger.error(f"Error in batch insert: {e}")
            if self.connection:
                self.connection.rollback()
            raise
        finally:
            self.disconnect()
            
    def upsert(self, table_name: str, data: Union[pd.DataFrame, Dict], 
               unique_columns: List[str], schema: str = 'public', batch_size: int = 1000) -> None:
        """
        Perform an upsert operation (INSERT ... ON CONFLICT DO UPDATE) in batches.
        
        Args:
            table_name: Name of the target table
            data: DataFrame or dictionary containing data
            unique_columns: List of columns that form the unique constraint
            schema: Database schema name
            batch_size: Number of records per batch
        """
        if isinstance(data, pd.DataFrame):
            data = data.to_dict('records')
        elif isinstance(data, dict):
            data = [data]
            
        if not data:
            self.logger.warning("No data provided for upsert")
            return
            
        try:
            self.connect()
            with self.connection.cursor() as cursor:
                # Get column names from first record
                columns = list(data[0].keys())
                values = [tuple(record[col] for col in columns) for record in data]
                
                if self.engine == 'postgres':
                    # Build the ON CONFLICT clause
                    conflict_columns = ','.join(unique_columns)
                    update_columns = [col for col in columns if col not in unique_columns]
                    update_set = ','.join([f"{col} = EXCLUDED.{col}" for col in update_columns])
                    
                    # Prepare the query
                    query = f"""
                        INSERT INTO {schema}.{table_name} 
                        ({','.join(columns)}) 
                        VALUES %s
                        ON CONFLICT ({conflict_columns})
                        DO UPDATE SET {update_set}
                    """
                    
                    # Execute in batches with progress bar
                    total_batches = (len(values) + batch_size - 1) // batch_size
                    with tqdm(total=total_batches, desc="Upserting batches") as pbar:
                        for i in range(0, len(values), batch_size):
                            batch = values[i:i + batch_size]
                            execute_values(cursor, query, batch)
                            pbar.update(1)
                else:
                    # MySQL ON DUPLICATE KEY UPDATE
                    update_columns = [col for col in columns if col not in unique_columns]
                    update_set = ','.join([f"`{col}` = VALUES(`{col}`)" for col in update_columns])
                    placeholders = ', '.join(['%s'] * len(columns))
                    
                    query = f"""
                        INSERT INTO `{table_name}` 
                        ({','.join([f'`{c}`' for c in columns])}) 
                        VALUES ({placeholders})
                        ON DUPLICATE KEY UPDATE {update_set}
                    """
                    
                    total_batches = (len(values) + batch_size - 1) // batch_size
                    with tqdm(total=total_batches, desc="Upserting batches") as pbar:
                        for i in range(0, len(values), batch_size):
                            batch = values[i:i + batch_size]
                            cursor.executemany(query, batch)
                            pbar.update(1)
                    
                self.connection.commit()
                self.logger.info(f"Successfully upserted {len(data)} records into {schema}.{table_name}")
                
        except Exception as e:
            self.logger.error(f"Error in upsert operation: {e}")
            if self.connection:
                self.connection.rollback()
            raise
        finally:
            self.disconnect()

    def reconcile_by_document(self, table_name: str, data: Union[pd.DataFrame, Dict],
                              doc_columns: List[str], schema: str = 'public',
                              batch_size: int = 500, dry_run: bool = False) -> dict:
        """
        Substitui atomicamente todas as linhas de cada documento presente em `data`:
        DELETE das linhas cujas colunas `doc_columns` batem com as do extrato, seguido
        de INSERT das linhas atuais — tudo numa única transação.

        Usado para ENTRADAS, onde o id da linha (currenttimemillis = nep.id) é
        REGENERADO a cada edição da nota no ERP. Um upsert por linha deixaria a linha
        antiga órfã; ancorar no documento estável (ex.: tipodocumento + id_documento)
        elimina o órfão porque o DELETE remove TODAS as linhas da nota antes de reinserir.

        IMPORTANTE: linhas com qualquer `doc_column` NULL devem ser filtradas pelo
        chamador (NULL não casa no DELETE e geraria duplicata). Destino PostgreSQL.

        Se `dry_run=True`: executa DELETE+INSERT na transação, contabiliza o impacto
        e dá ROLLBACK (não persiste nada). Retorna um dict com as contagens.
        NUNCA usa TRUNCATE — o DELETE é sempre escopado às chaves de documento.
        """
        if isinstance(data, pd.DataFrame):
            data = data.to_dict('records')
        elif isinstance(data, dict):
            data = [data]

        if not data:
            self.logger.warning("No data provided for reconcile_by_document")
            return {"documentos": 0, "linhas_deletadas": 0, "linhas_inseridas": 0, "dry_run": dry_run}

        if self.engine != 'postgres':
            raise NotImplementedError("reconcile_by_document só suporta destino PostgreSQL")

        columns = list(data[0].keys())
        values = [tuple(record[col] for col in columns) for record in data]
        # Chaves de documento distintas presentes no extrato
        doc_keys = sorted({tuple(record[c] for c in doc_columns) for record in data})

        cols_sql = ", ".join(doc_columns)
        delete_q = (
            f"DELETE FROM {schema}.{table_name} t "
            f"USING (VALUES %s) AS v({cols_sql}) "
            f"WHERE " + " AND ".join(f"t.{c} = v.{c}" for c in doc_columns)
        )
        insert_q = f"INSERT INTO {schema}.{table_name} ({', '.join(columns)}) VALUES %s"

        deleted = 0
        try:
            self.connect()
            with self.connection.cursor() as cursor:
                # 1) Apaga as linhas dos documentos afetados (em lotes)
                for i in range(0, len(doc_keys), batch_size):
                    chunk = doc_keys[i:i + batch_size]
                    execute_values(cursor, delete_q, chunk, page_size=batch_size)
                    deleted += cursor.rowcount
                # 2) Reinsere o estado atual de todas as linhas
                total_batches = (len(values) + batch_size - 1) // batch_size
                with tqdm(total=total_batches, desc="Reconciling (delete+insert)") as pbar:
                    for i in range(0, len(values), batch_size):
                        execute_values(cursor, insert_q, values[i:i + batch_size])
                        pbar.update(1)

            if dry_run:
                self.connection.rollback()
                self.logger.info(
                    f"[DRY-RUN] reconcile_by_document: {len(doc_keys)} documentos, "
                    f"{deleted} linhas seriam deletadas e {len(values)} reinseridas em "
                    f"{schema}.{table_name} — ROLLBACK (nada persistido)."
                )
            else:
                self.connection.commit()
                self.logger.info(
                    f"reconcile_by_document: {len(doc_keys)} documentos reconciliados, "
                    f"{deleted} linhas deletadas, {len(values)} reinseridas em {schema}.{table_name}"
                )
            return {
                "documentos": len(doc_keys),
                "linhas_deletadas": deleted,
                "linhas_inseridas": len(values),
                "dry_run": dry_run,
            }
        except Exception as e:
            self.logger.error(f"Error in reconcile_by_document operation: {e}")
            if self.connection:
                self.connection.rollback()
            raise
        finally:
            self.disconnect()
