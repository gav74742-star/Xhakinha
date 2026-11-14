# Pesquisa multi-cloud (limites gratuitos) — data da pesquisa: 14/11/2026

Metodologia: o sandbox bloqueia a maioria dos domínios oficiais (WebFetch → URL_NOT_ON_ALLOWLIST).
Consegui ler diretamente: docs.github.com (billing), developers.cloudflare.com (limites Workers, preços D1).
Os demais valores vêm de páginas de terceiros ou de conhecimento prévio. Cada linha traz o **grau de confiança**.
Os limites ficam **configuráveis** em `cloud/quota.py` (PROVIDER_DEFAULTS) e na tabela `providers`;
nenhum valor abaixo é "garantido" — o gerenciador usa margens e marca incerteza.

| Serviço | Limite | Fonte / confiança |
|---|---|---|
| Cloudflare Workers Free | 100.000 req/dia (zera 00:00 UTC), 10 ms CPU/req, 50 subrequests/req, 128 MB, 5 Cron Triggers, 100 Workers | developers.cloudflare.com (lido) — ALTA |
| Cloudflare D1 Free | 5 M linhas lidas/dia, 100 k escritas/dia, 5 GB total, 10 bancos | developers.cloudflare.com/d1/platform/pricing (lido) — ALTA |
| GitHub Actions | Repositório público: runners padrão gratuitos. Privado: franquia mensal por plano (valor não exibido na página lida). Limites de job (6 h) e agendamento mínimo (5 min, cron pode atrasar; workflows agendados em repo público são desativados após 60 dias sem atividade) | docs.github.com (lido: só a parte "público é grátis"); resto = conhecimento prévio — MÉDIA |
| GitHub — ToS de Actions | Uso deve ser ligado a produção/teste/deploy do projeto do repositório; não usar como "computação grátis" genérica | conhecimento prévio — MÉDIA. **Decisão:** Actions só para testes, "checagem leve" do coordenador e disparo de `kaggle kernels push`; nada de processamento de vídeo |
| Kaggle Notebooks | ~12 h/sessão (CPU/GPU), GPU ~30 h/semana (P100 ou T4×2), RAM ~32 GB, `/kaggle/working` ~20 GB e persiste entre execuções do mesmo notebook, saída de rede liberada (internet exige verificação de telefone), sem portas de entrada | página de terceiros — MÉDIA. Pode mudar |
| Kaggle API | `kaggle kernels push/status/output`, autenticação por token (KAGGLE_USERNAME/KAGGLE_KEY ou kaggle.json) | conhecimento prévio — MÉDIA (README oficial bloqueado) |
| Google Colab gratuito | runtime ≤ 12 h, "recursos não garantidos nem ilimitados", ociosidade derruba sessão, ~12 GB RAM, GPU sujeita a disponibilidade, sem execução agendada oficial | help.lafayette.edu espelhando o FAQ — MÉDIA. **Decisão:** Colab = executor manual/oportunista (você abre o notebook); nunca contado como capacidade garantida |
| Google OAuth | App em "Testing": refresh token expira em 7 dias, até 100 usuários de teste. "Published/In production": token dura até revogação | blog de terceiros — MÉDIA. **Decisão:** publicar o app OAuth pessoal (sem verificação, uso próprio, só você) com escopo `drive.file` |
| Google Drive API | 15 GB por conta compartilhados com Gmail/Fotos; upload ≤ 750 GB/dia; arquivo ≤ 5 TB; upload resumível recomendado; cotas de consultas por minuto | conhecimento prévio — MÉDIA |
| TikTok / Instagram | Cliente TikTok não auditado ⇒ posts SELF_ONLY. Instagram Reels exige `video_url` público (HTTPS) | fase 1 (docs oficiais lidas) — ALTA |

## Serviços descartados
Oracle (falhou para o usuário e exige cartão), Codespaces (horas limitadas), Replit/Render/Fly/Railway (sem plano
gratuito permanente confiável sem cartão ou com suspensão por inatividade), AWS/GCP/Azure (cartão), Hugging Face Spaces
(CPU free dorme; viável como extra, mas fora do escopo testável aqui), Modal/créditos promocionais (não permanentes).

## Decisões de arquitetura derivadas
1. **Nenhuma nuvem gratuita fica sempre ligada com Python + disco** sem cartão. Logo: Cloudflare Worker + D1 (sempre ligado,
   limites suficientes) é o **coordenador**; executores (Kaggle, Colab, GitHub Actions) são **descartáveis**.
2. O estado que importa mora em **D1 (tarefas/leases/cotas)** e **Drive (arquivos + snapshots verificados do SQLite)**.
3. Cada executor é efêmero: guarda estado local só como cache; tudo é reconstruível do Drive + coordenador.
4. Instagram exige URL pública: com custo zero e só celular, isso **não está resolvido** (ver STATUS). TikTok (upload direto) funciona de qualquer executor.
5. Não usar várias contas para driblar limites (requisito do usuário).
