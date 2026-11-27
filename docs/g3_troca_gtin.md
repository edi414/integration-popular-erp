# Troca de código de barras (GTIN/EAN) no G3 via banco

Especificação para automatizar a troca do código de barras de um produto direto no banco do G3. O discovery foi feito em 2026-11-25, somente com leitura, no banco `gtech-gestao`.

## Contexto

- O preço já é alterado direto no banco (ver `update_price_surgical` em `app/worker/repositories/mariadb_repository.py`).
- O G3 (Davi) confirmou que o código de barras fica em `produto.GTIN`.
- Este documento define **quais tabelas alterar**, **quais validações são obrigatórias** e **o que ainda está pendente**.

## Ambiente

| Item | Valor |
|---|---|
| Servidor | `servidornovo` (Windows), via Tailscale na conta do mercado |
| Banco | MariaDB 10.1.41, schema `gtech-gestao` (488 tabelas) |
| Usuário | `popular` |
| Permissões | `SELECT, UPDATE, PROCESS, ALTER, SHOW DATABASES, EXECUTE, SHOW VIEW`. **Sem `INSERT` e sem `DELETE`** |
| Credenciais | variáveis `G3_HOST`, `G3_PORT`, `G3_DB`, `G3_USER`, `G3_PASSWORD` |

## Discovery

### Colunas de código na tabela `produto`

| Coluna | Tipo | Restrição | Uso |
|---|---|---|---|
| `GTIN` | varchar(16) | **UNIQUE**, NOT NULL | Código principal. 98% das vendas usam esse código |
| `EAN` | varchar(20) | NOT NULL, sem índice | Código alternativo. Preenchido em 727 produtos e diferente do `GTIN` em 97. Há vendas registradas com o `EAN`, então o caixa provavelmente aceita esse código |
| `GTIN_2` | varchar(60) | índice não único | Segundo código. Só 2 produtos têm, e não há venda registrada com ele |

### Impacto em outras tabelas

- **Não há triggers** no banco.
- **Não há chaves estrangeiras** apontando para `produto`.
- As 488 tabelas foram varridas **pelo conteúdo**: os valores de toda coluna de texto ou número foram comparados com os `GTIN`, `EAN` e `GTIN_2` cadastrados.
- O código de barras só aparece nas tabelas abaixo. Todas se ligam ao produto pelo ID, não pelo código.

| Tabela | Coluna(s) com código | Ligação com o produto | Atualizar na troca? |
|---|---|---|---|
| `ecf_venda_detalhe` | `GTIN` | `ID_ECF_PRODUTO` = `produto.ID` | **Não**: histórico de vendas |
| `ecf_pre_venda_detalhe` | `gtin_produto` | `id_produto` | **Não**: histórico |
| `nfe_produtos` | `codigo_produto`, `EAN` | `id_produto` | **Não**: nota fiscal já emitida |
| `notas_entrada_produto` | `codigo_produto_cad`, `cean_produto_ne`, `codigo_produto_ne` | `id_produto` | **Não**: histórico, vem do XML do fornecedor |
| `preco_produto_fornecedor` | `cean_no_fornecedor`, `codigo_no_fornecedor` | `id_produto` | **Não**: em 1.956 dos 10.562 vínculos esse código já difere do nosso `GTIN`, e a entrada de notas funciona |

Evidência de que o próprio G3 não reescreve o histórico quando o código muda:
- As vendas guardam o código que valia no momento da venda.
- Exemplo: um produto vendido como `7891048040065` hoje tem `GTIN` `7896327512233`. A venda segue ligada a ele pelo `ID`.

Também não precisam ser alteradas:
- tabelas com coluna de código que estão vazias: `promocao_produto`, `lote`, `produtos_etiqueta_gondola_impressao_automatica`, `balanco_detalhe`, `inventario_detalhe`, `pedido_cotacao_detalhe` e outras;
- as views (`view_produtos`, `view_busca_preco_filial_1`), que leem `produto.GTIN` direto e se atualizam sozinhas.

### Sincronização com os caixas (ponto em aberto)

- Cada caixa (`ecf_caixa`: CAIXA001 a CAIXA004) tem um banco local próprio, `gtech-nfce`, na máquina do caixa.
- O servidor envia as alterações por uma fila, a tabela `pendentes_de_sincronizacao`, com estas colunas:
  - `id_referencia` = `produto.ID`;
  - `id_caixa` = `ecf_caixa.ID`;
  - `tipo_sincronizacao` = `1` para produto;
  - `perfil` = `0` ou `1`.
- O histórico dos envios fica em `log_sinc`.
- Os produtos que estão na fila são os editados pela tela do G3. Isso indica que **é a tela que coloca o produto na fila**, e um `UPDATE` direto não colocaria.
- O usuário `popular` não tem `INSERT`, então não consegue colocar o produto na fila.

## Regras definidas

| # | Regra |
|---|---|
| R1 | Alterar **somente a tabela `produto`**. Nenhuma outra tabela é atualizada |
| R2 | Atualizar `GTIN` **e** `EAN` com o código novo. `GTIN_2` não é alterado |
| R3 | O código novo **precisa ser um EAN/GTIN válido**. Se não for, a troca inteira é rejeitada e nada é gravado |
| R4 | **Produtos de balança ficam fora** do fluxo (`BALANCA = 1`) |
| R5 | Preencher `DATA_EDICAO = NOW()` e `ID_USUARIO_EDICAO = 5` (EDIVALDO) |
| R6 | A gravação deve afetar **exatamente 1 linha**; caso contrário, `ROLLBACK` |

### Como identificar produto de balança

- Use `produto.BALANCA = 1`: são 235 produtos ativos, 230 vendidos por KG.
- O `GTIN` deles é o código PLU, com até 6 dígitos.
- `BALANCA_INTEGRADA` está zerada em todos os produtos; **não usar**.
- Cerca de 500 produtos com `BALANCA = 0` também têm código curto (código interno). A regra R3 já os exclui, porque código curto nunca é um EAN válido.

## Validações (em ordem)

### 1. Formato do código novo (obrigatória, antes de qualquer acesso de escrita)

Usar a lib [`python-stdnum`](https://pypi.org/project/python-stdnum/):

```python
from stdnum import ean

novo = novo.strip()
if not ean.is_valid(novo):
    raise ValueError(f"Código {novo!r} não é um EAN/GTIN válido")
```

`ean.is_valid` confere três coisas:
- tamanho de 8, 12, 13 ou 14 dígitos;
- só números;
- dígito verificador.

| Entrada | Resultado |
|---|---|
| `7896327512233` | válido |
| `7896327512234` (dígito verificador errado) | inválido |
| `78946347` (EAN-8) | válido |
| `070330717527` (UPC-A, 12 dígitos) | válido |
| `352273` (PLU de balança) | inválido |
| `O879998949420` (letra O no lugar de zero) | inválido |

Retrato da base atual, sem contar balança:
- 21.500 produtos têm `GTIN` válido;
- 652 têm `GTIN` inválido: códigos internos curtos, UPC de 11 dígitos sem o zero da frente, e dígito verificador errado.

### 2. Pré-checagens no banco (somente leitura)

```sql
-- produto alvo
SELECT ID, GTIN, EAN, BALANCA, EXCLUIDO
FROM produto
WHERE ID = %(id_produto)s;
```
Rejeitar a troca se:
- o produto não existir;
- `BALANCA = 1`;
- `EXCLUIDO = 1`;
- `GTIN` for igual ao código novo (não há nada a trocar).

```sql
-- código novo já usado por outro produto (GTIN, EAN ou GTIN_2)
SELECT ID, GTIN, EAN, GTIN_2
FROM produto
WHERE ID <> %(id_produto)s
  AND (GTIN = %(novo)s OR EAN = %(novo)s OR GTIN_2 = %(novo)s);
```
Se retornar alguma linha, rejeitar a troca.
- No `GTIN` o banco já recusaria, pela restrição `UNIQUE`.
- No `EAN` e no `GTIN_2` não há trava no banco, então a checagem precisa ser feita aqui.

### 3. Gravação

```sql
START TRANSACTION;

UPDATE produto
SET GTIN = %(novo)s,
    EAN = %(novo)s,
    DATA_EDICAO = NOW(),
    ID_USUARIO_EDICAO = 5
WHERE ID = %(id_produto)s
  AND GTIN = %(gtin_atual)s   -- trava otimista: não sobrescreve se mudou desde a pré-checagem
  AND BALANCA = 0;

-- rowcount == 1 -> COMMIT ; senão -> ROLLBACK
```

Registrar em log o `id_produto`, o `GTIN` e o `EAN` antigos, o código novo e o resultado (`COMMIT` ou `ROLLBACK` com motivo). O G3 não guarda histórico de alterações de produto (`historico_produto` está vazia), então esse log é a única trilha de auditoria.

## Implementação (worker)

A troca entra como novo tipo de evento na mesma fila Redis do preço (`price_updates`), consumida por `app/worker`.

**Evento** (enviado pelo n8n):
```json
{ "type": "update_gtin", "gtin_atual": "7896327512233", "gtin_novo": "7891048040065" }
```

**Fluxo:** `redis_consumer` → `handlers/gtin_update_handler.py` → `services/gtin_service.py` (regras e validações) → `repositories/mariadb_repository.py` (`get_produto_by_gtin`, `find_produtos_com_codigo`, `update_gtin_surgical`).

**Retorno no webhook** (mesmo `WEBHOOK_URL` do preço): o evento original com estes campos a mais.

| Campo | Valores |
|---|---|
| `status` | `sucesso`, `rejeitado` (uma regra barrou, nada foi gravado) ou `erro` (falha de banco ou `ROLLBACK`) |
| `motivo` | texto explicando o resultado |
| `id_produto` | ID do produto no G3, quando encontrado |
| `ean_anterior` | valor do `EAN` antes da troca, para auditoria e reversão |

**Configuração:** `G3_ID_USUARIO_EDICAO` (padrão `5`).

**Testes:** `uv run pytest`, em `tests/test_gtin_service.py`.

## Pendências com o G3 (Eduardo)

1. **Bloqueante:** basta dar `UPDATE` em `produto.GTIN`/`EAN` para o caixa reconhecer o código novo, ou é preciso colocar o produto em `pendentes_de_sincronizacao`? Se for preciso, o G3 libera `INSERT` nessa tabela para o usuário `popular`?
2. O caixa busca o produto também por `EAN` e `GTIN_2`?
3. `preco_produto_fornecedor.cean_no_fornecedor` influencia algo além do vínculo na entrada de nota?

## Teste antes de automatizar

1. Escolher 1 produto com pouca venda, `BALANCA = 0` e `GTIN` válido.
2. Rodar o fluxo com um código novo válido.
3. Conferir:
   - o código novo aparece na tela de produto do G3;
   - o produto entrou ou não em `pendentes_de_sincronizacao`;
   - o código novo passa no caixa e o antigo não passa mais.
4. Se o código novo não chegar ao caixa sem a fila, resolver a pendência 1 antes de seguir.
