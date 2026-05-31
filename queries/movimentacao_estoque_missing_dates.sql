SELECT DISTINCT cs."date"
FROM company_schedule cs
LEFT JOIN movimentacao_estoque me ON DATE(me.datahora) = cs."date"
WHERE me.datahora IS NULL
   OR cs."date" >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY cs."date";

