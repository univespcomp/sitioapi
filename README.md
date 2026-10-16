# Previsão operacional INMET

## Configuração

Se ainda não houver um arquivo `.env`, copie `.env.example` para `.env` e ajuste
os valores necessários. O servidor carrega esse arquivo automaticamente quando
executado localmente; no Docker Compose, as variáveis declaradas na configuração
são encaminhadas ao container.

`DATABASE_URL` configura a conexão PostgreSQL com o Supabase usando as
credenciais do banco; `DATABASE_SSLMODE` usa `require` por padrão.
A rota `GET /api/executar` não exige token e dispara a execução do pipeline e a
sincronização. Não exponha o servidor publicamente sem adicionar outra forma de
controle de acesso.
Para executar sem sincronização ao PostgreSQL, deixe `DATABASE_URL` vazio.
No Docker Compose, `HOST_PORT` define a porta local publicada (padrão `3000`).

As opções `INMET_*` configuram a estação, os arquivos de entrada/saída, a API,
o período da previsão e a agregação diária. Caminhos relativos são resolvidos a
partir da raiz do projeto. `INMET_PREDICTION_END_DATE` vazio significa a data
atual. Os booleanos aceitam `true`/`false`, `1`/`0`, `yes`/`no` ou `sim`/`nao`.

## Execução

Instale as dependências e inicie o servidor local:

```sh
pip install -r requirements.txt
python dashboard/servidor.py
```

Ou inicie com Docker Compose:

```sh
docker compose up --build
```

O dashboard estará disponível em `http://localhost:${HOST_PORT}` (porta `3000`
por padrão).
