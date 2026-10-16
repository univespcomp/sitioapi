import csv
from pathlib import Path
from collections import Counter

entrada = Path("/mnt/data/afc4a693-e1d1-4dac-974a-654d1d0c0356.csv")
saida = Path("/mnt/data/presidente_prudente_A707_limpo_2016_2026.csv")

# O arquivo do INMET possui 9 linhas de metadados e, depois, o cabeçalho tabular.
with entrada.open("r", encoding="utf-8-sig", newline="") as f:
    linhas = f.readlines()

indice_cabecalho = next(
    i for i, linha in enumerate(linhas)
    if linha.startswith("Data Medicao;")
)

reader = csv.reader(linhas[indice_cabecalho:], delimiter=";")
rows = list(reader)

cabecalho_original = rows[0]

# Remove a última coluna vazia criada pelo ";" no fim de cada linha.
if cabecalho_original and cabecalho_original[-1] == "":
    cabecalho_original = cabecalho_original[:-1]

# Nomes curtos e padronizados, compatíveis com o pipeline já usado.
mapa_colunas = {
    "Data Medicao": "data",
    "PRECIPITACAO TOTAL, DIARIO (AUT)(mm)": "precipitacao_mm",
    "PRESSAO ATMOSFERICA MEDIA DIARIA (AUT)(mB)": "pressao_media_mb",
    "TEMPERATURA DO PONTO DE ORVALHO MEDIA DIARIA (AUT)(°C)": "temp_orvalho_media_c",
    "TEMPERATURA MAXIMA, DIARIA (AUT)(°C)": "temp_max_c",
    "TEMPERATURA MEDIA, DIARIA (AUT)(°C)": "temp_media_c",
    "TEMPERATURA MINIMA, DIARIA (AUT)(°C)": "temp_min_c",
    "UMIDADE RELATIVA DO AR, MEDIA DIARIA (AUT)(%)": "umidade_media_pct",
    "UMIDADE RELATIVA DO AR, MINIMA DIARIA (AUT)(%)": "umidade_min_pct",
    "VENTO, RAJADA MAXIMA DIARIA (AUT)(m/s)": "rajada_max_ms",
    "VENTO, VELOCIDADE MEDIA DIARIA (AUT)(m/s)": "vento_medio_ms",
}
cabecalho_limpo = [mapa_colunas.get(c, c) for c in cabecalho_original]

n_null = 0
n_linhas = 0
linhas_totalmente_sem_dados = 0
datas_vistas = Counter()
dados_limpos = []

for row in rows[1:]:
    if not row:
        continue

    # Ajusta quantidade de colunas ao cabeçalho.
    if len(row) > len(cabecalho_original):
        row = row[:len(cabecalho_original)]
    elif len(row) < len(cabecalho_original):
        row += [""] * (len(cabecalho_original) - len(row))

    limpa = []
    for valor in row:
        v = valor.strip()
        if v.lower() in {"null", "nan", "none"}:
            n_null += 1
            v = ""
        limpa.append(v)

    # Ignora apenas linhas sem data.
    if not limpa[0]:
        continue

    datas_vistas[limpa[0]] += 1
    n_linhas += 1

    if all(v == "" for v in limpa[1:]):
        linhas_totalmente_sem_dados += 1

    dados_limpos.append(limpa)

with saida.open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f, delimiter=";", lineterminator="\n")
    writer.writerow(cabecalho_limpo)
    writer.writerows(dados_limpos)

duplicadas = {d: n for d, n in datas_vistas.items() if n > 1}

print(f"Arquivo limpo criado: {saida.name}")
print(f"Período: {dados_limpos[0][0]} a {dados_limpos[-1][0]}")
print(f"Linhas de dados preservadas: {n_linhas}")
print(f"Valores 'null' convertidos em campos vazios: {n_null}")
print(f"Linhas com data, mas sem nenhuma variável meteorológica: {linhas_totalmente_sem_dados}")
print(f"Datas duplicadas encontradas: {len(duplicadas)}")
