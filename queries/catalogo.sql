SELECT
    p.ID as sku,
    p.gtin as ean,
    p.DESCRICAOPDV as nome,
    p.DESCRICAOPDV as nome_pdv,
    p.DATA_CADASTRO as cadastro_at,
    uc.data_ultima_compra as ultima_compra_at,
    p.DATA_ULTIMA_VENDA as ultima_venda_at,
    p.DATA_EDICAO as edited_at,
    uc.ultimo_preco_compra as preco_ultima_compra,
    pptp.preco as preco_venda,
    pptp.CUSTO_FINAL as preco_custo,
    uc.fator_multiplicativo as fator_multiplicativo,
    uc.cean_no_fornecedor as cean_no_fornecedor,
    COALESCE(ep.qtd_estoque, 0) as stock,
    p.unidade as unidade_venda,
    p.BALANCA_INTEGRADA as balanca_integrada,
    p.IMAGEM as imagem,
    p.ID_GRUPO as id_grupo,
    p.QTD_POR_CAIXA as qtd_por_caixa,
    -- impostos
    p.CEST as cest,
    p.NCM as ncm,
    p.ID_REGRA_ICMS as id_regra_icms,
    p.ID_GRUPO_IPI as id_grupo_ipi,
    p.ID_GRUPO_PIS as id_grupo_pis,
    p.ID_GRUPO_COFINS as id_grupo_cofins,
    p.PAF_P_ST as paf_p_st,
    p.IPPT as ippt,
    p.IAT as iat,
    p.ECF_ICMS_ST as ecf_icms_st,
    -- flag de produto desativado no Uniplus (EXCLUIDO continua filtrado abaixo)
    p.DESATIVADO as desativado
FROM produto p
LEFT JOIN preco_produto_tipo_pagamento pptp
    ON pptp.id_produto = p.ID 
    AND pptp.id_tipopagamento = 1 
    AND pptp.id_emitente = 1
LEFT JOIN (
    SELECT
        ppf.id_produto,
        ppf.data_ultima_compra,
        ppf.ultimo_preco_compra,
        ppf.fator_multiplicativo,
        ppf.cean_no_fornecedor
    FROM preco_produto_fornecedor ppf
    WHERE ppf.id IN (
        SELECT MAX(id)
        FROM preco_produto_fornecedor
        GROUP BY id_produto
    )
) uc ON uc.id_produto = p.ID
LEFT JOIN estoque_produto ep
    ON ep.id_produto = p.ID AND ep.id_filial = 1
WHERE p.EXCLUIDO = 0;