import csv
import hmac
import importlib.util
import json
import os
import threading
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import psycopg


BASE_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
PORT = int(os.environ.get("PORT", "8000"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
EXECUTION_LOCK = threading.Lock()

spec = importlib.util.spec_from_file_location("pipeline_inmet", BASE_DIR / "PI IV.py")
if spec is None or spec.loader is None:
    raise RuntimeError("Não foi possível carregar o algoritmo de previsão.")

pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def ler_csv(caminho, delimitador=";"):
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        return list(csv.DictReader(arquivo, delimiter=delimitador))


def numero_ou_none(valor):
    if valor is None or not valor.strip() or valor.strip().lower() in {"nan", "null", "none"}:
        return None
    return float(valor.replace(",", "."))


def sincronizar_postgresql():
    if not DATABASE_URL:
        raise RuntimeError("A variável DATABASE_URL não está configurada.")

    arquivo_observacoes = pipeline.ARQUIVO_ATUALIZADO
    if not arquivo_observacoes.exists():
        arquivo_observacoes = pipeline.ARQUIVO_HISTORICO

    observacoes = []
    for registro in ler_csv(arquivo_observacoes):
        if not registro.get("data"):
            continue
        observacoes.append(
            (
                pipeline.ESTACAO,
                date.fromisoformat(registro["data"]),
                numero_ou_none(registro.get("temp_media_c", "")),
                numero_ou_none(registro.get("umidade_media_pct", "")),
                numero_ou_none(registro.get("pressao_media_mb", "")),
                numero_ou_none(registro.get("vento_medio_ms", "")),
                numero_ou_none(registro.get("precipitacao_mm", "")),
            )
        )

    previsoes = []
    for registro in ler_csv(pipeline.ARQUIVO_PREVISOES):
        if not registro.get("data_prevista") or not registro.get("temperatura_prevista_c"):
            continue
        previsoes.append(
            (
                registro["estacao"],
                date.fromisoformat(registro["data_base"]),
                date.fromisoformat(registro["data_prevista"]),
                datetime.fromisoformat(registro["emitida_em"]),
                float(registro["temperatura_prevista_c"]),
                registro["metodo_entrada"],
                date.fromisoformat(registro["janela_media_inicio"])
                if registro.get("janela_media_inicio") else None,
                date.fromisoformat(registro["janela_media_fim"])
                if registro.get("janela_media_fim") else None,
                numero_ou_none(registro.get("temp_media_c", "")),
                numero_ou_none(registro.get("umidade_media_pct", "")),
                numero_ou_none(registro.get("pressao_media_mb", "")),
                numero_ou_none(registro.get("vento_medio_ms", "")),
                numero_ou_none(registro.get("precipitacao_mm", "")),
            )
        )

    with psycopg.connect(
        DATABASE_URL,
        sslmode="require",
        prepare_threshold=None,
    ) as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(
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
                """
            )
            cursor.execute(
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
                """
            )
            if observacoes:
                cursor.executemany(
                    """
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
                    """,
                    observacoes,
                )
            if previsoes:
                cursor.executemany(
                    """
                    INSERT INTO previsoes (
                        estacao, data_base, data_prevista, emitida_em,
                        temperatura_prevista_c, metodo_entrada,
                        janela_media_inicio, janela_media_fim, temp_media_c,
                        umidade_media_pct, pressao_media_mb, vento_medio_ms,
                        precipitacao_mm
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
                    """,
                    previsoes,
                )

    return len(observacoes), len(previsoes)


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
        super().end_headers()

    def do_GET(self):
        caminho = urlsplit(self.path).path
        if caminho == "/api/executar":
            return self.executar_algoritmo()
        if caminho == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard/")
            self.end_headers()
            return
        if caminho in {
            "/historico_previsoes_operacionais.csv",
            "/presidente_prudente_A707_dados_atualizados.csv",
        }:
            return self.servir_csv(caminho)
        if caminho in {"/dashboard", "/dashboard/"} or caminho.startswith("/dashboard/"):
            return super().do_GET()
        self.send_error(404)

    def executar_algoritmo(self):
        if not API_TOKEN:
            return self.responder_json(
                503,
                {"ok": False, "erro": "A variável API_TOKEN não está configurada."},
            )
        token_enviado = self.headers.get("Authorization", "")
        if not hmac.compare_digest(token_enviado, f"Bearer {API_TOKEN}"):
            return self.responder_json(
                401,
                {"ok": False, "erro": "Token de acesso inválido."},
            )
        if not DATABASE_URL:
            return self.responder_json(
                503,
                {"ok": False, "erro": "DATABASE_URL não está configurada."},
            )

        csv_atualizado = False
        try:
            with EXECUTION_LOCK:
                previsoes = pipeline.main()
                csv_atualizado = True
                total_observacoes, total_previsoes = sincronizar_postgresql()

            ultima = previsoes[-1] if previsoes else None
            self.responder_json(
                200,
                {
                    "ok": True,
                    "estacao": pipeline.ESTACAO,
                    "observacoes_sincronizadas": total_observacoes,
                    "previsoes_sincronizadas": total_previsoes,
                    "ultima_previsao": (
                        {
                            "data_base": ultima["data_base"].date().isoformat(),
                            "data_prevista": ultima["data_alvo"].date().isoformat(),
                            "temperatura_prevista_c": float(ultima["previsao_c"]),
                            "metodo_entrada": ultima["metodo"],
                        }
                        if ultima else None
                    ),
                },
            )
        except Exception as exc:
            self.responder_json(
                500,
                {
                    "ok": False,
                    "erro": str(exc),
                    "csv_atualizado": csv_atualizado,
                },
            )

    def servir_csv(self, caminho):
        arquivo = (
            pipeline.ARQUIVO_PREVISOES
            if caminho == "/historico_previsoes_operacionais.csv"
            else pipeline.ARQUIVO_ATUALIZADO
        )
        if caminho == "/presidente_prudente_A707_dados_atualizados.csv" and not arquivo.exists():
            arquivo = pipeline.ARQUIVO_HISTORICO
        if not arquivo.exists():
            return self.send_error(404)

        corpo = arquivo.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def responder_json(self, status, dados):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)


if __name__ == "__main__":
    servidor = ThreadingHTTPServer(("", PORT), Handler)
    print(f"Dashboard: http://localhost:{PORT}/dashboard/")
    print(f"Execução do algoritmo: http://localhost:{PORT}/api/executar")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor finalizado.")
    finally:
        servidor.server_close()