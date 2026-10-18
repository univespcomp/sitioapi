"""Servidor do dashboard + endpoint que executa o pipeline INMET e sincroniza no Supabase."""

import csv
import importlib.util
import json
import logging
import os
import sys
import threading
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from psycopg.rows import dict_row
import psycopg
from dotenv import load_dotenv

#---------------------------------------------------------------------------
#CORS
#---------------------------------------------------------------------------
CORS_ORIGINS = {
    o.strip().rstrip("/")
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
}

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
load_dotenv()  # fallback: .env da pasta atual

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("servidor")

DASHBOARD_DIR = BASE_DIR / "dashboard"
PORT = int(os.environ.get("PORT", "8000"))
EXECUTION_LOCK = threading.Lock()
VALORES_NULOS = {"", "nan", "null", "none"}


def normalizar_database_url(url: str) -> str:
    """Aceita URLs coladas com aspas ou no formato SQLAlchemy (postgresql+psycopg2://)."""
    url = url.strip().strip('"').strip("'")
    for prefixo in ("postgresql+psycopg2://", "postgresql+psycopg://"):
        if url.startswith(prefixo):
            return "postgresql://" + url[len(prefixo):]
    return url


DATABASE_URL = normalizar_database_url(os.environ.get("DATABASE_URL", ""))

# --------------------------------------------------------------------------
# Carrega o algoritmo de previsão
# --------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("pipeline_inmet", BASE_DIR / "PI IV.py")
if spec is None or spec.loader is None:
    raise RuntimeError("Não foi possível carregar o algoritmo de previsão (PI IV.py).")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


# --------------------------------------------------------------------------
# Leitura e conversão dos CSVs
# --------------------------------------------------------------------------
def ler_csv(caminho: Path, delimitador: str = ";") -> list[dict]:
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        return list(csv.DictReader(arquivo, delimiter=delimitador))


def para_float(valor):
    if valor is None:
        return None
    valor = valor.strip()
    if valor.lower() in VALORES_NULOS:
        return None
    try:
        return float(valor.replace(",", "."))
    except ValueError:
        return None


def para_data(valor):
    return date.fromisoformat(valor) if valor else None


def carregar_observacoes() -> list[tuple]:
    arquivo = pipeline.ARQUIVO_ATUALIZADO
    if not arquivo.exists():
        arquivo = pipeline.ARQUIVO_HISTORICO

    linhas = []
    for reg in ler_csv(arquivo):
        if not reg.get("data"):
            continue
        try:
            linhas.append((
                pipeline.ESTACAO,
                date.fromisoformat(reg["data"]),
                para_float(reg.get("temp_media_c")),
                para_float(reg.get("umidade_media_pct")),
                para_float(reg.get("pressao_media_mb")),
                para_float(reg.get("vento_medio_ms")),
                para_float(reg.get("precipitacao_mm")),
            ))
        except ValueError:
            log.warning("Observação ignorada (data inválida): %s", reg.get("data"))
    return linhas


def carregar_previsoes() -> list[tuple]:
    linhas = []
    for reg in ler_csv(pipeline.ARQUIVO_PREVISOES):
        if not reg.get("data_prevista") or not reg.get("temperatura_prevista_c"):
            continue
        try:
            linhas.append((
                reg["estacao"],
                date.fromisoformat(reg["data_base"]),
                date.fromisoformat(reg["data_prevista"]),
                datetime.fromisoformat(reg["emitida_em"]),
                float(reg["temperatura_prevista_c"].replace(",", ".")),
                reg["metodo_entrada"],
                para_data(reg.get("janela_media_inicio")),
                para_data(reg.get("janela_media_fim")),
                para_float(reg.get("temp_media_c")),
                para_float(reg.get("umidade_media_pct")),
                para_float(reg.get("pressao_media_mb")),
                para_float(reg.get("vento_medio_ms")),
                para_float(reg.get("precipitacao_mm")),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("Previsão ignorada (%s): %s", type(exc).__name__, exc)
    return linhas


# --------------------------------------------------------------------------
# Banco de dados (Supabase / PostgreSQL)
# --------------------------------------------------------------------------
SQL_DDL = (
    """
    CREATE TABLE IF NOT EXISTS observacoes_diarias (
        estacao TEXT NOT NULL,
        data DATE NOT NULL,
        temp_media_c DOUBLE PRECISION,
        umidade_media_pct DOUBLE PRECISION,
        pressao_media_mb DOUBLE PRECISION,
        vento_medio_ms DOUBLE PRECISION,
        precipitacao_mm DOUBLE PRECISION,
        PRIMARY KEY (estacao, data)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS previsoes (
        estacao TEXT NOT NULL,
        data_base DATE NOT NULL,
        data_prevista DATE NOT NULL,
        emitida_em TIMESTAMP NOT NULL,
        temperatura_prevista_c DOUBLE PRECISION NOT NULL,
        metodo_entrada TEXT NOT NULL,
        janela_media_inicio DATE,
        janela_media_fim DATE,
        temp_media_c DOUBLE PRECISION,
        umidade_media_pct DOUBLE PRECISION,
        pressao_media_mb DOUBLE PRECISION,
        vento_medio_ms DOUBLE PRECISION,
        precipitacao_mm DOUBLE PRECISION,
        PRIMARY KEY (estacao, data_prevista)
    )
    """,
    # Impede acesso público via API REST do Supabase (a conexão direta ignora o RLS).
    "ALTER TABLE observacoes_diarias ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE previsoes ENABLE ROW LEVEL SECURITY",
)

SQL_UPSERT_OBSERVACOES = """
    INSERT INTO observacoes_diarias (
        estacao, data, temp_media_c, umidade_media_pct,
        pressao_media_mb, vento_medio_ms, precipitacao_mm
    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (estacao, data) DO UPDATE SET
        temp_media_c = EXCLUDED.temp_media_c,
        umidade_media_pct = EXCLUDED.umidade_media_pct,
        pressao_media_mb = EXCLUDED.pressao_media_mb,
        vento_medio_ms = EXCLUDED.vento_medio_ms,
        precipitacao_mm = EXCLUDED.precipitacao_mm
"""

SQL_UPSERT_PREVISOES = """
    INSERT INTO previsoes (
        estacao, data_base, data_prevista, emitida_em,
        temperatura_prevista_c, metodo_entrada,
        janela_media_inicio, janela_media_fim, temp_media_c,
        umidade_media_pct, pressao_media_mb, vento_medio_ms, precipitacao_mm
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (estacao, data_prevista) DO UPDATE SET
        data_base = EXCLUDED.data_base,
        emitida_em = EXCLUDED.emitida_em,
        temperatura_prevista_c = EXCLUDED.temperatura_prevista_c,
        metodo_entrada = EXCLUDED.metodo_entrada,
        janela_media_inicio = EXCLUDED.janela_media_inicio,
        janela_media_fim = EXCLUDED.janela_media_fim,
        temp_media_c = EXCLUDED.temp_media_c,
        umidade_media_pct = EXCLUDED.umidade_media_pct,
        pressao_media_mb = EXCLUDED.pressao_media_mb,
        vento_medio_ms = EXCLUDED.vento_medio_ms,
        precipitacao_mm = EXCLUDED.precipitacao_mm
"""

DICAS_CONEXAO = (
    (
        ("network is unreachable", "could not translate host name",
         "name or service not known", "no route to host"),
        "Não há rota até o host. O host direto 'db.<ref>.supabase.co' é só IPv6; "
        "use a string do Session pooler (aws-0-<região>.pooler.supabase.com:5432).",
    ),
    (
        ("tenant or user not found",),
        "No pooler o usuário precisa ser 'postgres.<ref-do-projeto>', não só 'postgres'.",
    ),
    (
        ("password authentication failed",),
        "Senha incorreta. Se tiver caracteres especiais, codifique na URL (ex.: @ vira %40).",
    ),
    (
        ("timeout expired", "timed out"),
        "Tempo esgotado: rede/firewall bloqueando a porta (5432 ou 6543).",
    ),
    (
        ("missing \"=\"", "invalid connection option", "invalid dsn", "invalid uri"),
        "DATABASE_URL malformada: deve começar com postgresql:// e não ter colchetes "
        "[ ] em volta da senha.",
    ),
)


def explicar_erro_conexao(exc: Exception) -> str:
    texto = str(exc).strip()
    texto_baixo = texto.lower()
    for termos, dica in DICAS_CONEXAO:
        if any(t in texto_baixo for t in termos):
            return f"{dica} (detalhe: {texto.splitlines()[0] if texto else type(exc).__name__})"
    return texto or type(exc).__name__


def conectar() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("A variável DATABASE_URL não está configurada.")
    try:
        return psycopg.connect(
            DATABASE_URL,
            sslmode="require",
            connect_timeout=10,      # sem isso, rede sem rota pode travar por minutos
            prepare_threshold=None,  # necessário para o pooler em modo transaction (6543)
        )
    except psycopg.Error as exc:
        raise RuntimeError(explicar_erro_conexao(exc)) from exc


def sincronizar_postgresql() -> tuple[int, int]:
    observacoes = carregar_observacoes()
    previsoes = carregar_previsoes()

    with conectar() as conexao:  # commit automático ao sair do bloco
        with conexao.cursor() as cursor:
            for ddl in SQL_DDL:
                cursor.execute(ddl)
            if observacoes:
                cursor.executemany(SQL_UPSERT_OBSERVACOES, observacoes)
            if previsoes:
                cursor.executemany(SQL_UPSERT_PREVISOES, previsoes)

    return len(observacoes), len(previsoes)


def testar_conexao() -> str:
    with conectar() as conexao:
        return conexao.execute("SELECT version()").fetchone()[0]

def consultar(sql, params=()):
    with conectar() as conexao:
        with conexao.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

# --------------------------------------------------------------------------
# Servidor HTTP
# --------------------------------------------------------------------------
ROTAS_CSV = {
    "/historico_previsoes_operacionais.csv": lambda: pipeline.ARQUIVO_PREVISOES,
    "/presidente_prudente_A707_dados_atualizados.csv": lambda: (
        pipeline.ARQUIVO_ATUALIZADO
        if pipeline.ARQUIVO_ATUALIZADO.exists()
        else pipeline.ARQUIVO_HISTORICO
    ),
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def translate_path(self, path):
        caminho = urlsplit(path).path
        if caminho in {"/dashboard", "/dashboard/"}:
            caminho = "/"
        elif caminho.startswith("/dashboard/"):
            caminho = caminho[len("/dashboard"):]
        return super().translate_path(caminho)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        origem = self.headers.get("Origin", "")
        if origem in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origem)
        self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            self.headers.get("Access-Control-Request-Headers", "*"),
        )
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- roteamento -------------------------------------------------------
    def do_GET(self):
        caminho = urlsplit(self.path).path
        if caminho == "/api/executar":
            return self.executar_algoritmo()
        if caminho == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard/")
            self.end_headers()
            return
        if caminho in ROTAS_CSV:
            return self.servir_csv(ROTAS_CSV[caminho]())
        if caminho in {"/dashboard", "/dashboard/"} or caminho.startswith("/dashboard/"):
            return super().do_GET()
        if caminho == "/api/previsoes":
            return self.listar("previsoes", "data_prevista")
        if caminho == "/api/observacoes":
            return self.listar("observacoes_diarias", "data")
        self.send_error(404)

    do_POST = do_GET  # permite disparar o pipeline por POST também

    # ---- endpoint principal -----------------------------------------------
    def executar_algoritmo(self):
        if not EXECUTION_LOCK.acquire(blocking=False):
            return self.responder_json(
                409, {"ok": False, "erro": "Já existe uma execução em andamento."}
            )

        try:
            # Etapa 1: pipeline. Se falhar, não há o que sincronizar.
            try:
                previsoes = pipeline.main()
            except Exception as exc:
                log.exception("Falha no pipeline")
                return self.responder_json(
                    500, {"ok": False, "etapa": "pipeline", "erro": str(exc)}
                )

            # Etapa 2: banco. O CSV (que alimenta o dashboard) já foi atualizado,
            # então uma falha de conexão aqui não derruba a resposta.
            banco = {"sincronizado": False}
            total_obs = total_prev = None
            try:
                total_obs, total_prev = sincronizar_postgresql()
                banco["sincronizado"] = True
            except Exception as exc:
                log.exception("Falha ao sincronizar com o banco")
                banco["erro"] = str(exc)

            ultima = previsoes[-1] if previsoes else None
            self.responder_json(
                200,
                {
                    "ok": True,
                    "csv_atualizado": True,
                    "estacao": pipeline.ESTACAO,
                    "banco": banco,
                    "observacoes_sincronizadas": total_obs,
                    "previsoes_sincronizadas": total_prev,
                    "ultima_previsao": (
                        {
                            "data_base": ultima["data_base"].date().isoformat(),
                            "data_prevista": ultima["data_alvo"].date().isoformat(),
                            "temperatura_prevista_c": float(ultima["previsao_c"]),
                            "metodo_entrada": ultima["metodo"],
                        }
                        if ultima
                        else None
                    ),
                },
            )
        finally:
            EXECUTION_LOCK.release()

    # ---- helpers de resposta ----------------------------------------------

    def listar(self, tabela, coluna_data):
        params = parse_qs(urlsplit(self.path).query)
        try:
            limite = max(1, min(int(params.get("limite", ["90"])[0]), 1000))
        except ValueError:
            limite = 90
        try:
            # Pega os N mais recentes e devolve em ordem cronológica (bom para gráficos).
            linhas = consultar(
                f"SELECT * FROM (SELECT * FROM {tabela} WHERE estacao = %s "
                f"ORDER BY {coluna_data} DESC LIMIT %s) t ORDER BY {coluna_data} ASC",
                (pipeline.ESTACAO, limite),
            )
        except Exception:
            log.exception("Falha ao consultar %s", tabela)
            return self.responder_json(503, {"ok": False, "erro": "Banco indisponível."})
        self.responder_json(200, {"ok": True, "dados": linhas})

    def servir_csv(self, arquivo: Path):
        if not arquivo.exists():
            return self.send_error(404)
        corpo = arquivo.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def responder_json(self, status: int, dados: dict):
        corpo = json.dumps(dados, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)


def executar_uma_vez():
    pipeline.main()
    total_obs, total_prev = sincronizar_postgresql()
    print(f"Sincronizado: {total_obs} observações, {total_prev} previsões.")

def main():
    if "--testar-banco" in sys.argv:
        try:
            print("Conexão OK:", testar_conexao())
        except Exception as exc:
            print("Falha na conexão:", exc)
            sys.exit(1)
        return
    
    if "--executar" in sys.argv:
        executar_uma_vez()
        return

    servidor = ThreadingHTTPServer(("", PORT), Handler)
    print(f"Dashboard: http://localhost:{PORT}/dashboard/")
    print(f"Execução do algoritmo: http://localhost:{PORT}/api/executar")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor finalizado.")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()