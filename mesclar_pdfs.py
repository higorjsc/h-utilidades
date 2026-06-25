from pathlib import Path
from pypdf import PdfWriter
import re


def extrair_numero(nome_arquivo):
    """
    Extrai o primeiro numero encontrado no nome do arquivo.
    Se nao houver numero, joga o arquivo para o final da lista.
    """
    match = re.search(r"\d+", nome_arquivo)
    return int(match.group()) if match else float("inf")


def listar_pdfs(pasta):
    pdfs = list(Path(pasta).glob("*.pdf"))

    pdfs.sort(key=lambda arquivo: (extrair_numero(arquivo.name), arquivo.name.lower()))

    return pdfs


def escolher_pdf(pdfs, mensagem):
    while True:
        try:
            escolha = int(input(mensagem))

            if 1 <= escolha <= len(pdfs):
                return pdfs[escolha - 1]

            print("Numero invalido. Tente novamente.")
        except ValueError:
            print("Digite apenas um numero, porque infelizmente o computador ainda exige isso.")


def mesclar_pdfs(pdf1, pdf2, saida):
    merger = PdfWriter()

    merger.append(str(pdf1))
    merger.append(str(pdf2))

    merger.write(str(saida))
    merger.close()


def main():
    pasta = Path(__file__).resolve().parent

    pdfs = listar_pdfs(pasta)

    if len(pdfs) < 2:
        print("A pasta precisa ter pelo menos dois arquivos PDF.")
        return

    print("\nArquivos PDF encontrados:\n")

    for i, pdf in enumerate(pdfs, start=1):
        print(f"{i}. {pdf.name}")

    print()

    primeiro_pdf = escolher_pdf(pdfs, "Selecione o primeiro PDF: ")
    segundo_pdf = escolher_pdf(pdfs, "Selecione o segundo PDF: ")

    nome_saida = input("Nome do PDF final [mesclado.pdf]: ").strip()

    if not nome_saida:
        nome_saida = "mesclado.pdf"

    if not nome_saida.lower().endswith(".pdf"):
        nome_saida += ".pdf"

    caminho_saida = pasta / nome_saida

    mesclar_pdfs(primeiro_pdf, segundo_pdf, caminho_saida)

    print(f"\nPDF gerado com sucesso: {caminho_saida}")


if __name__ == "__main__":
    main()
