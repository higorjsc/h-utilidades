# Envio de e-mails em massa pelo Gmail

Este script envia o mesmo e-mail para uma lista de contatos usando Gmail, um arquivo CSV e um modelo HTML editavel.

## Arquivos

- `mass_mail.py`: script principal. Configure remetente, assunto, CSV, HTML, anexo e modo de teste nele.
- `text_email.html`: modelo visual do e-mail. Edite o texto que sera enviado.
- `destinatarios.csv`: exemplo de lista de contatos.

## Requisitos

- Python instalado.
- Uma conta Gmail.
- Uma senha de app do Gmail. Use senha de app, nao a senha normal da conta.

O script usa apenas bibliotecas padrao do Python. Nao e necessario instalar `pandas` ou outro pacote externo.

## 1. Configure a lista de contatos

Edite o arquivo `destinatarios.csv` ou crie outro CSV no mesmo formato:

```csv
nome,e-mail
Nome Exemplo,exemplo@gmail.com
Outro Nome,outro_email@gmail.com
```

Regras importantes:

- A primeira linha e o cabecalho.
- A coluna `e-mail` e obrigatoria.
- A coluna `nome` e opcional, mas util se voce quiser usar `{{nome}}` no HTML.
- Salve o arquivo como CSV. Se usar Excel, prefira salvar como `CSV UTF-8`.

Se usar outro nome de arquivo, ajuste esta linha em `mass_mail.py`:

```python
ARQUIVO_CONTATOS = BASE_DIR / "destinatarios.csv"
```

## 2. Edite o texto do e-mail

Abra `text_email.html` e altere apenas os textos dentro das tags `<p>...</p>`.

Exemplo:

```html
<p>Prezado(a),</p>

<p>
    Escreva aqui o primeiro paragrafo do e-mail.
</p>
```

Se quiser chamar cada contato pelo nome, use o marcador `{{nome}}`:

```html
<p>Prezado(a) {{nome}},</p>
```

O script troca `{{nome}}` pelo valor da coluna `nome` no CSV.

## 3. Preencha a configuracao do script

Abra `mass_mail.py` e procure a area:

```python
# CONFIGURACAO - PREENCHA AQUI
```

Preencha os campos principais:

```python
EMAIL_REMETENTE = "seu_email@gmail.com"
SENHA_DE_APP = ""
ASSUNTO = "Assunto real do e-mail"
ARQUIVO_CONTATOS = BASE_DIR / "destinatarios.csv"
ARQUIVO_HTML = BASE_DIR / "text_email.html"
ANEXO = None
MODO_TESTE = True
EMAIL_TESTE = "seu_email@gmail.com"
```

Como preencher:

- `EMAIL_REMETENTE`: Gmail que vai enviar os e-mails.
- `SENHA_DE_APP`: recomendado deixar vazio. Assim, o script pede a senha no terminal e ela nao fica salva no arquivo.
- `ASSUNTO`: assunto que aparecera para os destinatarios.
- `ARQUIVO_CONTATOS`: arquivo CSV com os contatos.
- `ARQUIVO_HTML`: arquivo HTML com o texto do e-mail.
- `ANEXO`: use `None` para enviar sem anexo.
- `MODO_TESTE`: mantenha `True` no primeiro envio.
- `EMAIL_TESTE`: e-mail que recebera o teste.

## 4. Como usar anexo

Para enviar sem anexo:

```python
ANEXO = None
```

Para enviar com anexo, coloque o arquivo dentro da pasta `General_Script` e preencha:

```python
ANEXO = BASE_DIR / "arquivo.pdf"
```

Troque `arquivo.pdf` pelo nome real do arquivo.

## 5. Faca um envio de teste

No primeiro uso, mantenha:

```python
MODO_TESTE = True
EMAIL_TESTE = "seu_email@gmail.com"
```

Execute no terminal, a partir da pasta do repositorio:

```powershell
python General_Script\mass_mail.py
```

O script enviara apenas 1 e-mail para `EMAIL_TESTE`.

Confira:

- se o assunto esta correto;
- se o texto esta correto;
- se o anexo abriu corretamente, caso exista;
- se o e-mail nao caiu no spam.

## 6. Envie para todos os contatos

Depois que o teste estiver correto, altere:

```python
MODO_TESTE = False
```

Execute novamente:

```powershell
python General_Script\mass_mail.py
```

Agora o script enviara para todos os e-mails preenchidos no CSV.

## Problemas comuns

### `Troque EMAIL_REMETENTE por seu Gmail`

Voce ainda nao alterou:

```python
EMAIL_REMETENTE = "exemplo@gmail.com"
```

### `Troque EMAIL_TESTE por um e-mail real`

O modo de teste esta ativo, mas `EMAIL_TESTE` ainda esta com o exemplo.

### `O CSV precisa ter a coluna "e-mail"`

O cabecalho do CSV nao tem a coluna obrigatoria `e-mail`.

Use:

```csv
nome,e-mail
Nome Exemplo,exemplo@gmail.com
```

### Erro de login no Gmail

Verifique se:

- o e-mail remetente esta correto;
- a senha usada e uma senha de app;
- a conta Gmail permite uso de senha de app;
- a senha foi digitada sem espacos extras.

## Cuidados

- Sempre envie um teste antes do envio em massa.
- Evite salvar a senha de app diretamente no script.
- Revise a lista de contatos antes de mudar `MODO_TESTE` para `False`.
- Use este script apenas para contatos que podem receber esse e-mail.
