# Arquitetura multi-cloud

## Visão geral

```text
GitHub (código, testes e Actions)
        |
        v
Kaggle (processamento e Brain)
        | HTTPS autenticado
        v
Cloudflare Worker + D1
(coordenação, fila, leases, locks e autorização)
        |
        +-------------------+
        |                   |
        v                   v
Turso / libSQL        Backblaze B2
(estado canônico)     (arquivos e snapshots)
```

## Responsabilidades

- **GitHub:** código-fonte, testes, versionamento e automação por Actions.
- **Kaggle:** ambiente de processamento e execução do Brain.
- **Cloudflare Worker + D1:** coordenação, fila, leases, locks, fencing, cotas e autorização do gatilho Kaggle.
- **Turso/libSQL:** banco canônico do estado compartilhado.
- **Backblaze B2:** armazenamento de objetos do fluxo de produção.

O Google Drive está desativado como backend de armazenamento neste fluxo. A configuração de produção utiliza `XH_STORAGE_BACKEND=b2` e `XH_STATE_BACKEND=turso`.

## Módulos

Em `xhakinha/cloud/`:

- `coordinator.py`: fila, leases, fencing, locks, breaker e reconciliação.
- `quota.py`: cotas, reservas, breaker e rate limit.
- `b2_storage.py`: integração com Backblaze B2 via API compatível com S3.
- `drive_storage.py`: abstrações e componentes reutilizados pelo backend B2 e implementações auxiliares/legadas.
- `snapshots.py` e `checkpoint.py`: snapshots e checkpoints.
- `executor.py` e `handlers.py`: execução de tarefas e processamento retomável.
- `brain.py`: coordenação do processamento principal.
- `runner.py` e `kaggle_runner.py`: inicialização dos ambientes.
- `healthcheck.py`, `api.py` e `client.py`: saúde, API e comunicação com o coordenador.

Em `cloudflare/`, o Worker implementa os endpoints de coordenação e utiliza D1 para persistir o estado operacional. O esquema SQL inclui `kaggle_launch_enabled`, desativado por padrão.

## Garantias e controles

1. Uma tarefa possui um único executor válido, controlado por atualização atômica.
2. O fencing impede confirmações de executores que perderam a autorização.
3. Os arquivos de produção são armazenados no B2, com verificações de integridade.
4. O Turso é a fonte canônica do estado compartilhado quando `XH_STATE_BACKEND=turso`.
5. Snapshots são mecanismos de recuperação e não substituem o estado mais recente do Turso.
6. Falhas de provedores são isoladas por circuit breakers.
7. O gatilho Kaggle depende de autorização explícita no coordenador e permanece desativado por padrão.

## Distribuição

- Processamento pesado: Kaggle/GPU ou executores auxiliares compatíveis.
- Tarefas leves: executores disponíveis, conforme a política de distribuição.
- Coordenação: Cloudflare Worker + D1.
- Estado canônico: Turso.
- Arquivos e snapshots: Backblaze B2.

O processamento local permanece como alternativa de contingência conforme a configuração do runner.

## Observação operacional

A existência do workflow de GitHub Actions não significa que o processamento Kaggle esteja ativo. A execução depende das configurações, dos segredos e da autorização remota. `kaggle_launch_enabled` deve permanecer `false` até a conclusão dos testes de integração e a autorização operacional.
