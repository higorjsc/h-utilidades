"""
Envia e-mails em massa pelo Gmail usando:
- um arquivo CSV com os contatos;
- um arquivo HTML com o texto do e-mail;
- um anexo opcional.

Antes de executar, preencha apenas a area CONFIGURACAO.
"""

from __future__ import annotations

import csv
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from getpass import getpass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

# =============================================================================
# CONFIGURACAO - PREENCHA AQUI
# =============================================================================

# 1) E-mail Gmail que fara o envio.
EMAIL_REMETENTE = "exemplo@gmail.com"

# 2) Senha de app do Gmail.
# Recomendado: deixe vazio ("") para digitar a senha no terminal sem exibir.
SENHA_DE_APP = ""

# 3) Assunto que aparecera na caixa de entrada dos destinatarios.
ASSUNTO = "Assunto do e-mail"

# 4) Arquivo CSV com os contatos.
# O CSV precisa ter uma coluna chamada "e-mail".
ARQUIVO_CONTATOS = BASE_DIR / "destinatarios.csv"

# 5) Modelo HTML com o texto do e-mail.
ARQUIVO_HTML = BASE_DIR / "text_email.html"

# 6) Anexo opcional.
# Para enviar sem anexo, use None.
ANEXO = None
# Para enviar com anexo, coloque o arquivo dentro de General_Script e use:
# ANEXO = BASE_DIR / "nome_do_arquivo.pdf"

# 7) Modo de teste.
# Enquanto estiver True, o script envia apenas 1 e-mail para EMAIL_TESTE.
# Depois de conferir o teste, mude para False para enviar para todo o CSV.
MODO_TESTE = True
EMAIL_TESTE = "exemplo@gmail.com"

# 8) Nome das colunas usadas no CSV.
COLUNA_EMAIL = "e-mail"
COLUNA_NOME = "nome"

# 9) Configuracao padrao do Gmail. Nao altere se for usar Gmail.
SERVIDOR_SMTP = "smtp.gmail.com"
PORTA_SMTP = 587


def validar_configuracao() -> None:
    """Impede execucao com valores de exemplo."""
    if EMAIL_REMETENTE == "exemplo@gmail.com":
        raise ValueError('Troque EMAIL_REMETENTE por seu Gmail, por exemplo: "seu_email@gmail.com".')

    if MODO_TESTE and EMAIL_TESTE == "exemplo@gmail.com":
        raise ValueError('Troque EMAIL_TESTE por um e-mail real antes de testar.')

    if not ASSUNTO.strip() or ASSUNTO == "Assunto do e-mail":
        raise ValueError('Troque ASSUNTO pelo assunto real do e-mail.')

    if ANEXO is not None and not ANEXO.exists():
        raise FileNotFoundError(f"Anexo nao encontrado: {ANEXO}")


def carregar_contatos(arquivo_csv: Path) -> list[dict[str, str]]:
    """Le contatos do CSV e retorna apenas linhas com e-mail preenchido."""
    if not arquivo_csv.exists():
        raise FileNotFoundError(f"Arquivo de contatos nao encontrado: {arquivo_csv}")

    with arquivo_csv.open("r", encoding="utf-8-sig", newline="") as arquivo:
        leitor = csv.DictReader(arquivo)

        if leitor.fieldnames is None:
            raise ValueError("O arquivo CSV esta vazio ou sem cabecalho.")

        if COLUNA_EMAIL not in leitor.fieldnames:
            colunas = ", ".join(leitor.fieldnames)
            raise ValueError(f'O CSV precisa ter a coluna "{COLUNA_EMAIL}". Colunas encontradas: {colunas}')

        contatos = []
        for numero_linha, linha in enumerate(leitor, start=2):
            email = (linha.get(COLUNA_EMAIL) or "").strip()
            if not email:
                print(f"Linha {numero_linha} ignorada: coluna {COLUNA_EMAIL} vazia.")
                continue

            linha[COLUNA_EMAIL] = email
            contatos.append(linha)

    if not contatos:
        raise ValueError("Nenhum contato com e-mail preenchido foi encontrado no CSV.")

    return contatos


def ler_html(arquivo_html: Path) -> str:
    """Le o modelo HTML do e-mail."""
    if not arquivo_html.exists():
        raise FileNotFoundError(f"Arquivo HTML nao encontrado: {arquivo_html}")

    return arquivo_html.read_text(encoding="utf-8")


def personalizar_html(modelo_html: str, contato: dict[str, str]) -> str:
    """
    Substitui marcadores simples do HTML por dados do CSV.

    Exemplo:
    - se o CSV tem a coluna "nome";
    - e o HTML tem {{nome}};
    - o script troca {{nome}} pelo valor da coluna nome.
    """
    html = modelo_html
    for coluna, valor in contato.items():
        html = html.replace("{{" + coluna + "}}", (valor or "").strip())
    return html


def anexar_arquivo(mensagem: MIMEMultipart, caminho_anexo: Path | None) -> None:
    """Adiciona o anexo na mensagem, quando ANEXO estiver preenchido."""
    if caminho_anexo is None:
        return

    with caminho_anexo.open("rb") as arquivo:
        parte_anexo = MIMEBase("application", "octet-stream")
        parte_anexo.set_payload(arquivo.read())

    encoders.encode_base64(parte_anexo)
    parte_anexo.add_header("Content-Disposition", "attachment", filename=caminho_anexo.name)
    mensagem.attach(parte_anexo)


def criar_mensagem(contato: dict[str, str], modelo_html: str) -> MIMEMultipart:
    """Monta a mensagem de e-mail para um contato."""
    email_destinatario = contato[COLUNA_EMAIL]

    mensagem = MIMEMultipart()
    mensagem["From"] = EMAIL_REMETENTE
    mensagem["To"] = email_destinatario
    mensagem["Subject"] = ASSUNTO

    html_final = personalizar_html(modelo_html, contato)
    mensagem.attach(MIMEText(html_final, "html", "utf-8"))
    anexar_arquivo(mensagem, ANEXO)

    return mensagem


def enviar_emails(contatos: list[dict[str, str]], modelo_html: str, senha_de_app: str) -> None:
    """Conecta no Gmail e envia os e-mails."""
    with smtplib.SMTP(SERVIDOR_SMTP, PORTA_SMTP) as servidor:
        servidor.starttls()
        servidor.login(EMAIL_REMETENTE, senha_de_app)

        for contato in contatos:
            email_destinatario = contato[COLUNA_EMAIL]
            mensagem = criar_mensagem(contato, modelo_html)
            servidor.sendmail(EMAIL_REMETENTE, email_destinatario, mensagem.as_string())
            print(f"E-mail enviado para {email_destinatario}")


def preparar_contatos_para_envio(contatos_csv: list[dict[str, str]]) -> list[dict[str, str]]:
    """Usa a lista completa ou cria uma lista de teste com apenas 1 destinatario."""
    if not MODO_TESTE:
        return contatos_csv

    print("MODO_TESTE ativo: sera enviado apenas 1 e-mail de teste.")
    return [{COLUNA_EMAIL: EMAIL_TESTE, COLUNA_NOME: "Teste"}]


def main() -> None:
    validar_configuracao()

    contatos_csv = carregar_contatos(ARQUIVO_CONTATOS)
    contatos_envio = preparar_contatos_para_envio(contatos_csv)
    modelo_html = ler_html(ARQUIVO_HTML)
    senha_de_app = SENHA_DE_APP.strip() or getpass("Digite a senha de app do Gmail: ")

    enviar_emails(contatos_envio, modelo_html, senha_de_app)
    print("Envio finalizado.")


if __name__ == "__main__":
    main()
