"""Coordenador, cotas, leases/fencing, locks, breaker, reconciliação.
Rótulo: REAL = executado de verdade (SQLite, threads); nenhum serviço externo é envolvido."""
import json
import threading

import pytest

from xhakinha.cloud import quota
from xhakinha.cloud.client import LocalClient
from xhakinha.cloud.coordinator import LEASE_S, Coordinator, Fenced


class Clock:
    def __init__(self, t=1_800_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s


@pytest.fixture()
def co():
    ck = Clock()
    c = Coordinator(":memory:", clock=ck)
    c.ck = ck
    return c


def reg(c, name, prov="kaggle-cpu", **caps):
    c.register(name, prov, caps, session_seconds=11 * 3600)


def test_duas_executores_disputam_a_mesma_tarefa_so_um_vence(tmp_path):          # REAL (threads + SQLite em arquivo)
    c = Coordinator(tmp_path / "c.db")
    for i in range(8):
        c.register(f"e{i}", "colab", {}, 6 * 3600)
    c.submit("sleep", {}, key="uma")
    wins, lock = [], threading.Lock()

    def go(i):
        t = c.claim(f"e{i}")
        if t:
            with lock: wins.append(i)
    ths = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ths]; [t.join() for t in ths]
    assert len(wins) == 1


def test_submit_idempotente_por_chave(co):
    a = co.submit("x", key="k1"); b = co.submit("x", key="k1")
    assert b["id"] == a["id"] and b["duplicate"] and sum(co.counts().values()) == 1


def test_lease_expira_e_executor_antigo_e_barrado_por_fencing(co):             # Kaggle cai → outro assume
    reg(co, "kaggle1"); reg(co, "colab1", "colab")
    co.submit("sleep", key="t")
    t1 = co.claim("kaggle1")
    co.save_checkpoint(t1["id"], "kaggle1", t1["fence"], {"done": 3})
    co.ck.adv(LEASE_S + 10)
    assert co.reap()["requeued"] == [t1["id"]]
    co.ck.adv(10)
    co.heartbeat("colab1")
    t2 = co.claim("colab1")
    assert t2["id"] == t1["id"] and t2["fence"] == t1["fence"] + 1
    assert json.loads(t2["checkpoint"]) == {"done": 3}                         # checkpoint sobrevive à troca de executor
    for fn in (lambda: co.renew(t1["id"], "kaggle1", t1["fence"]),
               lambda: co.save_checkpoint(t1["id"], "kaggle1", t1["fence"], {"x": 1}),
               lambda: co.complete(t1["id"], "kaggle1", t1["fence"], {"r": "velho"})):
        with pytest.raises(Fenced):
            fn()
    co.complete(t2["id"], "colab1", t2["fence"], {"r": "novo"})
    assert json.loads(co.get_task(t1["id"])["result"]) == {"r": "novo"}


def test_complete_duplicado_nao_reaplica(co):
    reg(co, "e"); co.submit("x", key="a"); t = co.claim("e")
    co.complete(t["id"], "e", t["fence"], {"n": 1})
    with pytest.raises(Fenced):
        co.complete(t["id"], "e", t["fence"], {"n": 2})
    assert json.loads(co.get_task(t["id"])["result"]) == {"n": 1}


def test_sem_gpu_tarefa_so_vai_para_executor_com_gpu(co):
    reg(co, "cpu", "kaggle-cpu", gpu=False); reg(co, "gpu", "kaggle-gpu", gpu=True)
    co.submit("process_content", needs=["gpu"], key="g")
    assert co.claim("cpu") is None
    assert co.claim("gpu")["key"] == "g"


def test_preferencia_suave_espera_o_provedor_preferido_e_depois_libera(co):
    reg(co, "gh", "github"); reg(co, "k", "kaggle-gpu", gpu=True)
    co.submit("x", prefer=["kaggle-gpu"], key="p", est_seconds=30)
    assert co.claim("gh") is None
    co.ck.adv(700)
    co.heartbeat("gh")
    assert co.claim("gh") is not None


def test_cota_esgotada_nao_inicia_tarefa_e_recupera_no_novo_periodo(co):
    co.set_provider_config("github", day_seconds=3600, safety=1.0)
    reg(co, "gh", "github")
    co.heartbeat("gh", used_seconds=3500)
    co.submit("x", key="a", est_seconds=300)
    assert co.claim("gh") is None                                              # 3500+300 > 3600: NÃO inicia
    co.submit("y", key="b", est_seconds=60, priority=9)
    assert co.claim("gh")["key"] == "b"                                        # a que cabe passa
    co.ck.adv(86400 * 2); reg(co, "gh", "github")                              # nova sessão no novo dia
    assert co.claim("gh")["key"] == "a"                                        # novo dia UTC: renovou


def test_reserva_de_cota_para_prioridade_alta(co):
    co.set_provider_config("kaggle-gpu", week_seconds=10000, safety=1.0, reserve_frac=0.2, session_seconds=0)
    reg(co, "g", "kaggle-gpu", gpu=True)
    co.heartbeat("g", 7900)
    co.submit("lo", key="lo", needs=["gpu"], est_seconds=500, priority=5)
    co.submit("hi", key="hi", needs=["gpu"], est_seconds=500, priority=9)
    t = co.claim("g")
    assert t["key"] == "hi"                                                    # reserva (20%) só para prioridade ≥ 8
    co.fail(t["id"], "g", t["fence"], "x", kind="permanent")
    assert co.claim("g") is None


def test_tarefa_que_nao_cabe_na_sessao_nao_e_iniciada(co):
    co.register("k", "kaggle-cpu", {}, session_seconds=600)
    co.submit("grande", key="g", est_seconds=3600)
    assert co.claim("k") is None
    co.submit("pequena", key="p", est_seconds=60)
    t = co.claim("k")                                                          # 600s - margem 120s = 480s: só a pequena cabe
    assert t["key"] == "p"


def test_circuit_breaker_abre_apos_falhas_do_provedor_e_fecha_com_sucesso(co):
    reg(co, "k", "kaggle-cpu")
    for i in range(3):
        co.submit("x", key=f"t{i}")
        t = co.claim("k")
        co.fail(t["id"], "k", t["fence"], "kaggle 503", kind="provider", retry_after=0)
    assert co.provider("kaggle-cpu")["breaker"] == "open"
    co.submit("y", key="novo")
    assert co.claim("k") is None                                               # provedor indisponível: não atribui
    co.ck.adv(130)
    co.heartbeat("k")
    t = co.claim("k")                                                          # meia-abertura: uma sonda
    assert co.provider("kaggle-cpu")["breaker"] == "half_open" or t
    co.complete(t["id"], "k", t["fence"], {})
    assert co.provider("kaggle-cpu")["breaker"] == "closed"


def test_falha_isolada_de_um_provedor_nao_afeta_outro(co):
    reg(co, "k", "kaggle-cpu"); reg(co, "c", "colab")
    for i in range(3):
        co.submit("x", key=f"f{i}", prefer=[])
        t = co.claim("k"); co.fail(t["id"], "k", t["fence"], "down", kind="provider", retry_after=1000)
    assert co.claim("k") is None
    co.submit("ok", key="ok")
    assert co.claim("c")["key"] == "ok"


def test_cota_estourada_reportada_pausa_o_provedor(co):
    reg(co, "g", "github"); co.submit("x", key="q")
    t = co.claim("g")
    co.fail(t["id"], "g", t["fence"], "429 quota", kind="quota", retry_after=3600)
    co.submit("y", key="y")
    assert co.claim("g") is None
    co.ck.adv(3700); co.heartbeat("g")
    assert co.claim("g") is not None


def test_backoff_e_dead_letter(co):
    reg(co, "e", "local")
    co.submit("x", key="d", max_attempts=2)
    t = co.claim("e"); assert co.fail(t["id"], "e", t["fence"], "e1") == "queued"
    assert co.claim("e") is None                                               # backoff: ainda não é hora
    co.ck.adv(4000); co.heartbeat("e")
    t = co.claim("e"); assert co.fail(t["id"], "e", t["fence"], "e2") == "dead"
    co.retry_dead(t["id"])
    assert co.claim("e")["attempts"] == 1


def test_executor_mudo_vira_perdido_e_release_nao_penaliza(co):
    reg(co, "e", "local"); reg(co, "f", "local")
    co.submit("x", key="a"); t = co.claim("e")
    co.release(t["id"], "e", t["fence"])
    assert co.get_task(t["id"])["attempts"] == 0
    co.ck.adv(400); co.heartbeat("f")
    assert "e" in co.reap()["lost_executors"]


def test_lock_distribuido_exclusivo_com_fencing(co):
    f1 = co.lock_acquire("brain", "A", 100)
    assert f1 == 1 and co.lock_acquire("brain", "B", 100) is None
    co.ck.adv(101)
    f2 = co.lock_acquire("brain", "B", 100)
    assert f2 == 2
    with pytest.raises(Fenced):
        co.lock_renew("brain", "A", f1)
    assert not co.lock_check("brain", "A", f1) and co.lock_check("brain", "B", f2)


def test_reconciliacao_repara_inconsistencias_e_reporta_o_resto(co):          # "inconsistência de banco"
    reg(co, "e", "local")
    for k in "abc":
        co.submit("x", key=k)
    co.c.execute("UPDATE tasks SET status='running', owner=NULL, lease_until=NULL WHERE key='a'")
    co.c.execute("UPDATE tasks SET owner='fantasma' WHERE key='b'")
    co.c.execute("UPDATE tasks SET status='done', result=NULL WHERE key='c'")
    rep = co.reconcile()
    assert len(rep["fixed"]) == 2 and any("sem resultado" in r for r in rep["reported"])
    assert co.get_by_key("a")["status"] == "queued" and co.get_by_key("b")["owner"] is None
    assert co.integrity()["ok"]


def test_cancelar_so_se_ainda_na_fila(co):
    reg(co, "e", "local"); co.submit("x", key="q"); co.submit("y", key="r")
    assert co.cancel_if_queued("q")
    t = co.claim("e")
    assert not co.cancel_if_queued(t["key"])


def test_decide_puro_incerteza_sinalizada():
    d = quota.decide(quota.PROVIDER_DEFAULTS["colab"], {"day": 0, "week": 0}, 60, 5, False, 7200)
    assert d.ok and d.uncertain                                                # colab: cota desconhecida => incerteza
    assert not quota.decide({"session_seconds": 100, "safety": 1.0, "known": True}, {}, 500, 5, False, 99999).ok


def test_breaker_next_maquina_de_estados():
    st = quota.breaker_next("closed", 0, 0, None, 0.0, ok=False)
    st = quota.breaker_next(st[0], st[1], st[2], st[3], 1.0, ok=False)
    st = quota.breaker_next(st[0], st[1], st[2], st[3], 2.0, ok=False)
    assert st[0] == "open" and not st[4]
    assert quota.breaker_next("open", 3, 1, 2.0, 50.0)[4] is False
    assert quota.breaker_next("open", 3, 1, 2.0, 200.0)[0] == "half_open"
    assert quota.breaker_next("half_open", 3, 1, 2.0, 200.0, ok=False)[0] == "open"


def test_rate_limiter_token_bucket():
    t = [0.0]
    rl = quota.RateLimiter(2.0, 2, clock=lambda: t[0])
    assert rl.acquire() == 0 and rl.acquire() == 0
    assert rl.acquire() == pytest.approx(0.5)
    t[0] += 1.0
    assert rl.acquire() == 0


def test_client_local_mesma_interface(co):
    cl = LocalClient(co)
    cl.register("e", "local", {}, 3600)
    cl.submit("x", {}, key="z")
    t = cl.claim("e")
    cl.complete(t["id"], "e", t["fence"], {"ok": 1})
    assert cl.state()["counts"]["done"] == 1
