SELECT
    ne.chave,
    ne.id_tipo_pagamento,
    nep.descricao_produto_ne,
    nep.ncm_produto_ne,
    nep.cfop_produto_ne,
    nep.unidade_produto_ne,
    nep.quantidade_produto_ne,
    nep.valor_icms_produto_ne,
    nep.valor_total_produto_ne,
    nep.valor_unitario_produto_ne,
    nep.fator_multiplicativo_ne,
    nep.quantidade_total_produto_fatorado_ne,
    nep.valor_unitario_produto_fatorado_ne,
    nep.id_produto,
    nep.notLinkado,
    nep.cfop_xml,
    nep.codigo_produto_cad
FROM notas_entrada ne
LEFT JOIN notas_entrada_produto nep
    ON nep.id_nota_entrada = ne.id
WHERE (ne.excluido IS NULL OR ne.excluido = 0)
  AND (ne.SITUACAO IS NULL OR ne.SITUACAO <> 'C')
-- AND ne.data_emissao >= CURDATE() - INTERVAL 180 DAY
