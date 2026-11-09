# Prosper Certificados

Central para validar certificados de vários programas e formações. O modo padrão abre a consulta em `/validacao`.

| Página | Uso |
| --- | --- |
| `/validacao` | A pessoa informa a chave e a data de emissão do certificado. O resultado mostra o status e, para certificados válidos, o nome, o programa, a formação e as habilidades. |
| `/admin/` | A equipe cadastra formações, importa certificados emitidos e gerencia a revogação de chaves. |

## Cadastrar certificados para validação

1. Entre no painel e crie a formação com o nome do programa e da formação ou turma.
2. Na seção **Lista para validação**, envie o arquivo Excel `.xlsx`. Use a primeira aba e inclua **Nome** e **Chave de validação** no cabeçalho.
3. Informe a data de emissão e as habilidades para cada certificado. Se forem comuns a todas as pessoas do lote, preencha no painel. Se variarem, use as colunas **Data de emissão** e **Habilidades** na planilha. Quando a linha contém esses valores, eles prevalecem sobre os dados do lote.
4. Use **Importar e ativar validação**. As chaves cadastradas podem ser consultadas imediatamente com a data correspondente.

Digite uma habilidade por linha. No Excel, use quebras de linha na célula ou separe os itens com `|`. A data pode ser uma célula de data do Excel ou texto no formato `DD/MM/AAAA`. A planilha-modelo está disponível na tela da formação.

- Cada chave é única entre os programas. Mantenha a coluna em formato de texto. A consulta aceita a chave em letras maiúsculas ou minúsculas.
- Para corrigir a data ou as habilidades, reimporte a mesma chave para a mesma pessoa e formação. Para PDFs já gerados pelo portal, a data permanece vinculada ao documento emitido.
- A revogação continua válida após reimportar a chave. A consulta com chave ou data divergente apresenta uma mensagem genérica.
- A importação aceita até 5.000 linhas e 4 MB na Vercel ou 5 MB no ambiente local. CPF e e-mail ficam fora do cadastro de validação.

## Etapa de autoemissão

Em um ambiente com LibreOffice, habilite a emissão com `PROSPER_ENABLE_ISSUANCE=1`. Nesse modo, a página inicial permite baixar o PDF com CPF e chave, e a consulta continua em `/validacao`. A implantação na Vercel roda o validador.

1. Entre no painel e crie a formação. Preencha a carga horária e o período se esses campos aparecem no certificado.
2. Envie o modelo em `.pptx`. Digite `<<Nome>>` e `<<Chave de validação>>` em caixas de texto editáveis dos slides. É possível usar vários slides, incluindo frente e verso. Mantenha cada marcador inteiro no mesmo parágrafo.
3. Baixe a planilha-modelo ou use um Excel `.xlsx`. Na primeira aba, coloque **Nome**, **CPF** e **Chave de validação** no cabeçalho. O painel também aceita cabeçalhos como **Nome completo** e **Chave validadora**.
4. Baixe a prévia em PDF e confira as fontes, o espaço para nomes longos e o verso. Importe a lista e clique em **Liberar emissão**.

Use `<<Programa>>`, `<<Formação>>`, `<<Carga horária>>`, `<<Período>>` e `<<Data de emissão>>` para aproveitar os dados da formação e da emissão. Para um campo como `<<Turma>>`, acrescente uma coluna **Turma** no Excel. O painel inclui essas colunas personalizadas na planilha-modelo.

- Importe somente as pessoas autorizadas a receber o certificado. Enquanto a formação estiver em rascunho, cada importação válida substitui a lista. Depois de liberada, a importação acrescenta novos cadastros e preserva os anteriores.
- Gere uma chave aleatória diferente para cada certificado, mesmo quando a pessoa tem certificados em várias formações. A coluna da chave deve estar em formato de texto. O portal aceita chaves de até 64 caracteres, com pelo menos oito letras ou números e separadores opcionais `-` e `_`. A consulta aceita letras maiúsculas e minúsculas.
- Use até 5.000 linhas de participantes, com os cabeçalhos necessários entre as primeiras 80 colunas. Os limites são 5 MB para o Excel e 12 MB para o PowerPoint. Use valores fixos nas células e salve o PowerPoint com suas imagens incorporadas.
- Certificados gerados pelo portal ficam disponíveis para validação após a primeira geração bem-sucedida. Certificados cadastrados na lista de validação seguem a data registrada nessa lista. A revogação bloqueia downloads e sinaliza os certificados que já haviam sido emitidos. Para corrigir um participante de uma formação liberada, revogue a chave anterior e importe o cadastro corrigido com uma nova chave.

## Rodar localmente

Use Python 3.12 ou superior. Para habilitar a geração de PDFs, instale também o LibreOffice Impress com o comando `soffice` disponível. Para indicar outro caminho, configure `PROSPER_SOFFICE`. O LibreOffice usa as fontes instaladas no ambiente. [Parâmetros de execução do LibreOffice](https://help.libreoffice.org/latest/pt-BR/text/shared/guide/start_parameters.html).

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

(
  umask 077
  python -m certificados.config > .env
)

PROSPER_SECURE_COOKIES=0 python -m flask --app certificados run
```

Abra `http://127.0.0.1:5000/validacao`. A senha do painel é a que você escolheu ao gerar o arquivo `.env`. O ambiente local usa `data/certificados.sqlite3` por padrão e usa PostgreSQL quando `DATABASE_URL` está definida.

## Vercel e Neon

1. Crie o banco Neon e aplique o esquema de `db/neon.sql` com um usuário autorizado a executar migrações.
2. No projeto Vercel, configure `DATABASE_URL` com a conexão PostgreSQL via TLS e uma função de acesso da aplicação com permissões de leitura e escrita nas tabelas do portal.
3. Configure `PROSPER_SESSION_SECRET`, `PROSPER_CPF_KEY` e `PROSPER_ADMIN_PASSWORD_HASH` como segredos de produção. Use `python -m certificados.config` para gerar os valores e definir a senha do painel. Para trocar só a senha, atualize apenas o hash e refaça o deploy.
4. Faça o deploy da `main`. O ponto de entrada é `app.py` e a página de consulta é `/validacao`.

O esquema é aplicado na preparação do banco. A aplicação usa o banco Neon nas requisições e mantém os dados entre deploys.

## Executar com Docker

A imagem Docker inclui o LibreOffice e o servidor Gunicorn. Rode uma instância com volume persistente em `/data` e encaminhe o tráfego público por um proxy HTTPS para a porta 8000 do contêiner.

```bash
docker build -t prosper-certificados .
docker volume create prosper-certificados-dados
docker run -d --name prosper-certificados \
  --env-file .env \
  --mount source=prosper-certificados-dados,target=/data \
  -p 127.0.0.1:8000:8000 prosper-certificados
```

- Mantenha os cookies seguros no ambiente HTTPS. Configure `PROSPER_PROXY_HOPS` somente quando a aplicação está atrás de proxies de confiança e esse valor corresponde à cadeia que configura `X-Forwarded-For`. [Configuração de proxy no Flask](https://flask.palletsprojects.com/en/stable/deploying/proxy_fix/).
- Guarde os segredos do arquivo `.env` separadamente e faça backups protegidos do banco. A chave `PROSPER_CPF_KEY` permite reconhecer CPFs já cadastrados após reiniciar ou restaurar o ambiente.
- O CPF é convertido em um identificador criptográfico no momento da importação. A consulta pública exige a chave e a data de emissão antes de mostrar o titular e os dados da formação. Carregue planilhas e modelos pelo painel.

## Testes

```bash
python -m unittest discover -s tests -v
```
