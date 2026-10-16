"""
PREVISÃO OPERACIONAL D+1 - INMET A707 (Presidente Prudente/SP)
==============================================================

Objetivo
--------
1. Lê a base histórica diária.
2. Consulta automaticamente os dados horários mais recentes do INMET.
3. Converte os dados horários em indicadores diários compatíveis com o modelo.
4. Atualiza uma base diária consolidada sem duplicar datas.
5. Treina/re-treina uma regressão linear múltipla usando pares D -> D+1.
6. Gera a previsão da temperatura média do dia seguinte.
7. Salva o modelo e o histórico das previsões.

bibliioteca principal:
    pip install pandas numpy scikit-learn

Observação importante
---------------------
O endpoint de estações do INMET pode mudar ou ficar temporariamente indisponível.
O script usa o padrão público historicamente utilizado:
    https://apitempo.inmet.gov.br/estacao/{inicio}/{fim}/{estacao}

Se o INMET alterar o endpoint, basta modificar API_URL_TEMPLATE na configuração.
"""

from __future__ import annotations

import json
import pickle
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


# ============================================================
# 1. CONFIGURAÇÃO
# ============================================================

ESTACAO = "A707"
NOME_ESTACAO = "Presidente Prudente/SP"
PASTA_PROJETO = Path(__file__).resolve().parent

# Base histórica diária
ARQUIVO_HISTORICO = PASTA_PROJETO / "presidente_prudente_A707_atualizado_com_media_12_meses.csv"

# Base consolidada: histórico + novos dias obtidos automaticamente do INMET.
ARQUIVO_ATUALIZADO = PASTA_PROJETO / "presidente_prudente_A707_dados_atualizados.csv"

# Arquivo em que o modelo treinado é persistido.
ARQUIVO_MODELO = PASTA_PROJETO / "modelo_regressao_temperatura_A707.pkl"

# Histórico de previsões realmente emitidas pelo sistema.
ARQUIVO_PREVISOES = PASTA_PROJETO / "historico_previsoes_operacionais.csv"

# Endpoint de observações de estação do INMET.
API_URL_TEMPLATE = (
    "https://apitempo.inmet.gov.br/estacao/{inicio}/{fim}/{estacao}"
)

# Quantos dias consultar novamente em cada execução.
# A sobreposição é proposital: permite incorporar revisões/atrasos do INMET.
DIAS_RECONSULTA = 7

# Divide consultas longas em blocos menores.
DIAS_POR_REQUISICAO = 30

# Se True, permite usar o dia de hoje mesmo antes de ele estar completo.
# Para uma previsão D+1 cientificamente comparável ao treinamento com dia completo,
# mantenha False e execute após o fechamento do dia.
PERMITIR_DIA_PARCIAL = False

# Convenção temporal para agregação das horas do INMET.
# "UTC" preserva a data informada pelo INMET e tende a ser a escolha mais segura
# se a base histórica foi criada agrupando diretamente a coluna Data do INMET.
# "BRASILIA" converte UTC -> America/Sao_Paulo antes de formar o dia.
# IMPORTANTE: usar a MESMA convenção da base histórica usada para treinar o modelo.
CONVENCAO_DIA = "UTC"  # opções: "UTC" ou "BRASILIA"

ALVO_ORIGEM = "temp_media_c"
ALVO = "temp_amanha_c"

PREDITORES = [
    "temp_media_c",
    "umidade_media_pct",
    "pressao_media_mb",
    "vento_medio_ms",
    "precipitacao_mm",
]

COLUNAS_DIARIAS = ["data"] + PREDITORES

# Período de previsões operacionais.
# Para cada data-alvo, o sistema usa o dia imediatamente anterior como dia-base.
# Se os dados reais do dia-base não estiverem completos no INMET, usa-se como
# cenário de entrada a média dos valores válidos dos 12 meses anteriores.
DATA_INICIO_PREVISAO = date(2026, 9, 10)
DATA_FIM_PREVISAO = date.today()
USAR_MEDIA_12_MESES_SEM_DADOS = True



# ============================================================
# 2. UTILIDADES
# ============================================================

def numero(valor):
    """Converte valor INMET para float e transforma sentinelas em NaN."""
    if valor is None:
        return np.nan

    if isinstance(valor, str):
        texto = valor.strip().replace(",", ".")
        if texto.lower() in {"", "null", "nan", "none"}:
            return np.nan
        valor = texto

    try:
        x = float(valor)
    except (TypeError, ValueError):
        return np.nan

    # Códigos de ausência encontrados em bases meteorológicas.
    if x in {9999.0, -9999.0, 9999.9, -9999.9}:
        return np.nan

    return x


def primeiro_campo(registro: dict, nomes: list[str]):
    """Retorna o primeiro campo existente entre nomes alternativos da API."""
    for nome in nomes:
        if nome in registro:
            return registro.get(nome)
    return None


def ler_csv_diario(caminho: Path) -> pd.DataFrame:
    """Lê uma base diária no formato do pipeline original."""
    if not caminho.exists():
        return pd.DataFrame(columns=COLUNAS_DIARIAS)

    df = pd.read_csv(
        caminho,
        sep=";",
        encoding="utf-8-sig",
        na_values=["", "null", "NULL", "NaN"],
    )

    if "data" not in df.columns:
        raise ValueError(f"O arquivo {caminho} não possui a coluna 'data'.")

    df["data"] = pd.to_datetime(df["data"], errors="coerce").dt.normalize()

    for coluna in PREDITORES:
        if coluna not in df.columns:
            raise ValueError(
                f"O arquivo {caminho} não possui a coluna obrigatória '{coluna}'."
            )
        df[coluna] = pd.to_numeric(df[coluna], errors="coerce")

    return (
        df[COLUNAS_DIARIAS]
        .dropna(subset=["data"])
        .sort_values("data")
        .drop_duplicates(subset=["data"], keep="last")
        .reset_index(drop=True)
    )


def salvar_csv_diario(df: pd.DataFrame, caminho: Path):
    saida = df.copy()
    saida["data"] = pd.to_datetime(saida["data"]).dt.strftime("%Y-%m-%d")
    saida.to_csv(caminho, index=False, sep=";", encoding="utf-8-sig")


# ============================================================
# 3. CONSULTA AUTOMÁTICA AO INMET
# ============================================================

def requisitar_json(url: str, tentativas: int = 3, timeout: int = 30):
    """GET com User-Agent e pequenas tentativas de recuperação."""
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; Pesquisa-Academica-INMET/1.0)"
                    ),
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            with urlopen(req, timeout=timeout) as resposta:
                corpo = resposta.read().decode("utf-8", errors="replace")

            if not corpo.strip():
                return []

            return json.loads(corpo)

        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            ultimo_erro = exc
            if tentativa < tentativas:
                time.sleep(2 * tentativa)

    raise RuntimeError(f"Falha ao consultar o INMET: {ultimo_erro}")


def intervalos(inicio: date, fim: date, tamanho: int):
    atual = inicio
    while atual <= fim:
        final_bloco = min(atual + timedelta(days=tamanho - 1), fim)
        yield atual, final_bloco
        atual = final_bloco + timedelta(days=1)


def baixar_inmet(inicio: date, fim: date) -> pd.DataFrame:
    """Baixa observações da A707 para o intervalo de datas."""
    todos = []

    if inicio > fim:
        return pd.DataFrame()

    for ini, final in intervalos(inicio, fim, DIAS_POR_REQUISICAO):
        url = API_URL_TEMPLATE.format(
            inicio=ini.isoformat(),
            fim=final.isoformat(),
            estacao=ESTACAO,
        )
        print(f"Consultando INMET: {ini} a {final} ...")
        dados = requisitar_json(url)

        if isinstance(dados, dict):
            # Algumas APIs envolvem o resultado em uma chave.
            for chave in ("data", "dados", "results", "result"):
                if isinstance(dados.get(chave), list):
                    dados = dados[chave]
                    break

        if isinstance(dados, list):
            todos.extend(dados)
        else:
            print(f"Aviso: resposta inesperada para {ini} a {final}.")

    if not todos:
        return pd.DataFrame()

    return pd.DataFrame(todos)


# ============================================================
# 4. NORMALIZAÇÃO DOS DADOS HORÁRIOS DO INMET
# ============================================================

def normalizar_inmet_horario(bruto: pd.DataFrame) -> pd.DataFrame:
    """
    Normaliza nomes comuns usados pelo INMET.

    Campos usuais do endpoint horário:
      DT_MEDICAO, HR_MEDICAO, TEMP_INS, UMD_INS,
      PRE_INS, VEN_VEL e CHUVA.
    """
    if bruto.empty:
        return pd.DataFrame()

    linhas = []

    for registro in bruto.to_dict(orient="records"):
        data_medicao = primeiro_campo(
            registro,
            ["DT_MEDICAO", "DATA", "data", "Data"],
        )
        hora_medicao = primeiro_campo(
            registro,
            ["HR_MEDICAO", "HORA", "hora", "Hora UTC"],
        )

        # A API pode devolver diário em alguns endpoints. Se houver TEMP_MED,
        # o tratamento diário é feito por função separada mais abaixo.
        temp = primeiro_campo(
            registro,
            ["TEMP_INS", "TEM_INS", "TEMPERATURA", "temp_ins"],
        )
        umid = primeiro_campo(
            registro,
            ["UMD_INS", "UMID_INS", "UMIDADE", "umd_ins"],
        )
        press = primeiro_campo(
            registro,
            ["PRE_INS", "PRESS_INS", "PRESSAO", "pre_ins"],
        )
        vento = primeiro_campo(
            registro,
            ["VEN_VEL", "VENTO_VEL", "VEL_VENTO", "ven_vel"],
        )
        chuva = primeiro_campo(
            registro,
            ["CHUVA", "PRECIPITACAO", "PRECIP_TOTAL", "chuva"],
        )

        dt = pd.to_datetime(data_medicao, errors="coerce")
        if pd.isna(dt):
            continue

        # HR_MEDICAO costuma vir como 0000, 0100, ..., 2300.
        hora_num = numero(hora_medicao)
        if not np.isnan(hora_num):
            hora_int = int(hora_num)
            hh = hora_int // 100
            mm = hora_int % 100
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                dt = dt + pd.Timedelta(hours=hh, minutes=mm)

        linhas.append(
            {
                "data_hora_utc": dt,
                "temperatura_c": numero(temp),
                "umidade_pct": numero(umid),
                "pressao_mb": numero(press),
                "vento_ms": numero(vento),
                "precipitacao_mm": numero(chuva),
            }
        )

    df = pd.DataFrame(linhas)
    if df.empty:
        return df

    # Filtros físicos amplos apenas para eliminar valores impossíveis.
    df.loc[~df["temperatura_c"].between(-60, 60), "temperatura_c"] = np.nan
    df.loc[~df["umidade_pct"].between(0, 100), "umidade_pct"] = np.nan
    df.loc[~df["pressao_mb"].between(700, 1100), "pressao_mb"] = np.nan
    df.loc[~df["vento_ms"].between(0, 100), "vento_ms"] = np.nan
    df.loc[~df["precipitacao_mm"].between(0, 500), "precipitacao_mm"] = np.nan

    if CONVENCAO_DIA.upper() == "BRASILIA":
        # A documentação do INMET informa horas em UTC.
        # Localiza em UTC e converte para o fuso de São Paulo/Brasília.
        data_hora = pd.to_datetime(df["data_hora_utc"], errors="coerce")
        data_hora = data_hora.dt.tz_localize("UTC").dt.tz_convert("America/Sao_Paulo")
        df["data_operacional"] = data_hora.dt.tz_localize(None).dt.normalize()
    else:
        df["data_operacional"] = pd.to_datetime(
            df["data_hora_utc"], errors="coerce"
        ).dt.normalize()

    return df


def agregar_diario(horario: pd.DataFrame) -> pd.DataFrame:
    """Converte observações horárias em uma linha por dia."""
    if horario.empty:
        return pd.DataFrame(columns=COLUNAS_DIARIAS)

    diario = (
        horario.groupby("data_operacional", as_index=False)
        .agg(
            temp_media_c=("temperatura_c", "mean"),
            umidade_media_pct=("umidade_pct", "mean"),
            pressao_media_mb=("pressao_mb", "mean"),
            vento_medio_ms=("vento_ms", "mean"),
            precipitacao_mm=("precipitacao_mm", lambda s: s.sum(min_count=1)),
            n_temp=("temperatura_c", "count"),
            n_umid=("umidade_pct", "count"),
            n_press=("pressao_mb", "count"),
            n_vento=("vento_ms", "count"),
            n_chuva=("precipitacao_mm", "count"),
        )
        .rename(columns={"data_operacional": "data"})
    )

    # Não transforma ausência total de chuva em zero; sum(min_count=1)
    # mantém NaN quando não houve nenhuma observação válida.
    return diario


# ============================================================
# 5. ATUALIZAÇÃO INCREMENTAL DA BASE
# ============================================================

def carregar_base_atual() -> pd.DataFrame:
    """Prioriza a base já atualizada; na primeira execução usa o histórico."""
    if ARQUIVO_ATUALIZADO.exists():
        return ler_csv_diario(ARQUIVO_ATUALIZADO)
    return ler_csv_diario(ARQUIVO_HISTORICO)


def atualizar_base_inmet(base: pd.DataFrame) -> pd.DataFrame:
    hoje = date.today()

    if base.empty:
        inicio = hoje - timedelta(days=DIAS_RECONSULTA)
    else:
        ultima_data = pd.to_datetime(base["data"]).max().date()
        inicio = ultima_data - timedelta(days=DIAS_RECONSULTA - 1)

    # Evita pedir datas futuras.
    fim = hoje

    bruto = baixar_inmet(inicio, fim)
    if bruto.empty:
        print("Aviso: o INMET não retornou observações novas.")
        return base

    horario = normalizar_inmet_horario(bruto)
    if horario.empty:
        print("Aviso: não foi possível interpretar os dados recebidos do INMET.")
        print("Colunas retornadas:", list(bruto.columns))
        return base

    diario = agregar_diario(horario)

    # Exibe qualidade/quantidade das observações recebidas.
    if not diario.empty:
        print("\n=== DIAS RECEBIDOS DO INMET ===")
        print(
            diario[
                ["data", "temp_media_c", "umidade_media_pct", "pressao_media_mb",
                 "vento_medio_ms", "precipitacao_mm", "n_temp"]
            ].tail(10).to_string(index=False)
        )

    novos = diario[COLUNAS_DIARIAS].copy()

    combinado = pd.concat([base[COLUNAS_DIARIAS], novos], ignore_index=True)
    combinado["data"] = pd.to_datetime(combinado["data"], errors="coerce").dt.normalize()

    # As linhas mais recentes vindas da API substituem a mesma data já armazenada.
    combinado = (
        combinado.dropna(subset=["data"])
        .sort_values("data")
        .drop_duplicates(subset=["data"], keep="last")
        .reset_index(drop=True)
    )

    salvar_csv_diario(combinado, ARQUIVO_ATUALIZADO)
    print(f"\nBase atualizada salva em: {ARQUIVO_ATUALIZADO.resolve()}")
    print(f"Última data armazenada: {combinado['data'].max().date()}")

    return combinado


# ============================================================
# 6. CONSTRUÇÃO DO ALVO D+1 SEM LIGAR DIAS NÃO CONSECUTIVOS
# ============================================================

def criar_base_modelo(base: pd.DataFrame) -> pd.DataFrame:
    df = base.copy().sort_values("data").reset_index(drop=True)
    df["proxima_data"] = df["data"].shift(-1)
    df[ALVO] = df[ALVO_ORIGEM].shift(-1)

    dias_entre = (df["proxima_data"] - df["data"]).dt.days
    df.loc[dias_entre != 1, ALVO] = np.nan

    return df.drop(columns="proxima_data")


# ============================================================
# 7. TREINAMENTO DO MODELO OPERACIONAL
# ============================================================

def metricas(y_true, y_pred, n_preditores):
    r2 = r2_score(y_true, y_pred)
    n = len(y_true)
    r2_aj = (
        1 - (1 - r2) * (n - 1) / (n - n_preditores - 1)
        if n > n_preditores + 1
        else np.nan
    )
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    return r2, r2_aj, mae, rmse


def treinar_modelo(base: pd.DataFrame):
    """
    Treina com todos os pares D -> D+1 disponíveis.

    A validação/teste científico continua. Todos os dados
    disponíveis para o modelo operacional final.
    """
    df = criar_base_modelo(base)
    modelo_df = df[["data"] + PREDITORES + [ALVO]].dropna().copy()

    if len(modelo_df) < 30:
        raise ValueError("Poucos pares completos D -> D+1 para treinar o modelo.")

    X = modelo_df[PREDITORES]
    y = modelo_df[ALVO]

    reg = LinearRegression()
    reg.fit(X, y)

    pacote = {
        "modelo": reg,
        "preditores": PREDITORES,
        "estacao": ESTACAO,
        "nome_estacao": NOME_ESTACAO,
        "convencao_dia": CONVENCAO_DIA,
        "treinado_em": datetime.now().isoformat(timespec="seconds"),
        "ultima_data_treino": modelo_df["data"].max().strftime("%Y-%m-%d"),
        "n_amostras": len(modelo_df),
    }

    with open(ARQUIVO_MODELO, "wb") as f:
        pickle.dump(pacote, f)

    print("\n=== MODELO OPERACIONAL ===")
    print(f"Amostras completas D->D+1: {len(modelo_df)}")
    print(f"Último D usado no treinamento: {modelo_df['data'].max().date()}")
    print(f"Modelo salvo em: {ARQUIVO_MODELO.resolve()}")

    return pacote


# ============================================================
# 8. PREVISÃO DO DIA SEGUINTE
# ============================================================

def selecionar_dia_para_previsao(base: pd.DataFrame) -> pd.Series:
    completos = base.dropna(subset=PREDITORES).sort_values("data").copy()

    if completos.empty:
        raise ValueError("Não existe nenhum dia com todos os preditores disponíveis.")

    if not PERMITIR_DIA_PARCIAL:
        hoje = pd.Timestamp(date.today())
        completos = completos[completos["data"] < hoje]
        if completos.empty:
            raise ValueError("Não há dia completo anterior a hoje para previsão.")

    return completos.iloc[-1]


def calcular_media_12_meses(base: pd.DataFrame, data_base: pd.Timestamp) -> tuple[pd.Series, pd.Timestamp, pd.Timestamp]:
    """
    Calcula, separadamente para cada preditor, a média dos valores válidos
    nos 12 meses anteriores e inclusive até a data-base.

    Atenção: isto é uma imputação/cenário climatológico de entrada, não uma
    observação real do dia-base.
    """
    data_base = pd.to_datetime(data_base).normalize()
    inicio = data_base - pd.DateOffset(years=1) + pd.Timedelta(days=1)

    janela = base[
        (base["data"] >= inicio) &
        (base["data"] <= data_base)
    ].copy()

    if janela.empty:
        raise ValueError("Não há dados no intervalo de 12 meses para construir a entrada substituta.")

    medias = janela[PREDITORES].mean(skipna=True)

    faltantes = medias[medias.isna()].index.tolist()
    if faltantes:
        raise ValueError(
            "Não foi possível calcular a média de 12 meses para: "
            + ", ".join(faltantes)
        )

    contagens = janela[PREDITORES].count()

    print("\n=== FALLBACK: MÉDIA DOS ÚLTIMOS 12 MESES ===")
    print(f"Janela: {inicio.date()} a {data_base.date()}")
    for p in PREDITORES:
        print(f"{p}: média={medias[p]:.4f} | n válido={int(contagens[p])}")

    linha = pd.Series({p: medias[p] for p in PREDITORES})
    linha["data"] = data_base
    return linha, inicio, data_base


def salvar_previsao(
    data_base: pd.Timestamp,
    previsao: float,
    linha: pd.Series,
    data_alvo: pd.Timestamp | None = None,
    metodo_entrada: str = "observacao_real",
    janela_inicio: pd.Timestamp | None = None,
    janela_fim: pd.Timestamp | None = None,
):
    if data_alvo is None:
        data_alvo = pd.to_datetime(data_base) + pd.Timedelta(days=1)
    else:
        data_alvo = pd.to_datetime(data_alvo).normalize()

    registro = pd.DataFrame(
        [
            {
                "emitida_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "estacao": ESTACAO,
                "data_base": pd.to_datetime(data_base).strftime("%Y-%m-%d"),
                "data_prevista": data_alvo.strftime("%Y-%m-%d"),
                "temperatura_prevista_c": round(float(previsao), 4),
                "metodo_entrada": metodo_entrada,
                "janela_media_inicio": (
                    pd.to_datetime(janela_inicio).strftime("%Y-%m-%d")
                    if janela_inicio is not None else ""
                ),
                "janela_media_fim": (
                    pd.to_datetime(janela_fim).strftime("%Y-%m-%d")
                    if janela_fim is not None else ""
                ),
                **{p: linha[p] for p in PREDITORES},
            }
        ]
    )

    if ARQUIVO_PREVISOES.exists():
        historico = pd.read_csv(ARQUIVO_PREVISOES, sep=";", encoding="utf-8-sig")
        historico = pd.concat([historico, registro], ignore_index=True)
        historico = historico.drop_duplicates(subset=["data_prevista"], keep="last")
    else:
        historico = registro

    historico.to_csv(
        ARQUIVO_PREVISOES,
        index=False,
        sep=";",
        encoding="utf-8-sig",
    )

    return data_alvo


def prever_dia_seguinte(base: pd.DataFrame, pacote: dict):
    """
    Gera previsões D+1, dia após dia, no intervalo configurado.

    Para cada data-alvo:
      1) usa os preditores observados do dia imediatamente anterior;
      2) se faltarem e USAR_MEDIA_12_MESES_SEM_DADOS=True,
         usa a média móvel dos 12 meses anteriores como cenário substituto.

    Assim, a ausência de atualização do INMET não interrompe a sequência
    de previsões do período.
    """
    modelo = pacote["modelo"]

    data_inicio = pd.Timestamp(DATA_INICIO_PREVISAO).normalize()
    data_fim = pd.Timestamp(DATA_FIM_PREVISAO).normalize()

    if data_inicio > data_fim:
        raise ValueError(
            f"DATA_INICIO_PREVISAO ({data_inicio.date()}) é posterior a "
            f"DATA_FIM_PREVISAO ({data_fim.date()})."
        )

    previsoes = []

    for data_alvo in pd.date_range(data_inicio, data_fim, freq="D"):
        data_base = data_alvo - pd.Timedelta(days=1)

        candidatos = base[base["data"] == data_base]
        tem_d_real = (
            not candidatos.empty
            and candidatos[PREDITORES].notna().all(axis=1).any()
        )

        if tem_d_real:
            linha = candidatos[
                candidatos[PREDITORES].notna().all(axis=1)
            ].iloc[-1]
            metodo = "observacao_real"
            janela_inicio = None
            janela_fim = None

        elif USAR_MEDIA_12_MESES_SEM_DADOS:
            linha, janela_inicio, janela_fim = calcular_media_12_meses(
                base, data_base
            )
            metodo = "media_12_meses"

        else:
            raise ValueError(
                f"Não há todos os preditores observados em {data_base.date()}."
            )

        X_novo = pd.DataFrame([{p: linha[p] for p in PREDITORES}])
        previsao = float(modelo.predict(X_novo)[0])

        salvar_previsao(
            data_base=data_base,
            previsao=previsao,
            linha=linha,
            data_alvo=data_alvo,
            metodo_entrada=metodo,
            janela_inicio=janela_inicio,
            janela_fim=janela_fim,
        )

        print("\n" + "=" * 60)
        print("PREVISÃO OPERACIONAL D+1")
        print("=" * 60)
        print(f"Estação: {ESTACAO} - {NOME_ESTACAO}")
        print(f"Dia-base necessário: {data_base.date()}")
        print(f"Temperatura média prevista para: {data_alvo.date()}")
        print(f"Método de entrada: {metodo}")
        print(f"PREVISÃO: {previsao:.2f} °C")
        print("=" * 60)

        previsoes.append(
            {
                "data_base": data_base,
                "data_alvo": data_alvo,
                "metodo": metodo,
                "previsao_c": previsao,
            }
        )

    print(f"Histórico de previsões: {ARQUIVO_PREVISOES.resolve()}")

    return previsoes


# ============================================================
# 9. EXECUÇÃO COMPLETA
# ============================================================

def main():
    print("=" * 60)
    print("SISTEMA AUTOMÁTICO INMET -> REGRESSÃO -> PREVISÃO D+1")
    print(f"Estação {ESTACAO} - {NOME_ESTACAO}")
    print("=" * 60)

    if not ARQUIVO_HISTORICO.exists() and not ARQUIVO_ATUALIZADO.exists():
        raise FileNotFoundError(
            "Não encontrei a base histórica. Coloque o arquivo "
            f"'{ARQUIVO_HISTORICO.name}' na mesma pasta do script."
        )

    base = carregar_base_atual()
    print(f"Base carregada: {len(base)} dias")
    print(f"Período atual: {base['data'].min().date()} a {base['data'].max().date()}")

    # 1) Atualização automática pelo INMET.
    try:
        base = atualizar_base_inmet(base)
    except Exception as exc:
        # Não destrói o funcionamento do modelo se a API estiver temporariamente fora.
        print(f"\nATENÇÃO: não foi possível atualizar pelo INMET: {exc}")
        print("O script continuará com a última base local disponível.")

    # 2) Re-treina o modelo operacional com todos os pares completos disponíveis.
    pacote = treinar_modelo(base)

    # 3) Emite previsões D+1, dia após dia, no período configurado.
    return prever_dia_seguinte(base, pacote)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nERRO: {exc}")
        sys.exit(1)

