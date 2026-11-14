# Arquitetura multi-cloud

```
 GitHub (código, testes, gatilho leve)          Cloudflare Worker + D1  ← coordenador sempre ligado (painel, fila, leases, cotas)
        │ kaggle kernels push                         ▲  HTTPS (Bearer + X-Requested-With)
        ▼                                             │
 Kaggle sessão ── cérebro (lock "brain" + fencing) ───┤── Colab/GitHub/Kaggle auxiliares (executores de tarefas)
        │  Engine fase 1: monitor, agenda, publicação │
        ▼                                             ▼
                    Google Drive: originals/ clips/ db/snapshots/ checkpoints/
```
Módulos (`xhakinha/cloud/`): `coordinator.py` (fila, leases, fencing, locks, breaker, reconciliação), `quota.py` (decisão de cota,
reserva, breaker, rate limit), `drive_storage.py` (Drive real + pasta local), `snapshots.py`, `checkpoint.py`, `executor.py`,
`handlers.py` (process_content retomável), `brain.py`, `runner.py`, `kaggle_runner.py`, `healthcheck.py`, `api.py`, `client.py`.
Worker JS: `cloudflare/` (mesmo esquema SQL e mesmas regras; testado em SQLite via node:sqlite, não em D1 real).

Garantias: (1) uma tarefa → um dono (UPDATE atômico); (2) executor antigo não confirma nada (fence); (3) arquivos só no Drive com
MD5/sha256 e sem sobrescrita; (4) estado reconstruível do último snapshot verificado; (5) falha de um provedor abre só o breaker dele.
Distribuição: pesado (análise/render) → `prefer` kaggle-gpu/colab, com espera suave de 10 min antes de liberar a outro; leve → github;
painel/coordenação → Cloudflare; arquivos → Drive. O cérebro cai no processamento local se nenhum executor assumir (remote_wait_s).
