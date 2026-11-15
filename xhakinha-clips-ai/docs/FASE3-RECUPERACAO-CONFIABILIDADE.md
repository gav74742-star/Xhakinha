# Fase 3 — Recuperação e confiabilidade entre sessões

## Problemas corrigidos (diagnóstico → correção)
| Problema | Correção | Arquivos |
|---|---|---|
| Clipe local sumia entre sessões; publicação falhava como "arquivo ausente" | Coluna `clips.drive_path`/`contents.drive_path`; `Brain.rehydrate_clip` (baixa, confere sha256, mantém id) chamado na partida (próximas 6 h), a cada 5 min e sob demanda em `Pipeline.prepare_platform_file` via `clip_fetcher` | `xhakinha/db.py`, `xhakinha/pipeline.py`, `xhakinha/cloud/brain.py` |
| Cópias duplicadas no Drive | UM caminho por arquivo (`drive_path`); clipe vindo de executor remoto usa o caminho do executor; `sync_files` só envia o que não tem `drive_path`; original usa `drive_path` em vez de re-hashear | `brain.py` |
| `ingest_paused` não era lido | Lido por `h_monitor`, `h_archive_scan` (adia), `h_acquire` (adia, sem consumir tentativa), `Engine.tick` (não agenda monitor/acervo). Publicação e estoque seguem. Histerese: pausa < `drive_min_free_gb` (1,5), retoma ≥ `drive_resume_free_gb` (2,5), só se a pausa foi automática (`drive:`); pausa manual permanece | `pipeline.py`, `engine.py`, `brain.check_storage` |
| Duplicação de post entre sessões (snapshot defasado) | Diário imutável `logs/pubjournal/` no Drive, gravado a cada mudança de estado; reconciliado na partida. Falha ao gravar o diário (antes de publicar) adia o passo; depois de publicada, vai para backlog | `xhakinha/cloud/journal.py`, `brain._install_journal` |
| TikTok: upload podia ser repetido após queda | `upload_started` gravado antes do PUT; se a sessão morrer, só acompanha o status (não reenvia). >1 h em processamento ⇒ erro permanente "verifique no app" (conservador) | `xhakinha/publish/tiktok.py` |
| Credenciais em snapshot | `upload_url` assinada removida da CÓPIA do snapshot (com VACUUM/secure_delete); diário usa lista branca de campos; tokens continuam criptografados (Fernet) | `snapshots.py`, `brain._sanitize`, `journal.py` |
| Bug achado nos testes | Dois snapshots no mesmo segundo colidiam (nome igual) → agora com milissegundos | `snapshots.py` |

## Decisões e limites (leia)
- Erro temporário (`TransientError`) ⇒ nova tentativa com backoff até `max_attempts` do job (12 pelo `Engine.tick`); esgotou ⇒ job `dead` + publicação `failed` + `needs_admin=1`.
  Erro permanente ⇒ direto para `failed`/`needs_admin`, sem retentativas.
- Se o resultado de um upload TikTok for incerto (>1 h), o sistema NÃO reenvia: pede verificação manual. Prefere não publicar a duplicar.
- Se o clipe nunca chegou ao Drive antes da sessão morrer (`no_drive_copy`), ele é reportado e a publicação vira `failed`; reprocessar o conteúdo é manual.
- O comportamento real do TikTok quando o upload é interrompido NÃO foi verificado (só o servidor falso).
- Instagram: ainda sem URL pública (fora do escopo desta etapa). Nada de publicação real foi implementado.
- OAuth por código de dispositivo para TikTok/Instagram: NÃO implementado. O fluxo de dispositivo do Google (Drive) já existe em `scripts/drive_authorize.py`; para TikTok/Instagram falta confirmar suporte oficial.
