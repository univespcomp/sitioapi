FROM python:3.11-slim

# Define o diretório de trabalho no container
WORKDIR /app

# Copia o arquivo de requisitos e instala as dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o código para o container
COPY . .

# Expõe a porta que o servidor local utilizará
EXPOSE 8000

# O algoritmo é executado sob demanda pela rota GET /api/executar.
CMD ["python", "dashboard/servidor.py"]
