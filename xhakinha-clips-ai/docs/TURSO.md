# Turso como banco canônico — Xhakinha Clips AI

## Regra de ouro
Existe UM banco de estado: o Turso. Render (painel/API) e Kaggle (cérebro) leem e gravam nele. O Drive guarda mídia e backups
(exportações). O Worker+D1 da Cloudflare continua só coordenando (lock do cérebro, leases, fencing, executores) — não guarda a fila.

## Estado de cada parte
- FEITO e testado com FAKE libsql (SIMULADO): adaptador, Store, fila atômica, cota atômica, export/backup, import seguro, 166+18 testes.
- DEPENDE DE CREDENCIAIS (não verificado): comportamento do Turso REAL -> `python scripts/turso_check.py`.
- NÃO verificado: wheel do `libsql` na versão de Python do Render e no Termux; latência real.
- PENDENTE: nada foi migrado/implantado por mim (sem acesso ao seu Turso, Render ou Kaggle).

## Passo a passo
1. Turso (celular, navegador): abra o banco existente. Gere um token de **leitura e escrita** (o atual do Kaggle é read-only e não serve).
   Anote a "Database URL" (`libsql://...turso.io`). Não cole token em chat/código.
2. Render > seu serviço > Environment: defina `TURSO_DATABASE_URL` e `TURSO_AUTH_TOKEN`. Não mexa em `SECRET_KEY`.
   (Não defina `XH_STATE_BACKEND`; "auto" usa Turso quando as duas variáveis existem.) Ainda NÃO faça deploy.
3. Kaggle > notebook > Add-ons > Secrets: crie `TURSO_DATABASE_URL` e `TURSO_AUTH_TOKEN` (mesmos valores), marque Attached.
4. Atualize o código (ZIP novo no seu repositório, branch separada se quiser) e rode no Kaggle a célula nova **VALIDAÇÃO**
   (instala `libsql`, roda `turso_check.py` e `turso_migrate.py check`). Não rode a célula do runner.
   `turso_check.py` tem que terminar com "TUDO OK". Se falhar, envie a saída (não contém segredos).
5. Ainda no Kaggle (nova célula) crie o esquema, uma única vez:
   `!python /kaggle/working/app/scripts/turso_migrate.py init`
6. (Opcional) Dados antigos: baixe um backup .db (Render: botão de backup do painel; ou snapshot do Drive descompactado) e rode
   `python scripts/turso_migrate.py import ARQUIVO.db` — só funciona com o Turso vazio e confere contagens.
7. Faça o deploy do Render (ele já sobe usando Turso). Depois rode o runner do Kaggle.
8. Rollback: apague `TURSO_*` (ou `XH_STATE_BACKEND=sqlite`) e volta ao SQLite local; o Turso não é apagado.

## Fluxo de dados
Painel (Render) --grava/lê--> Turso <--grava/lê-- Cérebro (Kaggle). Cloudflare Worker+D1: lock/lease/fencing do cérebro.
Mídia: Kaggle -> Drive (originais, clipes). Backup: a cada intervalo, Kaggle exporta o Turso para .sqlite.gz no Drive
(db/snapshots/) — nunca restaurado automaticamente no modo Turso.

## Limites conhecidos
- Sem transação interativa no Turso: fila e cota usam comandos únicos atômicos. `Store.tx()` só existe no SQLite local.
- Exportação para backup é cópia lógica tabela a tabela, não instantâneo transacional entre tabelas.
- Escritas não são repetidas após erro de rede (evita efeito duplicado); leituras reconectam uma vez.
- Cada consulta é uma ida à rede: mais lenta que SQLite local (o `turso_check` mostra o tempo).
