SELECT
    MIN(n.numero)                                                   AS codigo,
    m.chave_nfe,
    STR_TO_DATE(MIN(m.dhEmi), '%d/%m/%Y %H:%i:%s')                 AS data_emissao,
    MIN(m.xNome)                                                    AS fornecedor,
    MIN(m.CNPJCPF)                                                  AS cpnj_cpf,
    CAST(REPLACE(MIN(m.vNF), ',', '.') AS DECIMAL(15,2))            AS valor,
    MIN(n.natureza_operacao)                                        AS natureza_operacao,
    FALSE                                                           AS processed,
    MIN(TIMESTAMP(n.data_edicao, n.hora_chegada))                   AS data_hora_entrada,

    MAX(CASE
        WHEN n.chave IS NULL THEN 'Pendente Importação'
        WHEN n.movimentacao_estoque = 1 AND n.status_alt_preco = 1 THEN 'Processado Total'
        WHEN n.movimentacao_estoque = 1 AND n.status_alt_preco = 0 THEN 'Estoque Atualizado (Preço Pendente)'
        ELSE 'Em Processamento'
    END)                                                            AS status_processamento,

    MAX(m.deuCiencia)                                               AS manifestacao,
    IF(SUM(m.schemaType = 'procNFe') > 0, 'XML Disponível', 'Apenas Resumo') AS status_xml,
    MIN(n.id_usuario_insercao)                                      AS id_usuario_insercao

FROM dfe_server_log_monitor_notas m
LEFT JOIN `gtech-gestao`.notas_entrada n
    ON n.chave = m.chave_nfe
    AND n.movimentacao_estoque = 1

GROUP BY
    m.chave_nfe;