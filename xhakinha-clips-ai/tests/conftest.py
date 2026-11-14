import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["XH_FFMPEG_PRESET"] = "ultrafast"
SECRET = "t3st-s3cret-" + "x" * 40


@pytest.fixture(scope="session")
def synth_video(tmp_path_factory):
    """VÍDEO SINTÉTICO (testsrc2 + áudio gerado) — usado só para testes técnicos, não é conteúdo real."""
    d = tmp_path_factory.mktemp("synth")
    p = d / "synthetic.mp4"
    expr = ("0.02*(random(0)*2-1)+0.12*sin(2*PI*200*t)*(0.5+0.5*sin(2*PI*1.3*t))*lt(mod(t,9),5)"
            "+between(t,40,44)*0.6*sin(2*PI*300*t)*(0.5+0.5*sin(2*PI*5*t))+between(t,80,83)*0.8*(random(1)*2-1)")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=120",
                    "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=16000:d=120", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                    "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    from xhakinha.config import Settings
    s = Settings(secret_key=SECRET, data_dir=tmp_path / "data", env="test", cookie_secure=False,
                 retry_base_s=1, retry_cap_s=5, max_workers=2, min_free_gb=0.01)
    s.validate()
    s.ensure_dirs()
    return s


@pytest.fixture()
def engine(settings):
    from xhakinha.engine import Engine
    e = Engine(settings)
    e.db.set("whisper", 0)
    settings.whisper_enabled = False
    return e


@pytest.fixture()
def fake():
    from fake_platforms import Fake
    f = Fake()
    f.start()
    yield f
    f.stop()


def make_content(engine, video: Path, kind="upload", protected=0, status="downloaded", name="Fonte"):
    """Registra um conteúdo local (como o painel faz no upload)."""
    from xhakinha.media import probe
    from xhakinha.storage import quick_fingerprint
    db = engine.db
    src = db.one("SELECT id FROM sources WHERE platform='upload'")
    sid = src["id"] if src else db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('upload',?,'upload',0,1,0)", (name,))
    dest = engine.st.download_path(db.one("SELECT COALESCE(MAX(id),0)+1 n FROM contents")["n"])
    shutil.copy(video, dest)
    info = probe(dest)
    cid = db.x("INSERT INTO contents(source_id,ext_id,kind,title,status,local_path,duration,fingerprint,discovered,protected) VALUES(?,?,?,?,?,?,?,?,0,?)",
               (sid, f"up-{dest.name}", kind, "Vídeo de teste", status, str(dest), info["duration"], quick_fingerprint(dest, info["duration"]), protected))
    return cid
