# Documentação para Agente de Busca por Linguagem Natural

## Visão Geral do Sistema

Este sistema integra o ERP local (G3/Uniplus) com um banco PostgreSQL centralizado (`banco_mercado`), permitindo rastrear compras, estoque e precificação de produtos a partir das Notas Fiscais eletrônicas (NF-e).

**Host do banco:** `187.127.45.49` | **Banco:** `banco_mercado` | **Porta:** `5432`

---

## Pipeline ETL (main.py)

O `main.py` roda na máquina local do servidor e executa sequencialmente:

```
1. VendasDailyETL       → sincroniza vendas do PDV
2. NotasFiscaisETL      → extrai NFs do G3, baixa XMLs da SEFAZ
   └── (sleep 60s)
3. NFeProcessorETL      → parseia os XMLs e popula precificacao
4. CatalogoETL          → sincroniza catálogo de produtos
5. ProdutosSimilaresETL → calcula similaridade entre produtos (FAISS)
6. ContasAPagarETL      → sincroniza contas a pagar
7. MovimentacaoEstoqueETL → sincroniza movimentação de estoque
```

---

## Tabelas Principais e seus Papéis

### `notas_fiscais`
Registro de todas as NF-e recebidas. Alimentada pelo `NotasFiscaisETL`.

| Coluna | Descrição |
|---|---|
| `chave` | Chave de 44 dígitos da NF-e (identificador único) |
| `codigo` | Número da nota fiscal |
| `fornecedor` | Razão social do fornecedor |
| `data_emissao` | Data de emissão da nota |
| `valor` | Valor total da nota |
| `arquivo_xml` | XML completo da NF-e (bytea) |
| `status_xml` | `"XML Disponível"` ou `"Apenas Resumo"` |
| `status_processamento` | Ver estados abaixo |
| `processed` | `'true'` se o XML já foi processado pelo NFeProcessor |

**Estados de `status_processamento`:**
- `"Processado Total"` — NF processada completamente
- `"Pendente Importação"` — NF recebida mas não processada
- `"Estoque Atualizado (Preço Pendente)"` — estoque lançado, preço ainda não atualizado
- `NULL / vazio` — estado desconhecido (14.661 registros históricos)

---

### `precificacao`
Itens extraídos dos XMLs das NF-e. Alimentada pelo `NFeProcessorETL` via `NFeParser.parse_items()`.

| Coluna | Descrição |
|---|---|
| `chave_nfe` | Chave da NF-e (FK para `notas_fiscais.chave`) |
| `ean` | EAN do produto **conforme informado pelo fornecedor no XML** (`<cEAN>`) |
| `ean_trib` | EAN tributado (`<cEANTrib>`) |
| `descricao` | Descrição do produto conforme fornecedor (`<xProd>`) |
| `quantidade` | Quantidade comprada |
| `valor_total` | Valor total do item |
| `preco_compra` | `valor_total + IPI + ICMS-ST` |
| `preco_min` | Preço mínimo sugerido (`preco_compra * 1.15 / quantidade`) |
| `insert_timestamp` | Data/hora do processamento |

**⚠️ Limitação crítica:** O `ean` nesta tabela vem diretamente do XML da NF-e. Fornecedores frequentemente informam EANs incorretos (versão errada do produto, embalagem-mãe, ou com dígito extra). Não confiar cegamente no EAN para identificar o produto.

---

### `catalogo`
Catálogo de produtos cadastrados no sistema. Alimentado pelo `CatalogoETL`.

| Coluna | Descrição |
|---|---|
| `sku` | Código interno do produto |
| `ean` | EAN oficial do produto no sistema |
| `nome` | Nome completo do produto |
| `nome_pdv` | Nome exibido no PDV |
| `preco_ultima_compra` | Último preço de compra registrado |
| `preco_venda` | Preço de venda atual |
| `stock` | Estoque atual |

---

### `movimentacao_estoque`
Todas as movimentações de estoque (entradas e saídas). Alimentada pelo `MovimentacaoEstoqueETL`.

| Coluna | Descrição |
|---|---|
| `documento` | Número do documento/nota |
| `chave_nfe` | Chave da NF-e associada |
| `codigo` | Código interno do produto (= `catalogo.sku`) |
| `nome` | Nome do produto conforme lançado no ERP |
| `datahora` | Data/hora da movimentação |
| `tipo_movimentacao` | `'E'` = Entrada, `'S'` = Saída |
| `tipodocumento` | `2` = NF de entrada, `1` = venda, `55` = NF-e, etc. |
| `qtd` | Quantidade |
| `valortotal` | Valor total |
| `chave_nfe` | Chave da NF-e (quando disponível) |

---

### `contas_a_pagar`
Títulos financeiros gerados pelas NFs. Alimentada pelo `ContasAPagarETL`.

| Coluna | Descrição |
|---|---|
| `documento` | **Chave de 44 dígitos da NF-e** (não o número da nota) |
| `razao_social` | Fornecedor |
| `valor` | Valor do título |
| `emissao` | Data de emissão |
| `vencimento` | Data de vencimento |
| `historico` | Contém o número da nota: `"Nota Fiscal: XXXXXX"` |

---

## Como Buscar um Produto a partir de um Item Físico

### Estratégia em camadas (ordem de confiança):

**1. Pelo EAN exato** — mais rápido, mas pode falhar se o fornecedor errou o EAN:
```sql
SELECT * FROM precificacao WHERE ean = '<EAN>'
  AND insert_timestamp >= NOW() - INTERVAL '90 days'
ORDER BY insert_timestamp DESC;
```

**2. Pelo catálogo** — verificar se o EAN está cadastrado e qual é o SKU:
```sql
SELECT sku, nome, ean FROM catalogo WHERE ean = '<EAN>';
```

**3. Pela descrição (fuzzy)** — usar `ILIKE` com partes do nome da marca + produto:
```sql
SELECT DISTINCT ean, descricao, chave_nfe, insert_timestamp
FROM precificacao
WHERE descricao ILIKE '%<MARCA>%' AND descricao ILIKE '%<PRODUTO>%'
  AND insert_timestamp >= NOW() - INTERVAL '90 days'
ORDER BY insert_timestamp DESC;
```

**4. Pela movimentação de estoque** — cruzar com o nome interno do produto:
```sql
SELECT m.documento, m.datahora, m.nome, m.chave_nfe, m.qtd, m.valortotal
FROM movimentacao_estoque m
WHERE m.nome ILIKE '%<TERMO>%'
  AND m.tipo_movimentacao = 'E'
  AND m.datahora >= NOW() - INTERVAL '90 days'
ORDER BY m.datahora DESC;
```

**5. Pelo fornecedor + data** — quando os métodos acima falham, cruzar pela chave da NF:
```sql
-- Após encontrar a chave_nfe, buscar o fornecedor em contas_a_pagar:
SELECT DISTINCT documento, razao_social, valor, emissao, historico
FROM contas_a_pagar
WHERE documento = '<chave_nfe>';
```

---

## Padrão de Qualidade de Dados: EAN Incorreto por Fornecedor

### Casos documentados:

| Produto Físico | EAN Real | Como o Fornecedor Registrou | Confiança |
|---|---|---|---|
| Galbani Requeijão Cremoso 190g | 7891097108280 | Desc: `"REQUEIJAO CREMOSO GALBANI 190G"` — EAN não mapeado | ✅ **Confirmado** — descrição específica o suficiente |
| OMO Sabão Em Pó Lavagem Perfeita 2.4kg Bag | 7891150064577 | EAN: `7891150064553` (1.6kg) — Desc: `"DET.PO OMO L.PERFEITA SCO"` | ⚠️ **Hipótese** — EAN aponta para 1.6kg; "SCO" (saco) sugere 2.4kg |

### Critério de confiança para documentação:

- **✅ Confirmado:** a descrição do fornecedor no XML identifica unicamente o produto (marca + nome + gramatura)
- **⚠️ Hipótese:** a descrição é genérica ou usa código interno; o EAN aponta para versão diferente; só há indício circunstancial
- **❌ Não identificável:** sem EAN correto e sem descrição específica

### Fornecedores com padrão de EAN incorreto identificado:
- **ATACADAO S.A.** — usa EAN de variante diferente do produto; descrições em código interno (`DT.PO`, `DT.LIQ`, `SCO`, `CART`, etc.)
- **GONCALVES SILVESTRE E CIA LTDA** — omite EAN do produto unitário, usa código de caixa

---

## Diagnóstico: NFs Pendentes de Processamento (base: 2026-07-04)

### Por que uma NF pode não ter itens na `precificacao`:

1. **`status_xml = "Apenas Resumo"`** — XML não disponível na SEFAZ ainda. O parser não consegue extrair itens sem o XML completo.
2. **`status_processamento = "Pendente Importação"`** — NF chegou ao banco mas o `NFeProcessorETL` não processou (processo local não rodou, ou XML ainda não estava disponível no momento).
3. **`status_processamento = "Estoque Atualizado (Preço Pendente)"`** — O ERP deu entrada no estoque via outro caminho (integração direta), mas o fluxo de precificação via XML não completou.
4. **XML com erro de parse** — O `NFeParser` falhou silenciosamente; a NF fica com `processed = NULL` e não entra na `precificacao`.

### Query para listar NFs pendentes:
```sql
SELECT nf.codigo, nf.data_emissao, nf.fornecedor, nf.valor,
       nf.status_processamento, nf.status_xml
FROM notas_fiscais nf
WHERE NOT EXISTS (
    SELECT 1 FROM precificacao p WHERE p.chave_nfe = nf.chave
)
  AND nf.data_emissao >= NOW() - INTERVAL '90 days'
ORDER BY nf.data_emissao DESC;
```

---

## Dicionário de Termos Internos (Fornecedores Atacadistas)

Para ajudar o agente a interpretar descrições abreviadas:

| Abreviação | Significado |
|---|---|
| `DT.PO` | Detergente em Pó |
| `DT.LIQ` | Detergente Líquido |
| `SCO` | Saco (embalagem bag/saco) |
| `CART` | Caixa cartonada |
| `BAG` | Embalagem bag (refil) |
| `DOY` | Embalagem doy-pack |
| `RP` | Refil/Recarga |
| `LAV.PERF` / `L.PERFEITA` | Lavagem Perfeita |
| `FDO` | Fardo |
| `CXT` | Caixa |
| `UND` | Unidade |
| `PCT` | Pacote |
| `CXA` | Caixa |
| `IOG` | Iogurte |
| `BEB LAC` | Bebida Láctea |
| `REQ` | Requeijão |
| `MRG` | Morango |

---

## Notas para o Agente

1. **Sempre buscar nos últimos 90 dias por padrão** para pesquisas de compras recentes.
2. **Nunca concluir "produto não foi comprado"** só porque não está na `precificacao` — verificar `movimentacao_estoque` e `notas_fiscais` antes.
3. **Ao encontrar um produto com EAN divergente**, registrar a confiança: confirmado (descrição clara) vs hipótese (EAN errado + descrição ambígua).
4. **Para identificar o fornecedor** de uma NF encontrada na `precificacao` ou `movimentacao_estoque`, usar a `chave_nfe` para cruzar com `contas_a_pagar.documento`.
5. **O campo `codigo` em `movimentacao_estoque`** corresponde ao `sku` do `catalogo`, não ao EAN.
