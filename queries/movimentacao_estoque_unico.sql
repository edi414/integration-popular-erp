WITH ult_notafiscalitem AS (
    SELECT
        idnotafiscal,
        produto,
        SUM(icms)               AS icms,
        SUM(icmssubstituicao)   AS icmssubstituicao,
        SUM(ipi)                AS ipi,
        SUM(pis)                AS pis,
        SUM(cofins)             AS cofins,
        SUM(outrosimpostospreco) AS outrosimpostospreco,
        SUM(comissao)           AS comissao,
        MAX(tributacao)         AS tributacao,
        MAX(cfop)               AS cfop,
        MAX(unidade)            AS unidade
    FROM public.notafiscalitem
    GROUP BY idnotafiscal, produto
),
agg_item AS (
    SELECT
        idoperacao,
        produto,
        SUM(icms)    / NULLIF(SUM(quantidade), 0) AS icms_por_unidade,
        SUM(pis)     / NULLIF(SUM(quantidade), 0) AS pis_por_unidade,
        SUM(cofins)  / NULLIF(SUM(quantidade), 0) AS cofins_por_unidade,
        SUM(comissao)/ NULLIF(SUM(quantidade), 0) AS comissao_por_unidade,
        MAX(ippt)          AS ippt,
        MAX(cfop)          AS cfop,
        MAX(unidademedida) AS unidademedida
    FROM public.item
    GROUP BY idoperacao, produto
)
SELECT
    'Geral' AS local_estoque,
    1 AS filial,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n.numeronotafiscal::text
        WHEN m.tipodocumento::text IN ('1') THEN o.serienfce::text || '/' || o.numeronfce::text
        ELSE NULL
    END AS documento,
    m.idoriginal AS id_documento,
    p.codigo,
    p.nome,
    m.datahora,
    m.currenttimemillis,
    m.tipodocumento,
    CASE
        WHEN m.quantidadeentrada IS NULL OR m.quantidadeentrada = 0 THEN m.quantidadesaida
        ELSE m.quantidadeentrada
    END AS qtd,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN 'E'
        WHEN m.tipodocumento::text IN ('1') THEN 'S'
        ELSE NULL
    END AS tipo_movimentacao,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n.chavenfe
        ELSE NULL
    END AS chave_nfe,
    m.valortotal,
    m.precoultimacompra,
    m.custoaquisicao,
    m.customedio,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n2.icms
        WHEN m.tipodocumento::text IN ('1') THEN m.qtd * i.icms_por_unidade
        ELSE NULL
    END AS icms,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n2.icmssubstituicao
        WHEN m.tipodocumento::text IN ('1') THEN NULL
        ELSE NULL
    END AS icms_st,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n2.tributacao
        WHEN m.tipodocumento::text IN ('1') THEN i.ippt
        ELSE NULL
    END AS ippt,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN (COALESCE(n2.pis, 0) + COALESCE(n2.cofins, 0))
        WHEN m.tipodocumento::text IN ('1') THEN m.qtd * (COALESCE(i.pis_por_unidade, 0) + COALESCE(i.cofins_por_unidade, 0))
        ELSE NULL
    END AS pis_cofins,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n2.ipi
        WHEN m.tipodocumento::text IN ('1') THEN NULL
        ELSE NULL
    END AS ipi,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n2.outrosimpostospreco
        WHEN m.tipodocumento::text IN ('1') THEN NULL
        ELSE NULL
    END AS outros_impostos,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n2.comissao
        WHEN m.tipodocumento::text IN ('1') THEN m.qtd * i.comissao_por_unidade
        ELSE NULL
    END AS comissao,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n2.cfop
        WHEN m.tipodocumento::text IN ('1') THEN i.cfop
        ELSE NULL
    END AS cfop,
    CASE
        WHEN m.tipodocumento::text IN ('2', '3') THEN n2.unidade::text
        WHEN m.tipodocumento::text IN ('1') THEN i.unidademedida::text
        ELSE NULL
    END AS un

FROM (
    SELECT DISTINCT ON (currenttimemillis, idproduto, idoriginal, tipodocumento)
        *,
        CASE
            WHEN quantidadeentrada IS NULL OR quantidadeentrada = 0 THEN quantidadesaida
            ELSE quantidadeentrada
        END AS qtd
    FROM public.movimentoestoque
    WHERE DATE(datahora) = %(data)s
      AND cancelado = 0
    ORDER BY currenttimemillis, idproduto, idoriginal, tipodocumento, id DESC
) m
INNER JOIN public.produto p
    ON p.id::text = m.idproduto::text
LEFT JOIN public.operacao o
    ON m.idoriginal = o.id
LEFT JOIN public.notafiscal n
    ON m.idoriginal = n.id
LEFT JOIN ult_notafiscalitem n2
    ON m.idoriginal = n2.idnotafiscal AND p.codigo = n2.produto
LEFT JOIN agg_item i
    ON o.id = i.idoperacao AND p.codigo = i.produto;
