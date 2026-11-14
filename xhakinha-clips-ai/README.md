# XHAKINHA CLIPS AI

Fábrica contínua de clipes verticais (9:16) a partir de **conteúdo próprio ou autorizado**: monitora fontes
(YouTube, Twitch, Instagram próprio, URLs, upload), analisa áudio/vídeo/texto localmente, escolhe os melhores
momentos, renderiza com FFmpeg, agenda por plataforma e publica no TikTok e Instagram pelas APIs oficiais.
Custo zero: Python + SQLite + FFmpeg + modelos locais, hospedável na Oracle Cloud Always Free.

> Instalação pelo celular Android: veja **TUTORIAL-INSTALACAO.txt**. Estado real do projeto: **STATUS-DO-PROJETO.txt**.
> Testes: **RELATORIO-DE-TESTES.txt**.

## Arquitetura
```
xhakinha/
  config.py      configuração (SECRET_KEY obrigatória/validada, redação de segredos)
  db.py          SQLite WAL, segredos cifrados (Fernet), configurações editáveis
  retry.py       erros temporários x permanentes, backoff exponencial + jitter
  queue.py       fila persistente (recuperação após queda, fila de falhas, dedupe)
  storage.py     produção x estoque permanente, integridade, backup, Cleaner (única via de exclusão)
  sources/       YouTube (RSS + cota), Twitch, Instagram próprio, URL/yt-dlp, quota
  analysis.py    áudio (riso/grito/surpresa), visão (rosto/movimento), transcrição local opcional, nota 0-100
  editor.py      9:16, rastreio de rosto suave, loudnorm, marca d'água @ por plataforma
  captions.py    descrição + hashtags (texto da publicação; nunca no vídeo)
  publish/       TikTok (Content Posting API) e Instagram (Graph API, URL pública assinada)
  scheduler.py   metas diárias (fuso), horários inteligentes, estoque, acervo antigo
  learning.py    aprendizado com métricas reais (inativo sem amostras suficientes)
  pipeline.py    monitor → adquirir → processar → publicar → métricas
  engine.py      workers com concorrência autoajustável + agendador
  realtest.py    botão TESTAR PROCESSAMENTO REAL
  web/           painel responsivo + PWA (service worker, manifest, ícones)
```

## Rodar
```bash
python3 scripts/setup.py                       # cria .env com SECRET_KEY forte
docker compose up -d --build                   # app + HTTPS automático (Caddy)
docker compose exec app python -m xhakinha.cli create-admin
```
Sem Docker (desenvolvimento): `pip install -r requirements-dev.txt && python -m xhakinha.main`.

## Testes
```bash
pip install -r requirements-dev.txt && pytest tests -q
```
Testes marcados `SIM` usam servidor/ferramenta **simulados** (não provam integração real).

## Regras de segurança do projeto
Sem segredo padrão; senhas com scrypt; sessão JWT em cookie HttpOnly; CSRF por cabeçalho; CSP; upload validado por ffprobe;
yt-dlp só com URL https em lista permitida e sem shell; tokens de plataformas cifrados em repouso; logs sem segredos.
Nada é apagado fora do `Cleaner`; originais só após processamento concluído + carência; clipes só após publicação confirmada em todas as redes.

## Multi-cloud (Kaggle + Drive + Cloudflare)
Veja TUTORIAL-ANDROID-MULTICLOUD.txt, KAGGLE_SETUP.txt e docs/ARQUITETURA-MULTICLOUD.md.
