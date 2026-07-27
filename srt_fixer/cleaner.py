#!/usr/bin/env python3
"""
Limpa, sequencialmente, todos os arquivos SRT da pasta de execução.

O script usa timestamps como delimitadores, portanto tolera cabeçalho WEBVTT,
índices ausentes, entradas coladas e linhas em branco irregulares. Para cada
entrada, remove música e descrições entre parênteses, colchetes, chaves ou
asteriscos, elimina entradas sem fala e reconstrói os índices.

Cada resultado é validado e gravado atomicamente como <nome>_limpo.srt.
Arquivos já limpos e saídas existentes não são processados novamente.

Uso:
    Abra o terminal na pasta das legendas e execute:
    python cleaner.py
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import List, Sequence, Tuple


START_WITH = ("#", "(", "[", "{", "♪", "*")
END_WITH = ("#", ")", "]", "}", "♪", "*")
BRACKETS = (("♪", "♪"), ("(", ")"), ("[", "]"), ("{", "}"), ("*", "*"))

# Por segurança, uma saída existente não é sobrescrita silenciosamente.
SOBRESCREVER_SAIDA = False

TIMESTAMP_RE = re.compile(
    r"^\s*"
    r"(?P<inicio>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<fim>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
    r"(?P<config>\s+.*?)?"
    r"\s*$"
)
INTEGER_RE = re.compile(r"^\d+$")
ONLY_SEPARATOR_RE = re.compile(r"^[\s\-–—_.·•]+$")

LOG = logging.getLogger("srt-cleaner")


class CleanerError(ValueError):
    """Erro que impede uma limpeza segura da legenda."""


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text_with_fallback(path: Path) -> Tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise CleanerError(f"Não foi possível decodificar {path.name}.")


def timestamp_to_ms(value: str, line_number: int) -> int:
    normalized = value.replace(".", ",")
    hh_text, mm_text, rest = normalized.split(":")
    ss_text, ms_text = rest.split(",")
    hh, mm, ss = int(hh_text), int(mm_text), int(ss_text)
    milliseconds = int(ms_text.ljust(3, "0"))
    if mm > 59 or ss > 59 or len(ms_text) > 3:
        raise CleanerError(
            f"Timestamp inválido na linha {line_number}: {value!r}."
        )
    return ((hh * 60 + mm) * 60 + ss) * 1000 + milliseconds


def reconstruct_blocks(content: str) -> List[str]:
    """Reconstrói blocos usando timestamps, sem depender de linhas vazias."""
    lines = normalize_newlines(content).split("\n")
    timestamps: List[Tuple[int, re.Match[str]]] = []
    invalid_arrow_lines: List[int] = []

    for position, line in enumerate(lines):
        match = TIMESTAMP_RE.fullmatch(line)
        if match:
            timestamps.append((position, match))
        elif "-->" in line:
            invalid_arrow_lines.append(position + 1)

    if invalid_arrow_lines:
        shown = ", ".join(map(str, invalid_arrow_lines[:10]))
        suffix = "..." if len(invalid_arrow_lines) > 10 else ""
        raise CleanerError(
            "Há linhas contendo '-->' que não são timestamps SRT válidos: "
            f"{shown}{suffix}."
        )
    if not timestamps:
        raise CleanerError("Nenhum timestamp SRT válido foi encontrado.")

    index_positions = set()
    for timestamp_position, _ in timestamps:
        candidate = timestamp_position - 1
        while candidate >= 0 and not lines[candidate].strip():
            candidate -= 1
        if candidate >= 0 and INTEGER_RE.fullmatch(lines[candidate].strip()):
            index_positions.add(candidate)

    prefix = [
        line.strip()
        for position, line in enumerate(lines[: timestamps[0][0]])
        if position not in index_positions and line.strip()
    ]
    if prefix:
        LOG.warning(
            "Texto fora de qualquer timestamp no início foi ignorado: %r",
            " | ".join(prefix)[:160],
        )

    blocks: List[str] = []
    for ordinal, (position, match) in enumerate(timestamps, start=1):
        next_position = (
            timestamps[ordinal][0] if ordinal < len(timestamps) else len(lines)
        )
        start_ms = timestamp_to_ms(match.group("inicio"), position + 1)
        end_ms = timestamp_to_ms(match.group("fim"), position + 1)
        if end_ms < start_ms:
            raise CleanerError(
                f"O timestamp da linha {position + 1} termina antes de começar."
            )

        text_lines = [
            lines[line_position]
            for line_position in range(position + 1, next_position)
            if line_position not in index_positions
        ]
        while text_lines and not text_lines[0].strip():
            text_lines.pop(0)
        while text_lines and not text_lines[-1].strip():
            text_lines.pop()

        timestamp = lines[position].strip()
        blocks.append(f"{ordinal}\n{timestamp}\n" + "\n".join(text_lines))
    return blocks


def only_spaces_or_hyphens(text: str) -> bool:
    return not text.strip() or bool(ONLY_SEPARATOR_RE.fullmatch(text))


def remove_between(blocks: Sequence[str], opening: str, closing: str) -> List[str]:
    if opening == closing:
        pattern = re.compile(
            re.escape(opening) + r"[^" + re.escape(closing) + r"]*"
            + re.escape(closing)
        )
    else:
        pattern = re.compile(
            re.escape(opening) + r"[^" + re.escape(closing) + r"]*"
            + re.escape(closing)
        )

    cleaned_blocks: List[str] = []
    for block in blocks:
        lines = block.split("\n")
        structural = lines[:2]
        dialogue: List[str] = []
        for line in lines[2:]:
            cleaned = pattern.sub("", line)
            cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
            if not only_spaces_or_hyphens(cleaned):
                dialogue.append(cleaned)
        cleaned_blocks.append("\n".join(structural + dialogue))
    return cleaned_blocks


def remove_marked_lines(blocks: Sequence[str]) -> List[str]:
    cleaned_blocks: List[str] = []
    for block in blocks:
        lines = block.split("\n")
        structural = lines[:2]
        dialogue = []
        for line in lines[2:]:
            cleaned = re.sub(r"[ \t]+", " ", line).strip()
            if not cleaned or only_spaces_or_hyphens(cleaned):
                continue
            if cleaned.startswith(START_WITH) or cleaned.endswith(END_WITH):
                continue
            dialogue.append(cleaned)
        cleaned_blocks.append("\n".join(structural + dialogue))
    return cleaned_blocks


def rebuild_indexes(blocks: Sequence[str]) -> List[str]:
    result: List[str] = []
    for block in blocks:
        lines = block.split("\n")
        dialogue = [line for line in lines[2:] if line.strip()]
        if not dialogue:
            continue
        result.append(
            "\n".join([str(len(result) + 1), lines[1]] + dialogue)
        )
    return result


def clean_content(content: str) -> Tuple[str, int, int]:
    blocks = reconstruct_blocks(content)
    original_count = len(blocks)
    for opening, closing in BRACKETS:
        blocks = remove_between(blocks, opening, closing)
    blocks = remove_marked_lines(blocks)
    blocks = rebuild_indexes(blocks)
    if not blocks:
        raise CleanerError(
            "Nenhuma fala permaneceu depois da limpeza; nada será gravado."
        )
    return "\n\n".join(blocks) + "\n", original_count, len(blocks)


def validate_cleaned_content(content: str, expected_count: int) -> None:
    blocks = normalize_newlines(content).strip().split("\n\n")
    if len(blocks) != expected_count:
        raise CleanerError(
            "A quantidade de entradas mudou durante a gravação."
        )
    for expected_index, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3 or lines[0] != str(expected_index):
            raise CleanerError(
                f"A entrada final {expected_index} está estruturalmente inválida."
            )
        if not TIMESTAMP_RE.fullmatch(lines[1]):
            raise CleanerError(
                f"O timestamp da entrada final {expected_index} é inválido."
            )


def atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.stem}_",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def clean_srt(path_in: Path | str, path_out: Path | str) -> Path:
    input_path = Path(path_in)
    output_path = Path(path_out)
    content, encoding = read_text_with_fallback(input_path)
    LOG.info(
        "Arquivo lido: %.1f KiB; codificação detectada: %s.",
        len(content.encode("utf-8")) / 1024,
        encoding,
    )
    result, entries_read, entries_kept = clean_content(content)
    validate_cleaned_content(result, entries_kept)
    atomic_write(output_path, result)
    LOG.info(
        "Limpeza: %d entradas lidas; %d mantidas; %d removidas.",
        entries_read,
        entries_kept,
        entries_read - entries_kept,
    )
    return output_path


# Mantém o nome público usado pelo script anterior.
def limpar_srt(path_in: Path | str, path_out: Path | str) -> None:
    clean_srt(path_in, path_out)


def find_inputs(folder: Path) -> List[Path]:
    candidates = sorted(
        (
            item
            for item in folder.iterdir()
            if item.is_file()
            and item.suffix.casefold() == ".srt"
            and not item.stem.casefold().endswith("_limpo")
        ),
        key=lambda path: path.name.casefold(),
    )
    if not candidates:
        raise CleanerError(
            "Nenhum arquivo .srt de entrada foi encontrado na pasta atual."
        )
    return candidates


def output_path_for(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_limpo.srt")


def run() -> Tuple[int, int, int]:
    folder = Path.cwd()
    LOG.info("Pasta de trabalho: %s", folder)
    LOG.info("Procurando arquivos SRT de entrada...")

    inputs = find_inputs(folder)
    pending: List[Path] = []
    skipped = 0
    for input_path in inputs:
        output_path = output_path_for(input_path)
        if output_path.exists() and not SOBRESCREVER_SAIDA:
            skipped += 1
            LOG.info(
                "Ignorando %s: a saída %s já existe.",
                input_path.name,
                output_path.name,
            )
        else:
            pending.append(input_path)

    LOG.info(
        "%d entrada(s) encontrada(s): %d pendente(s) e %d já concluída(s).",
        len(inputs),
        len(pending),
        skipped,
    )

    completed = 0
    failed = 0
    for number, input_path in enumerate(pending, start=1):
        LOG.info(
            "===== Arquivo %d/%d: %s =====",
            number,
            len(pending),
            input_path.name,
        )
        try:
            output_path = clean_srt(input_path, output_path_for(input_path))
            completed += 1
            LOG.info("Arquivo concluído: %s", output_path.name)
        except (OSError, CleanerError) as error:
            failed += 1
            LOG.error("Falha em %s: %s", input_path.name, error)
        except Exception:
            failed += 1
            LOG.exception("Falha inesperada em %s.", input_path.name)

    return completed, skipped, failed


def main() -> int:
    setup_logging()
    LOG.info("Iniciando limpeza sequencial de arquivos SRT.")
    try:
        completed, skipped, failed = run()
    except KeyboardInterrupt:
        LOG.error(
            "Processo interrompido pelo usuário; o arquivo em andamento "
            "não foi gravado."
        )
        return 130
    except (OSError, CleanerError) as error:
        LOG.error("%s", error)
        return 1
    except Exception:
        LOG.exception("Falha inesperada.")
        return 1

    LOG.info(
        "Resumo: %d arquivo(s) concluído(s), %d ignorado(s) e %d com falha.",
        completed,
        skipped,
        failed,
    )
    if failed:
        LOG.error("Processo finalizado com falhas.")
        return 1
    LOG.info("Processo finalizado com sucesso.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
