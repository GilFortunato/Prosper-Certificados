# Prosper Certificados

Central de emissão e validação para as formações da Prosper. Cada formação tem um programa, um modelo PowerPoint e uma lista de participantes. A mesma central atende várias formações e programas.

| Página | Uso |
| --- | --- |
| `/` | O participante informa CPF e chave para baixar o certificado em PDF. |
| `/validar` | Qualquer pessoa com a chave consulta o status, o programa, a formação e a data de emissão. |
| `/admin/` | A equipe administra as formações, os modelos e as listas. |

## Cadastrar uma formação

1. Entre no painel e crie a formação. Preencha a carga horária e o período se esses campos aparecem no certificado.
2. Envie o modelo em `.pptx`. Digite `<<Nome>>` e `<<Chave de validação>>` em caixas de texto editáveis dos slides. É possível usar vários slides, incluindo frente e verso. Mantenha cada marcador inteiro no mesmo parágrafo.
3. Baixe a planilha-modelo ou use um Excel `.xlsx`. Na primeira aba, coloque **Nome**, **CPF** e **Chave de validação** no cabeçalho. O painel também aceita cabeçalhos como **Nome completo** e **Chave validadora**.
4. Baixe a prévia em PDF e confira as fontes, o espaço para nomes longos e o verso. Importe a lista e clique em **Liberar emissão**.

Use `<<Programa>>`, `<<Formação>>`, `<<Carga horária>>`, `<<Período>>` e `<<Data de emissão>>` para aproveitar os dados da formação e da emissão. Para um campo como `<<Turma>>`, acrescente uma coluna **Turma** no Excel. O painel inclui essas colunas personalizadas na planilha-modelo.

- Importe somente as pessoas autorizadas a receber o certificado. Enquanto a formação estiver em rascunho, cada importação válida substitui a lista. Depois de liberada, a importação acrescenta novos cadastros e preserva os anteriores.
- Gere uma chave aleatória diferente para cada certificado, mesmo quando a pessoa tem certificados em várias formações. A coluna da chave deve estar em formato de texto. O portal aceita chaves de até 64 caracteres, com pelo menos oito letras ou números e separadores opcionais `-` e `_`. A consulta aceita letras maiúsculas e minúsculas.
- Use até 5.000 linhas de participantes, com os cabeçalhos necessários entre as primeiras 80 colunas. Os limites são 5 MB para o Excel e 12 MB para o PowerPoint. Use valores fixos nas células e salve o PowerPoint com suas imagens incorporadas.
- A consulta pública confirma o registro a partir da primeira geração bem-sucedida. A revogação bloqueia downloads e sinaliza os certificados que já haviam sido emitidos. Para corrigir um participante de uma formação liberada, revogue a chave anterior e importe o cadastro corrigido com uma nova chave.

## Rodar localmente

Requisitos de execução local incluem Python 3.12 ou superior e LibreOffice Impress com o comando `soffice` disponível. Para indicar outro caminho, configure `PROSPER_SOFFICE`. O LibreOffice usa as fontes instaladas no ambiente. [Parâmetros de execução do LibreOffice](https://help.libreoffice.org/latest/pt-BR/text/shared/guide/start_parameters.html).

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

Abra `http://127.0.0.1:5000/`. A senha do painel é a que você escolheu ao gerar o arquivo `.env`. O banco local é criado em `data/certificados.sqlite3`.

## Hospedar o portal

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
- O CPF é convertido em um identificador criptográfico no momento da importação. A consulta pública mostra os dados da formação e o status da emissão. Carregue planilhas e modelos pelo painel.

## Testes

```bash
python -m unittest discover -s tests -v
```
