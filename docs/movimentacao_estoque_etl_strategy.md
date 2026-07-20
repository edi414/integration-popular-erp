# Estratégia de carga da `movimentacao_estoque`

Este documento explica:
1. A chave única atual e como ela falha quando o G3 recria itens de uma nota.
2. A estratégia "delete + insert por documento" — solução proposta mas **não implementada**.
3. Como diagnosticar fantasmas residuais sem rodar o fix.

---

## 1. Chave única atual

```sql
CREATE UNIQUE INDEX movimentacao_estoque_uk
ON public.movimentacao_estoque (tipodocumento, id_documento, currenttimemillis)
NULLS NOT DISTINCT;
```

Origem de cada componente:

| Coluna | Unico (tipodoc 1,2,3) | G3 venda (tipodoc 65) | G3 entrada (tipodoc 55) |
|---|---|---|---|
| `tipodocumento` | constante por sistema | constante | constante |
| `id_documento` | `operacao.id` | `ecf_venda_cabecalho.id` | `notas_entrada.id` |
| `currenttimemillis` | timestamp Unix do legado | `id_ecf_caixa * 1e8 + id_detalhe` | `notas_entrada_produto.id` |

`tipo_movimentacao` **não** entra na chave: quando uma nota vai de `'E'` para `'C'` (excluída no G3), o UPSERT atualiza a mesma linha — "último status vence".

---

## 2. O caso fantasma — quando a chave falha

O G3 às vezes deleta e reinsere os itens de uma nota em `notas_entrada_produto`. Os novos `nep.id` (que viram `currenttimemillis` no DW) são diferentes dos antigos. Resultado:

- ETL roda dia D: insere linhas com `currenttimemillis = 6075, 6076, 6077`.
- G3 recria itens da nota → novos `nep.id = 6856, 6857, 6858`.
- ETL roda dia D+N: insere as novas linhas. As antigas **permanecem** porque a chave não colide.

Entre 01/04/2026 e 07/05/2026 isso aconteceu **11 vezes** (3 documentos: 196, 293, 320). Foram deletadas manualmente.

### Por que outras chaves não resolvem

| Chave alternativa | Colisões em ~2,98M linhas | Por quê |
|---|---|---|
| `(tipodocumento, id_documento)` | 2.760.932 | Cada nota tem N produtos |
| `(tipodocumento, id_documento, codigo)` | 777.916 | Mesmo produto pode aparecer várias vezes na mesma nota |
| `(tipodocumento, id_documento, currenttimemillis)` ← atual | 0 | Identifica o item |
| `(tipodocumento, id_documento, codigo, datahora, ...)` | depende | `datahora` é instável (`hora_chegada` é preenchida tardiamente) |

Não existe combinação de colunas estáveis que identifique um item e seja resiliente à recriação de `nep.id` — isso é um problema **fundamental** do modelo de dados, não da escolha de chave.

---

## 3. Estratégia "delete + insert por documento" (NÃO implementada)

A única solução estrutural ao problema fantasma é trocar a semântica de carga:

> Para cada `(tipodocumento, id_documento)` no batch atual, deletar tudo que existe no DW antes de inserir as linhas novas.

### Implementação proposta

Em [services/movimentacao_estoque.py](../services/movimentacao_estoque.py), adicionar método antes do UPSERT:

```python
def _clean_documents_in_batch(self, df: pd.DataFrame) -> None:
    """Remove linhas existentes para os (tipodocumento, id_documento) presentes no batch.
    Garante que itens deletados/recriados na origem sumam do DW."""
    pairs = df[['tipodocumento', 'id_documento']].dropna().drop_duplicates()
    if pairs.empty:
        return
    tuples = [(int(r.tipodocumento), int(r.id_documento)) for r in pairs.itertuples()]
    self.target_connection.connect()
    try:
        with self.target_connection.connection.cursor() as cur:
            cur.execute(
                "DELETE FROM public.movimentacao_estoque "
                "WHERE (tipodocumento, id_documento) IN %s",
                (tuple(tuples),)
            )
            deleted = cur.rowcount
            self.target_connection.connection.commit()
            self.logger.info(f"Pre-clean: {deleted} linhas removidas para {len(tuples)} documentos.")
    finally:
        self.target_connection.disconnect()
```

E em `load_data()`:
```python
self._clean_documents_in_batch(df)
self.target_connection.upsert(...)  # mantém como rede de segurança
```

### Trade-offs

**Prós:**
- Itens removidos na origem somem do DW automaticamente.
- Itens recriados com novo `nep.id` substituem os antigos sem deixar fantasmas.
- Idempotente: rodar o ETL N vezes converge para o estado da origem.

**Contras:**
- DELETE escala com o tamanho do batch (uma data Unico pode ter milhares de documentos por batch).
- Linhas com `id_documento IS NULL` (366 Unico pré-backfill + 52 ajustes internos) **não** são afetadas pela limpeza — continuam confiando só no UPSERT. São registros históricos imutáveis então não há risco prático.
- Perda do registro histórico: se um item válido foi deletado na origem por engano, o ETL "corrige" essa perda no DW. Considerado comportamento desejado — origem é a fonte da verdade.

### Decisão atual

Não foi implementado. O usuário optou por aceitar o risco e detectar fantasmas via diagnóstico periódico (seção 4). Implementar quando o número de fantasmas residuais justificar.

---

## 4. Como diagnosticar fantasmas residuais

Fantasma = linha sem `chave_nfe` cuja `(tipodocumento, id_documento)` tem **outras linhas com `chave_nfe` preenchida**. A linha antiga ficou órfã quando a origem recriou `currenttimemillis`.

### Query de diagnóstico (G3 — tipodoc=55)

```sql
-- Fantasmas G3: entradas sem chave que TÊM irmãos com chave (mesmo id_documento)
SELECT m.id, m.id_documento, m.documento, m.codigo,
       m.datahora, m.qtd, m.valortotal, m.currenttimemillis,
       m.chave_nfe
FROM public.movimentacao_estoque m
WHERE m.tipodocumento = 55
  AND m.tipo_movimentacao = 'E'
  AND m.chave_nfe IS NULL
  AND EXISTS (
      SELECT 1 FROM public.movimentacao_estoque m2
      WHERE m2.tipodocumento = m.tipodocumento
        AND m2.id_documento = m.id_documento
        AND m2.chave_nfe IS NOT NULL
  )
ORDER BY m.datahora DESC;
```

### Diferenciar fantasmas de NULLs estruturalmente corretos

Nem toda linha com `chave_nfe IS NULL` é fantasma. Use esta classificação:

```sql
SELECT
  CASE
    WHEN tipodocumento IN (1,2,3) THEN 'Unico'
    WHEN tipodocumento IN (55,65) THEN 'G3'
  END AS sistema,
  tipodocumento,
  CASE
    WHEN tipodocumento = 3 THEN 'CT-e: estrutural (Unico não armazena chave)'
    WHEN tipodocumento = 2 AND id_documento IS NULL THEN 'Pré-backfill: irrecuperável'
    WHEN EXISTS (
      SELECT 1 FROM movimentacao_estoque m2
      WHERE m2.tipodocumento = m.tipodocumento
        AND m2.id_documento = m.id_documento
        AND m2.chave_nfe IS NOT NULL
    ) THEN 'FANTASMA: deletável'
    ELSE 'A investigar'
  END AS classificacao,
  COUNT(*) AS total
FROM movimentacao_estoque m
WHERE tipo_movimentacao = 'E'
  AND chave_nfe IS NULL
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
```

Estado em **2026-05-08** (após dedup):

| sistema | tipodoc | classificação | linhas |
|---|---|---|---|
| Unico | 2 | Pré-backfill: irrecuperável | 366 |
| Unico | 3 | CT-e: estrutural | 912 |
| G3 | 55 | FANTASMA: deletável | 0 |

### Comando de limpeza cirúrgica (quando aparecerem novos fantasmas)

```sql
DELETE FROM public.movimentacao_estoque
WHERE tipodocumento = 55
  AND tipo_movimentacao = 'E'
  AND chave_nfe IS NULL
  AND EXISTS (
      SELECT 1 FROM public.movimentacao_estoque m2
      WHERE m2.tipodocumento = movimentacao_estoque.tipodocumento
        AND m2.id_documento = movimentacao_estoque.id_documento
        AND m2.chave_nfe IS NOT NULL
  );
```

Também é possível rodar via script:
```
python scripts/audit_movimentacao_estoque.py --audit
```
A Fase 5 (duplicatas) e Fase 3 (chave_nfe) reportam grupos suspeitos.

---

## 5. Cadência de monitoramento sugerida

- **Semanal:** rodar a query de diagnóstico G3. Se aparecer fantasma novo, deletar com o comando da seção 4.
- **Mensal:** revisar a contagem de NULLs por classificação. Se o número de fantasmas ultrapassar a casa das centenas, reconsiderar implementar a estratégia delete-and-insert.
- **A cada release de schema do G3:** confirmar que `notas_entrada_produto` ainda usa `id` autoincremento. Mudança nesse comportamento muda o vetor de fantasma.
