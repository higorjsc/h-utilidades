#!/usr/bin/env python3
"""
Limpa e traduz, sequencialmente, os arquivos SRT da pasta atual para português
brasileiro.

Fluxo:
1. Localiza todos os arquivos .srt de entrada na pasta de execução.
2. Reconstrói as entradas usando os timestamps como delimitadores.
3. Remove trechos musicais e descrições não verbais reconhecidas localmente.
4. Renumera os índices sequencialmente.
5. Pede ao DeepSeek que identifique o idioma e traduza apenas os textos.
6. Valida e grava imediatamente a tradução inicial em
   <nome>_traduzido.srt.
7. Faz uma revisão final, também pelo DeepSeek, sobre o arquivo gravado.
8. Substitui atomicamente o mesmo arquivo pela versão auditada e corrigida.
9. Repete o processo para o próximo arquivo somente após concluir o atual.

Uso:
    1. Cole uma chave nova em CHAVE_API ou defina DEEPSEEK_API_KEY.
    2. Coloque este script na pasta do SRT (ou abra o terminal nessa pasta).
    3. Execute: python clean_and_translate_srt.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Tuple)

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------------

# É possível colar a chave diretamente entre as aspas. Se esta variável ficar
# vazia, o script tentará usar a variável de ambiente DEEPSEEK_API_KEY.
# Não reutilize uma chave que já tenha sido publicada ou compartilhada.

CHAVE_API = os.getenv("DEEPSEEK_API_KEY")

if not CHAVE_API:
    raise RuntimeError(
        "A variável de ambiente DEEPSEEK_API_KEY não foi definida."
    )

URL_CHAT = "https://api.deepseek.com/chat/completions"
MODELO = "deepseek-v4-pro"

# O DeepSeek V4 aceita contextos muito maiores, mas lotes moderados são mais
# fáceis de validar e repetir quando uma resposta vem malformada.
# Lotes menores reduzem respostas truncadas, IDs omitidos e alterações de
# formatação. Se uma resposta ainda vier inválida, o lote será subdividido.
MAX_CARACTERES_POR_LOTE = 22_000
MAX_CARACTERES_POR_LOTE_REVISAO = 10_000
MAX_CARACTERES_AMOSTRA_IDIOMA = 30_000
MAX_TOKENS_TRADUCAO = 65_536
MAX_TOKENS_ANALISE = 4_096

# Limite total desejado para uma chamada. Se uma tradução ou auditoria exceder
# cinco minutos, o lote será subdividido para não permanecer indefinidamente em
# uma resposta grande. O watchdog também fecha uma resposta cujo corpo continue
# aberto depois desse limite.
TIMEOUT_REQUISICAO = 300
MAX_TENTATIVAS_HTTP = 5
MAX_TENTATIVAS_FORMATO = 3
MAX_CICLOS_DE_CORRECAO = 2
INTERVALO_LOG_ESPERA = 30

# Os lotes da revisão são independentes e podem ser auditados simultaneamente.
# Três solicitações preservam uma margem conservadora para os limites da API.
# A tradução inicial permanece sequencial para aproveitar o contexto já
# traduzido dos lotes anteriores.
MAX_TRABALHADORES_REVISAO = 3

# Versão do checkpoint que diferencia uma tradução inicial de uma saída que já
# passou pela auditoria final.
VERSAO_CHECKPOINT_AUDITORIA = 1

# Além de música, remove descrições autônomas claramente não verbais, como
# "[risos]" e "(porta batendo)". Falas entre parênteses são preservadas.
REMOVER_DESCRICOES_NAO_VERBAIS = True

# Por segurança, um resultado existente não é sobrescrito silenciosamente.
SOBRESCREVER_SAIDA = False

LOG = logging.getLogger("srt")


# ---------------------------------------------------------------------------
# MODELOS E EXPRESSÕES REGULARES
# ---------------------------------------------------------------------------

TIMESTAMP_RE = re.compile(
    r"^\s*"
    r"(?P<inicio>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<fim>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
    r"(?P<config>\s+.*?)?"
    r"\s*$"
)

MUSIC_SYMBOL_RE = re.compile(r"[♪♫♬♩♭♮♯🎵🎶🎼🎤]")
HASH_LYRIC_RE = re.compile(r"^\s*#+\s*.+?\s*#+\s*$")
SINGING_PREFIX_RE = re.compile(
    r"^\s*(?:singer|singing|sings|cantando|cantor|cantora|"
    r"chant|chanting|choir|chorus|coro)\s*:",
    flags=re.IGNORECASE,
)
ONLY_SEPARATOR_RE = re.compile(r"^[\s\-–—_.·•]+$")
INTEGER_RE = re.compile(r"^\d+$")

# Tags que devem sobreviver literalmente à tradução.
FORMATTING_TAG_RE = re.compile(
    r"</?(?:i|b|u|font|span)\b[^>]*>|"
    r"\{\\[^{}\r\n]+\}",
    flags=re.IGNORECASE,
)

BRACKET_SPAN_RE = re.compile(
    r"\[[^\]\r\n]{1,160}\]|"
    r"\([^)\r\n]{1,160}\)|"
    r"\{(?!\\)[^}\r\n]{1,160}\}|"
    r"\*[^*\r\n]{1,160}\*"
)

FULL_BRACKET_RE = re.compile(
    r"^\s*(?:"
    r"\[[^\]\r\n]+\]|"
    r"\([^)\r\n]+\)|"
    r"\{(?!\\)[^}\r\n]+\}|"
    r"\*[^*\r\n]+\*"
    r")\s*[.!?…]?\s*$"
)

MUSIC_TERMS = {
    "music",
    "musica",
    "musique",
    "musik",
    "musikk",
    "muziek",
    "muzyka",
    "song",
    "songs",
    "cancion",
    "canciones",
    "chanson",
    "chansons",
    "lied",
    "singing",
    "sings",
    "singer",
    "cantando",
    "cantam",
    "canto",
    "chant",
    "chanting",
    "humming",
    "hums",
    "tarareando",
    "instrumental",
    "melody",
    "melodia",
    "melodie",
    "soundtrack",
    "karaoke",
    "choir",
    "chorus",
    "refrain",
    "lyrics",
    "lyric",
    "bgm",
}

MUSIC_PHRASES = {
    "theme song",
    "opening theme",
    "ending theme",
    "background music",
    "background score",
    "musica de fundo",
    "musica tocando",
    "song playing",
    "music playing",
    "continua a musica",
    "continues singing",
}

NONVERBAL_TERMS = {
    "applause",
    "aplausos",
    "clapping",
    "laugh",
    "laughs",
    "laughing",
    "laughter",
    "risos",
    "rindo",
    "ri",
    "chuckles",
    "chuckling",
    "giggles",
    "sigh",
    "sighs",
    "sighing",
    "suspiro",
    "suspira",
    "gasps",
    "gasping",
    "ofegante",
    "groans",
    "gemido",
    "cough",
    "coughs",
    "coughing",
    "tosse",
    "sneeze",
    "sneezes",
    "espirro",
    "crying",
    "sobbing",
    "chorando",
    "grunts",
    "grunting",
    "door",
    "porta",
    "phone",
    "telefone",
    "footsteps",
    "passos",
    "thunder",
    "trovao",
    "explosion",
    "explosao",
    "gunshot",
    "gunshots",
    "tiro",
    "tiros",
    "alarm",
    "alarme",
    "bell",
    "campainha",
    "wind",
    "vento",
    "engine",
    "motor",
    "silence",
    "silencio",
}

MELODIC_SYLLABLES = {
    "ah",
    "aah",
    "la",
    "lalala",
    "na",
    "nanana",
    "oh",
    "ooh",
    "uh",
    "yeah",
    "hey",
    "woah",
    "whoa",
    "doo",
    "dum",
    "da",
}

DIALOGUE_PRONOUNS = {
    "i",
    "you",
    "he",
    "she",
    "we",
    "they",
    "me",
    "my",
    "your",
    "eu",
    "tu",
    "voce",
    "ele",
    "ela",
    "nos",
    "meu",
    "minha",
    "seu",
    "sua",
    "yo",
    "tu",
    "usted",
    "el",
    "ella",
    "nosotros",
    "je",
    "tu",
    "il",
    "elle",
    "nous",
    "vous",
}


@dataclass
class Cue:
    """Uma entrada SRT já separada da estrutura que a IA não pode alterar."""

    source_index: Optional[int]
    timestamp: str
    start_ms: int
    end_ms: int
    text: str
    line_number: int


@dataclass
class CleaningStats:
    cues_read: int = 0
    cues_kept: int = 0
    cues_without_text: int = 0
    music_lines_removed: int = 0
    music_cues_removed: int = 0
    nonverbal_lines_removed: int = 0
    indexes_rebuilt: int = 0


class SRTError(ValueError):
    """Erro de estrutura que impede uma reconstrução segura do SRT."""


class DeepSeekError(RuntimeError):
    """Erro de comunicação ou de resposta da API."""


class DeepSeekFormatError(DeepSeekError):
    """Resposta recebida, mas incompatível com o formato solicitado."""


class DeepSeekRequestTimeoutError(DeepSeekFormatError):
    """A chamada ultrapassou o limite total e o lote deve ser subdividido."""


class DeepSeekRequestDeadline(TimeoutError):
    """Sinal interno de que o prazo total da chamada terminou."""


# ---------------------------------------------------------------------------
# UTILITÁRIOS DE TEXTO
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalized_for_detection(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def words_for_detection(text: str) -> List[str]:
    return normalized_for_detection(text).split()


def contains_terms(text: str, terms: Iterable[str]) -> bool:
    normalized = f" {normalized_for_detection(text)} "
    return any(f" {term} " in normalized for term in terms)


def clean_visible_line(line: str) -> str:
    line = line.replace("\ufeff", "").replace("\u200b", "")
    line = re.sub(r"[ \t]+", " ", line).strip()
    return line


def strip_markdown_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def normalize_ai_text(text: str) -> str:
    text = normalize_newlines(text).strip()
    lines = [clean_visible_line(line) for line in text.split("\n")]
    # Linhas vazias dentro de uma entrada criariam um novo bloco SRT.
    return "\n".join(line for line in lines if line)


def formatting_signature(text: str) -> Tuple[str, ...]:
    return tuple(FORMATTING_TAG_RE.findall(text))


def protect_formatting(text: str) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    replacements: List[Tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        token = f"__SRT_FORMAT_TAG_{len(replacements):03d}__"
        replacements.append((token, match.group(0)))
        return token

    return FORMATTING_TAG_RE.sub(replace, text), tuple(replacements)


def restore_formatting(
    text: str,
    replacements: Sequence[Tuple[str, str]],
    cue_id: int,
) -> str:
    restored = text
    for token, tag in replacements:
        if restored.count(token) != 1:
            raise DeepSeekFormatError(
                f"O marcador protegido {token} da entrada {cue_id} "
                "não foi preservado exatamente uma vez."
            )
        restored = restored.replace(token, tag)
    if "__SRT_FORMAT_TAG_" in restored:
        raise DeepSeekFormatError(
            f"A entrada {cue_id} contém marcador de formatação desconhecido."
        )
    return restored


# ---------------------------------------------------------------------------
# LEITURA E RECONSTRUÇÃO SEGURA DO SRT
# ---------------------------------------------------------------------------

def read_text_with_fallback(path: Path) -> Tuple[str, str]:
    raw = path.read_bytes()
    encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # latin-1 decodifica qualquer sequência de bytes; este ponto é defensivo.
    raise SRTError(f"Não foi possível decodificar {path.name}.")


def timestamp_to_ms(value: str, line_number: int) -> int:
    normalized = value.replace(".", ",")
    hh_text, mm_text, rest = normalized.split(":")
    ss_text, ms_text = rest.split(",")
    hh, mm, ss = int(hh_text), int(mm_text), int(ss_text)
    milliseconds = int(ms_text.ljust(3, "0"))

    if mm > 59 or ss > 59 or len(ms_text) > 3:
        raise SRTError(
            f"Timestamp inválido na linha {line_number}: {value!r}."
        )
    return ((hh * 60 + mm) * 60 + ss) * 1000 + milliseconds


def parse_srt(content: str) -> List[Cue]:
    """
    Usa cada timestamp como âncora, em vez de confiar em linhas em branco.

    Isso recupera entradas coladas, índices ausentes e blocos contendo mais de
    um timestamp. Linhas com '-->' que não sejam timestamps válidos provocam
    erro: descartá-las silenciosamente seria a receita clássica para corromper
    uma legenda fingindo que tudo deu certo.
    """
    lines = normalize_newlines(content).split("\n")
    parsed_timestamps: List[Tuple[int, re.Match[str]]] = []
    invalid_arrow_lines: List[int] = []

    for position, line in enumerate(lines):
        match = TIMESTAMP_RE.fullmatch(line)
        if match:
            parsed_timestamps.append((position, match))
        elif "-->" in line:
            invalid_arrow_lines.append(position + 1)

    if invalid_arrow_lines:
        shown = ", ".join(map(str, invalid_arrow_lines[:10]))
        suffix = "..." if len(invalid_arrow_lines) > 10 else ""
        raise SRTError(
            "Há linhas contendo '-->' que não são timestamps SRT válidos: "
            f"{shown}{suffix}."
        )

    if not parsed_timestamps:
        raise SRTError("Nenhum timestamp SRT válido foi encontrado.")

    index_line_positions = set()
    source_indexes: Dict[int, int] = {}
    for timestamp_position, _ in parsed_timestamps:
        candidate_position = timestamp_position - 1
        # Tolera linhas vazias indevidas entre o índice e o timestamp.
        while candidate_position >= 0 and not lines[candidate_position].strip():
            candidate_position -= 1
        if candidate_position < 0:
            continue
        candidate = lines[candidate_position].strip()
        if INTEGER_RE.fullmatch(candidate):
            index_line_positions.add(candidate_position)
            source_indexes[timestamp_position] = int(candidate)

    prefix_nonempty = [
        line.strip()
        for pos, line in enumerate(lines[: parsed_timestamps[0][0]])
        if pos not in index_line_positions and line.strip()
    ]
    if prefix_nonempty:
        LOG.warning(
            "Texto fora de qualquer timestamp no início foi ignorado: %r",
            " | ".join(prefix_nonempty)[:160],
        )

    cues: List[Cue] = []
    for ordinal, (timestamp_position, match) in enumerate(parsed_timestamps):
        next_position = (
            parsed_timestamps[ordinal + 1][0]
            if ordinal + 1 < len(parsed_timestamps)
            else len(lines)
        )

        text_lines = [
            lines[pos]
            for pos in range(timestamp_position + 1, next_position)
            if pos not in index_line_positions
        ]
        while text_lines and not text_lines[0].strip():
            text_lines.pop(0)
        while text_lines and not text_lines[-1].strip():
            text_lines.pop()

        start_ms = timestamp_to_ms(
            match.group("inicio"), timestamp_position + 1
        )
        end_ms = timestamp_to_ms(match.group("fim"), timestamp_position + 1)
        if end_ms < start_ms:
            raise SRTError(
                f"O timestamp da linha {timestamp_position + 1} termina "
                "antes de começar."
            )

        cues.append(
            Cue(
                source_index=source_indexes.get(timestamp_position),
                timestamp=lines[timestamp_position].strip(),
                start_ms=start_ms,
                end_ms=end_ms,
                text="\n".join(text_lines).strip(),
                line_number=timestamp_position + 1,
            )
        )

    return cues


def is_music_descriptor(text: str) -> bool:
    normalized = normalized_for_detection(text)
    if not normalized:
        return False
    if contains_terms(normalized, MUSIC_TERMS):
        return True
    padded = f" {normalized} "
    return any(f" {phrase} " in padded for phrase in MUSIC_PHRASES)


def strip_music_descriptors(line: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return "" if is_music_descriptor(match.group(0)) else match.group(0)

    return clean_visible_line(BRACKET_SPAN_RE.sub(replace, line))


def looks_like_repeated_singing(line: str) -> bool:
    plain = FORMATTING_TAG_RE.sub("", line)
    words = words_for_detection(plain)
    if len(words) < 5:
        return False
    recognized = [word for word in words if word in MELODIC_SYLLABLES]
    return len(recognized) / len(words) >= 0.8 and len(set(words)) <= 4


def is_music_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if MUSIC_SYMBOL_RE.search(stripped):
        return True
    if HASH_LYRIC_RE.fullmatch(stripped):
        return True
    if SINGING_PREFIX_RE.match(stripped):
        return True
    if looks_like_repeated_singing(stripped):
        return True

    # Descritores musicais são removidos apenas quando a linha inteira parece
    # uma rubrica. Assim, "I love this song" não desaparece por excesso de zelo.
    detected_words = words_for_detection(stripped)
    looks_like_dialogue = any(
        word in DIALOGUE_PRONOUNS for word in detected_words
    )
    descriptor_shape = (
        bool(FULL_BRACKET_RE.fullmatch(stripped))
        and not looks_like_dialogue
    )
    short_stage_label = (
        len(detected_words) <= 10
        and not looks_like_dialogue
        and (
            stripped.isupper()
            or stripped.endswith(":")
            or stripped.startswith(("[", "(", "{", "*"))
        )
    )
    normalized = normalized_for_detection(stripped)
    bare_descriptor = (
        normalized in MUSIC_TERMS
        or normalized in MUSIC_PHRASES
        or (
            len(normalized.split()) <= 7
            and any(
                qualifier in normalized.split()
                for qualifier in (
                    "playing",
                    "plays",
                    "continues",
                    "starts",
                    "stops",
                    "fades",
                    "tocando",
                    "continua",
                    "comeca",
                    "termina",
                    "suave",
                    "dramatic",
                    "dramatica",
                    "soft",
                    "loud",
                    "upbeat",
                )
            )
        )
    )
    return is_music_descriptor(stripped) and (
        descriptor_shape or short_stage_label or bare_descriptor
    )


def is_nonverbal_line(line: str) -> bool:
    if not REMOVER_DESCRICOES_NAO_VERBAIS:
        return False
    stripped = line.strip()
    if not FULL_BRACKET_RE.fullmatch(stripped):
        return False
    return contains_terms(stripped, NONVERBAL_TERMS)


def clean_cue_text(text: str, stats: CleaningStats) -> str:
    cleaned_lines: List[str] = []
    for original_line in normalize_newlines(text).split("\n"):
        line = clean_visible_line(original_line)
        if not line or ONLY_SEPARATOR_RE.fullmatch(line):
            continue
        if is_music_line(line):
            stats.music_lines_removed += 1
            continue

        line = strip_music_descriptors(line)
        if not line or ONLY_SEPARATOR_RE.fullmatch(line):
            stats.music_lines_removed += 1
            continue
        if is_nonverbal_line(line):
            stats.nonverbal_lines_removed += 1
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def clean_cues(cues: Sequence[Cue]) -> Tuple[List[Cue], CleaningStats]:
    stats = CleaningStats(cues_read=len(cues))
    cleaned: List[Cue] = []

    for cue in cues:
        new_text = clean_cue_text(cue.text, stats)
        if not new_text:
            stats.cues_without_text += 1
            if cue.text.strip() and (
                MUSIC_SYMBOL_RE.search(cue.text)
                or is_music_descriptor(cue.text)
                or looks_like_repeated_singing(cue.text)
            ):
                stats.music_cues_removed += 1
            continue

        cleaned.append(
            Cue(
                source_index=cue.source_index,
                timestamp=cue.timestamp,
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                text=new_text,
                line_number=cue.line_number,
            )
        )

    stats.cues_kept = len(cleaned)
    stats.indexes_rebuilt = sum(
        cue.source_index != expected
        for expected, cue in enumerate(cleaned, start=1)
    )
    return cleaned, stats


def warn_about_timeline(cues: Sequence[Cue]) -> None:
    regressions = 0
    overlaps = 0
    previous: Optional[Cue] = None
    for cue in cues:
        if previous is not None:
            if cue.start_ms < previous.start_ms:
                regressions += 1
            if cue.start_ms < previous.end_ms:
                overlaps += 1
        previous = cue

    if regressions:
        LOG.warning(
            "%d entrada(s) começam antes da entrada anterior. "
            "A ordem e os timestamps foram preservados.",
            regressions,
        )
    if overlaps:
        LOG.info(
            "Linha do tempo: %d sobreposição(ões) detectada(s); "
            "nenhuma foi alterada.",
            overlaps,
        )


def render_srt(cues: Sequence[Cue], texts: Mapping[int, str]) -> str:
    blocks: List[str] = []
    for output_index, cue in enumerate(cues, start=1):
        text = normalize_ai_text(texts[output_index])
        if not text:
            raise SRTError(
                f"A entrada {output_index} ficou vazia antes da gravação."
            )
        blocks.append(f"{output_index}\n{cue.timestamp}\n{text}")
    return "\n\n".join(blocks) + "\n"


def validate_rendered_srt(
    content: str,
    original_cues: Sequence[Cue],
    expected_texts: Mapping[int, str],
) -> None:
    reparsed = parse_srt(content)
    if len(reparsed) != len(original_cues):
        raise SRTError(
            "A validação final encontrou quantidade diferente de entradas."
        )

    for index, (expected, actual) in enumerate(
        zip(original_cues, reparsed), start=1
    ):
        if actual.source_index != index:
            raise SRTError(
                f"O índice final {index} não foi reconstruído corretamente."
            )
        if actual.timestamp != expected.timestamp:
            raise SRTError(
                f"O timestamp da entrada {index} mudou durante o processo."
            )
        if actual.text != normalize_ai_text(expected_texts[index]):
            raise SRTError(
                f"O texto da entrada {index} mudou durante a serialização."
            )


# ---------------------------------------------------------------------------
# CLIENTE DEEPSEEK
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    key = CHAVE_API.strip() or os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key or key.startswith("COLE_"):
        raise DeepSeekError(
            "Informe uma chave DeepSeek nova em CHAVE_API ou na variável "
            "DEEPSEEK_API_KEY."
        )
    return key


def retry_delay(
    response_headers: Optional[Mapping[str, str]], attempt: int
) -> float:
    if response_headers is not None:
        retry_after = response_headers.get("Retry-After", "").strip()
        try:
            if retry_after:
                return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return min(2 ** (attempt - 1) + random.uniform(0.0, 0.8), 30.0)


def log_request_heartbeat(
    stop_event: threading.Event, operation: str, started_at: float
) -> None:
    while not stop_event.wait(INTERVALO_LOG_ESPERA):
        elapsed = time.monotonic() - started_at
        remaining = max(0.0, TIMEOUT_REQUISICAO - elapsed)
        LOG.info(
            "%s: aguardando resposta da API há %.0fs; processo ativo; "
            "limite deste pedido em aproximadamente %.0fs.",
            operation,
            elapsed,
            remaining,
        )


def read_response_with_deadline(
    response: Any,
    *,
    deadline: float,
    operation: str,
) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeepSeekRequestDeadline(
            f"{operation} excedeu {TIMEOUT_REQUISICAO}s antes de receber "
            "o corpo da resposta."
        )

    expired = threading.Event()

    def close_on_deadline() -> None:
        expired.set()
        response_socket = getattr(
            getattr(getattr(response, "fp", None), "raw", None),
            "_sock",
            None,
        )
        if response_socket is not None:
            try:
                response_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            response.close()
        except OSError:
            pass

    watchdog = threading.Timer(remaining, close_on_deadline)
    watchdog.daemon = True
    watchdog.name = "deepseek-request-deadline"
    watchdog.start()
    try:
        try:
            body = response.read()
        except (OSError, ValueError) as error:
            if expired.is_set():
                raise DeepSeekRequestDeadline(
                    f"{operation} excedeu o limite total de "
                    f"{TIMEOUT_REQUISICAO}s."
                ) from error
            raise
        if expired.is_set() or time.monotonic() >= deadline:
            raise DeepSeekRequestDeadline(
                f"{operation} excedeu o limite total de "
                f"{TIMEOUT_REQUISICAO}s."
            )
        return body.decode("utf-8")
    finally:
        watchdog.cancel()


class DeepSeekClient:
    def __init__(self, api_key: str) -> None:
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "clean-and-translate-srt/1.0",
        }
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._usage_lock = threading.Lock()

    def close(self) -> None:
        # Mantém a mesma interface caso o transporte seja trocado no futuro.
        return None

    def request_json(
        self,
        *,
        system_prompt: str,
        user_data: Mapping[str, Any],
        operation: str,
        max_tokens: int,
        thinking: bool = False,
        reasoning_effort: str = "high",
    ) -> Dict[str, Any]:
        payload = {
            "model": MODELO,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        user_data, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            ],
            "thinking": {
                "type": "enabled" if thinking else "disabled"
            },
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        if thinking:
            payload["reasoning_effort"] = reasoning_effort
        else:
            payload["temperature"] = 0.0

        retryable_statuses = {408, 409, 429, 500, 502, 503, 504}
        last_error: Optional[BaseException] = None

        for attempt in range(1, MAX_TENTATIVAS_HTTP + 1):
            response_headers: Optional[Mapping[str, str]] = None
            response_status: Optional[int] = None
            started_at = time.monotonic()
            deadline = started_at + TIMEOUT_REQUISICAO
            deadline_reached = False
            stop_heartbeat = threading.Event()
            heartbeat = threading.Thread(
                target=log_request_heartbeat,
                args=(stop_heartbeat, operation, started_at),
                daemon=True,
                name="deepseek-heartbeat",
            )
            try:
                LOG.info(
                    "%s: enviando solicitação à API%s "
                    "(tentativa HTTP %d/%d)...",
                    operation,
                    (
                        f" com reasoning {reasoning_effort}"
                        if thinking
                        else ""
                    ),
                    attempt,
                    MAX_TENTATIVAS_HTTP,
                )
                heartbeat.start()
                request = urllib.request.Request(
                    URL_CHAT,
                    data=json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8"),
                    headers=self.headers,
                    method="POST",
                )
                with urllib.request.urlopen(
                    request,
                    timeout=max(0.1, deadline - time.monotonic()),
                ) as response:
                    response_status = response.status
                    response_headers = response.headers
                    raw_body = read_response_with_deadline(
                        response,
                        deadline=deadline,
                        operation=operation,
                    )

                LOG.info(
                    "%s: resposta recebida em %.1fs; validando...",
                    operation,
                    time.monotonic() - started_at,
                )
                body = json.loads(raw_body)
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise DeepSeekFormatError(
                        f"A resposta de {operation} atingiu o limite de tokens."
                    )
                content = choice["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise DeepSeekFormatError(
                        f"A resposta de {operation} veio vazia."
                    )

                usage = body.get("usage") or {}
                with self._usage_lock:
                    self.prompt_tokens += int(
                        usage.get("prompt_tokens") or 0
                    )
                    self.completion_tokens += int(
                        usage.get("completion_tokens") or 0
                    )

                parsed = json.loads(strip_markdown_fence(content))
                if not isinstance(parsed, dict):
                    raise DeepSeekFormatError(
                        f"A resposta JSON de {operation} não é um objeto."
                    )
                return parsed

            except urllib.error.HTTPError as error:
                response_status = error.code
                response_headers = error.headers
                try:
                    details = error.read().decode(
                        "utf-8", errors="replace"
                    )[:800].replace("\n", " ")
                except OSError:
                    details = str(error)

                if response_status == 401:
                    raise DeepSeekError(
                        "Chave de API inválida, revogada ou sem autorização."
                    ) from error
                if response_status == 402:
                    raise DeepSeekError(
                        "A conta DeepSeek está sem créditos suficientes."
                    ) from error
                if response_status not in retryable_statuses:
                    raise DeepSeekError(
                        f"A DeepSeek retornou HTTP {response_status}: "
                        f"{details}"
                    ) from error
                last_error = DeepSeekError(
                    f"HTTP {response_status}: {details[:300]}"
                )
            except DeepSeekRequestDeadline as error:
                last_error = error
                deadline_reached = True
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
            ) as error:
                if time.monotonic() >= deadline:
                    last_error = DeepSeekRequestDeadline(
                        f"{operation} excedeu o limite total de "
                        f"{TIMEOUT_REQUISICAO}s."
                    )
                    deadline_reached = True
                else:
                    last_error = error
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
                KeyError,
                IndexError,
                TypeError,
            ) as error:
                last_error = DeepSeekFormatError(
                    f"Resposta malformada em {operation}: {error}"
                )
            except DeepSeekError as error:
                last_error = error
                raise
            finally:
                stop_heartbeat.set()
                if heartbeat.is_alive():
                    heartbeat.join(timeout=1.0)

            if deadline_reached:
                LOG.warning(
                    "%s ultrapassou o limite total de %ds. A etapa chamadora "
                    "tentará recuperar o trabalho com um lote menor.",
                    operation,
                    TIMEOUT_REQUISICAO,
                )
                break
            if attempt == MAX_TENTATIVAS_HTTP:
                break
            wait = retry_delay(response_headers, attempt)
            LOG.warning(
                "%s falhou (tentativa %d/%d): %s. Nova tentativa em %.1fs.",
                operation,
                attempt,
                MAX_TENTATIVAS_HTTP,
                str(last_error)[:260],
                wait,
            )
            time.sleep(wait)

        message = (
            f"{operation} falhou na tentativa {attempt}/"
            f"{MAX_TENTATIVAS_HTTP}: {last_error}"
        )
        if isinstance(last_error, DeepSeekRequestDeadline):
            raise DeepSeekRequestTimeoutError(message)
        if isinstance(last_error, DeepSeekFormatError):
            raise DeepSeekFormatError(message)
        raise DeepSeekError(message)


# ---------------------------------------------------------------------------
# ANÁLISE, TRADUÇÃO E REVISÃO
# ---------------------------------------------------------------------------

def representative_sample(
    cues: Sequence[Cue], max_characters: int
) -> List[Dict[str, Any]]:
    if not cues:
        return []

    total = sum(len(cue.text) for cue in cues)
    if total <= max_characters:
        selected = list(range(len(cues)))
    else:
        # Amostra distribuída por todo o arquivo, não apenas pelos créditos
        # iniciais ou por uma única cena.
        average_size = max(1, total // len(cues))
        target_count = min(
            len(cues),
            220,
            max(4, max_characters // (average_size + 40)),
        )
        if target_count == 1:
            selected = [0]
        else:
            selected = sorted(
                {
                    round(i * (len(cues) - 1) / (target_count - 1))
                    for i in range(target_count)
                }
            )

    result: List[Dict[str, Any]] = []
    used = 0
    for position in selected:
        text = cues[position].text
        if result and used + len(text) > max_characters:
            continue
        if not result and len(text) > max_characters:
            text = text[:max_characters]
        result.append({"id": position + 1, "text": text})
        used += len(text)
    return result


def analyze_language_and_context(
    client: DeepSeekClient, cues: Sequence[Cue]
) -> Dict[str, Any]:
    system_prompt = """
Você analisa legendas antes de uma tradução profissional.
Identifique o idioma predominante do texto, inclusive sua variante regional,
e resuma o contexto necessário para manter coerência entre lotes. O material
pode conter falas intencionalmente em outros idiomas.

Responda SOMENTE como um objeto JSON neste formato:
{
  "language_name": "nome do idioma em português",
  "language_code": "código curto",
  "is_portuguese": false,
  "regional_variant": "variante ou desconhecida",
  "content_summary": "resumo curto, sem traduzir as falas",
  "register": "registro, época e nível de informalidade",
  "proper_names": ["nomes próprios, empresas e marcas reconhecidos"],
  "preserve_terms": ["estrangeirismos ou termos técnicos que devem permanecer"]
}

"is_portuguese" só deve ser true se as falas já estiverem
predominantemente em português. Não trate nomes próprios, marcas,
estrangeirismos estabelecidos ou expressões deliberadamente estrangeiras como
prova de que o idioma predominante é outro.
""".strip()

    user_data: Dict[str, Any] = {
        "task": "analyze_language_and_context",
        "sample": representative_sample(
            cues, MAX_CARACTERES_AMOSTRA_IDIOMA
        ),
    }
    last_error: Optional[BaseException] = None

    for format_attempt in range(1, MAX_TENTATIVAS_FORMATO + 1):
        if format_attempt > 1:
            user_data["strict_retry"] = (
                "A resposta anterior não seguiu o esquema. Retorne todos os "
                "campos solicitados com os tipos JSON corretos."
            )
        try:
            data = client.request_json(
                system_prompt=system_prompt,
                user_data=user_data,
                operation="Análise de idioma",
                max_tokens=MAX_TOKENS_ANALISE,
            )

            required_strings = (
                "language_name",
                "language_code",
                "regional_variant",
                "content_summary",
                "register",
            )
            if any(
                not isinstance(data.get(key), str)
                for key in required_strings
            ):
                raise DeepSeekError(
                    "A análise de idioma não retornou todos os campos "
                    "esperados."
                )
            if not isinstance(data.get("is_portuguese"), bool):
                raise DeepSeekError(
                    "A análise de idioma não retornou is_portuguese como "
                    "booleano."
                )
            for key in ("proper_names", "preserve_terms"):
                if not isinstance(data.get(key), list):
                    data[key] = []
                data[key] = [
                    item[:160]
                    for item in data[key]
                    if isinstance(item, str) and item.strip()
                ][:100]
            return data
        except DeepSeekError as error:
            last_error = error
            if format_attempt < MAX_TENTATIVAS_FORMATO:
                LOG.warning(
                    "Análise de idioma inválida (%d/%d): %s",
                    format_attempt,
                    MAX_TENTATIVAS_FORMATO,
                    error,
                )

    raise DeepSeekError(
        f"A análise de idioma permaneceu inválida: {last_error}"
    )


def build_batches(
    cues: Sequence[Cue], max_characters: int
) -> List[List[int]]:
    batches: List[List[int]] = []
    current: List[int] = []
    current_size = 0

    for cue_id, cue in enumerate(cues, start=1):
        estimated = len(cue.text) + 40
        if current and current_size + estimated > max_characters:
            batches.append(current)
            current = []
            current_size = 0
        current.append(cue_id)
        current_size += estimated

    if current:
        batches.append(current)
    return batches


def compact_analysis(analysis: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "source_language": analysis["language_name"],
        "language_code": analysis["language_code"],
        "regional_variant": analysis["regional_variant"],
        "content_summary": analysis["content_summary"],
        "register": analysis["register"],
        "known_proper_names": analysis.get("proper_names", []),
        "preserve_terms": analysis.get("preserve_terms", []),
    }


def validate_translation_response(
    response: Mapping[str, Any],
    batch_ids: Sequence[int],
    source_texts: Mapping[int, str],
    formatting_replacements: Mapping[
        int, Sequence[Tuple[str, str]]
    ],
) -> Dict[int, str]:
    translations = response.get("translations")
    if not isinstance(translations, dict):
        raise DeepSeekFormatError(
            "A resposta não contém o objeto 'translations'."
        )

    expected_keys = {str(cue_id) for cue_id in batch_ids}
    actual_keys = {str(key) for key in translations.keys()}
    if actual_keys != expected_keys:
        if len(batch_ids) == 1 and len(translations) == 1:
            only_value = next(iter(translations.values()))
            translations = {str(batch_ids[0]): only_value}
            actual_keys = expected_keys
            LOG.warning(
                "A API devolveu uma chave incorreta para a entrada única %d; "
                "o único valor recebido será validado e aproveitado.",
                batch_ids[0],
            )
        else:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise DeepSeekFormatError(
                f"IDs incorretos na tradução; ausentes={missing[:8]}, "
                f"extras={extra[:8]}."
            )

    validated: Dict[int, str] = {}
    for cue_id in batch_ids:
        value = translations.get(str(cue_id))
        if not isinstance(value, str):
            # Alguns serializadores podem devolver chaves inteiras.
            value = translations.get(cue_id)  # type: ignore[arg-type]
        if not isinstance(value, str):
            raise DeepSeekFormatError(
                f"A tradução da entrada {cue_id} não é texto."
            )
        value = restore_formatting(
            value, formatting_replacements[cue_id], cue_id
        )
        value = normalize_ai_text(value)
        if not value:
            raise DeepSeekFormatError(
                f"A tradução da entrada {cue_id} ficou vazia."
            )
        if len(value) > len(source_texts[cue_id]) * 6 + 500:
            raise DeepSeekFormatError(
                f"A entrada {cue_id} expandiu de forma anormal."
            )
        if formatting_signature(value) != formatting_signature(
            source_texts[cue_id]
        ):
            raise DeepSeekFormatError(
                f"As tags de formatação da entrada {cue_id} foram alteradas."
            )
        validated[cue_id] = value
    return validated


def request_translation_chunk(
    client: DeepSeekClient,
    *,
    batch_number: int,
    batch_ids: Sequence[int],
    cues: Sequence[Cue],
    translated: Mapping[int, str],
    analysis: Mapping[str, Any],
) -> Dict[int, str]:
    source_texts = {
        cue_id: cues[cue_id - 1].text for cue_id in batch_ids
    }
    protected_items = {
        cue_id: protect_formatting(source_texts[cue_id])
        for cue_id in batch_ids
    }
    protected_texts = {
        cue_id: protected_items[cue_id][0] for cue_id in batch_ids
    }
    formatting_replacements = {
        cue_id: protected_items[cue_id][1] for cue_id in batch_ids
    }
    first_id, last_id = batch_ids[0], batch_ids[-1]
    before_ids = range(max(1, first_id - 4), first_id)
    after_ids = range(last_id + 1, min(len(cues), last_id + 4) + 1)

    system_prompt = """
Você é um tradutor profissional de legendas para português brasileiro.
Traduza cada valor de "cues" com naturalidade, coerência narrativa e registro
compatível com o contexto fornecido.

Regras obrigatórias:
- Retorne SOMENTE JSON: {"translations":{"ID":"texto traduzido"}}.
- Retorne exatamente os mesmos IDs recebidos, uma única vez cada.
- Traduza apenas os valores textuais; nunca invente novas entradas.
- Preserve literalmente todas as tags HTML/ASS, como <i> e {\\an8}.
- Preserve literalmente marcadores como __SRT_FORMAT_TAG_000__; eles
  representam tags protegidas e nunca devem ser traduzidos, removidos,
  duplicados, renumerados ou modificados.
- Preserve nomes próprios de pessoas, lugares, empresas, produtos e marcas.
- Preserve estrangeirismos consagrados, siglas e termos técnicos que
  normalmente permanecem no idioma original.
- Traduza expressões estrangeiras comuns quando não forem nomes, marcas,
  estrangeirismos consagrados ou uma escolha narrativa deliberada.
- Mantenha palavrões, humor, informalidade, relações entre personagens e
  diferenças de tratamento; não censure nem torne a fala artificialmente
  formal.
- "context_before" e "context_after" servem apenas para coerência e NÃO devem
  aparecer no objeto "translations".
- Preserve quebras de linha quando forem úteis, mas nunca insira linha vazia
  dentro de uma entrada.
""".strip()

    user_data = {
        "task": "translate_subtitle_cues_to_brazilian_portuguese",
        "analysis": compact_analysis(analysis),
        "context_before": [
            {
                "id": cue_id,
                "text": translated.get(cue_id, cues[cue_id - 1].text),
            }
            for cue_id in before_ids
        ],
        "cues": [
            {"id": cue_id, "text": protected_texts[cue_id]}
            for cue_id in batch_ids
        ],
        "context_after": [
            {"id": cue_id, "text": cues[cue_id - 1].text}
            for cue_id in after_ids
        ],
    }

    last_error: Optional[BaseException] = None
    format_attempts = (
        2
        if len(batch_ids) > 1
        else MAX_TENTATIVAS_FORMATO + 3
    )
    for format_attempt in range(1, format_attempts + 1):
        if format_attempt > 1:
            user_data["strict_retry"] = (
                f"A tentativa anterior violou o formato: {last_error}. "
                "Retorne todos e somente os IDs pedidos e preserve "
                "literalmente todos os marcadores __SRT_FORMAT_TAG_NNN__."
            )
        LOG.info(
            "Lote %d, entradas %d-%d: tentativa de formato %d/%d.",
            batch_number,
            batch_ids[0],
            batch_ids[-1],
            format_attempt,
            format_attempts,
        )
        try:
            response = client.request_json(
                system_prompt=system_prompt,
                user_data=user_data,
                operation=(
                    f"Tradução do lote {batch_number} "
                    f"[{batch_ids[0]}-{batch_ids[-1]}]"
                ),
                max_tokens=MAX_TOKENS_TRADUCAO,
            )
            return validate_translation_response(
                response,
                batch_ids,
                source_texts,
                formatting_replacements,
            )
        except DeepSeekRequestTimeoutError:
            # Não repete o mesmo pedido grande: translate_batch o dividirá.
            raise
        except DeepSeekFormatError as error:
            last_error = error
            if format_attempt < format_attempts:
                LOG.warning(
                    "Resposta inválida no lote %d, entradas %d-%d "
                    "(%d/%d): %s O trecho será solicitado novamente.",
                    batch_number,
                    batch_ids[0],
                    batch_ids[-1],
                    format_attempt,
                    format_attempts,
                    error,
                )

    raise DeepSeekFormatError(
        f"O lote {batch_number}, entradas {batch_ids[0]}-{batch_ids[-1]}, "
        f"permaneceu inválido: {last_error}"
    )


def translate_batch(
    client: DeepSeekClient,
    *,
    batch_number: int,
    batch_ids: Sequence[int],
    cues: Sequence[Cue],
    translated: Mapping[int, str],
    analysis: Mapping[str, Any],
) -> Dict[int, str]:
    """
    Traduz um lote e o subdivide recursivamente quando a API viola o formato.

    Erros externos reais (autenticação, créditos e indisponibilidade de rede)
    não são mascarados. Respostas incompletas ou com estrutura alterada são
    recuperadas em partes progressivamente menores.
    """
    try:
        result = request_translation_chunk(
            client,
            batch_number=batch_number,
            batch_ids=batch_ids,
            cues=cues,
            translated=translated,
            analysis=analysis,
        )
        LOG.info(
            "Lote %d, entradas %d-%d: %d tradução(ões) validada(s).",
            batch_number,
            batch_ids[0],
            batch_ids[-1],
            len(result),
        )
        return result
    except DeepSeekFormatError as error:
        if len(batch_ids) == 1:
            raise DeepSeekError(
                "A API não conseguiu devolver uma tradução estruturalmente "
                f"válida para a entrada {batch_ids[0]} após todas as "
                f"tentativas: {error}"
            ) from error

        midpoint = len(batch_ids) // 2
        left_ids = batch_ids[:midpoint]
        right_ids = batch_ids[midpoint:]
        LOG.warning(
            "Recuperação automática do lote %d: entradas %d-%d serão "
            "divididas em %d-%d e %d-%d.",
            batch_number,
            batch_ids[0],
            batch_ids[-1],
            left_ids[0],
            left_ids[-1],
            right_ids[0],
            right_ids[-1],
        )
        recovered = translate_batch(
            client,
            batch_number=batch_number,
            batch_ids=left_ids,
            cues=cues,
            translated=translated,
            analysis=analysis,
        )
        context_with_left = dict(translated)
        context_with_left.update(recovered)
        recovered.update(
            translate_batch(
                client,
                batch_number=batch_number,
                batch_ids=right_ids,
                cues=cues,
                translated=context_with_left,
                analysis=analysis,
            )
        )
        return recovered


def validate_review_response(
    response: Mapping[str, Any],
    batch_ids: Sequence[int],
    current_texts: Mapping[int, str],
    formatting_replacements: Mapping[
        int, Sequence[Tuple[str, str]]
    ],
) -> Dict[int, str]:
    complete = response.get("complete")
    corrections = response.get("corrections")
    if not isinstance(complete, bool) or not isinstance(corrections, dict):
        raise DeepSeekFormatError(
            "A revisão não retornou 'complete' e 'corrections' corretamente."
        )

    allowed = {str(cue_id) for cue_id in batch_ids}
    actual = {str(key) for key in corrections.keys()}
    if not actual.issubset(allowed):
        raise DeepSeekFormatError(
            "A revisão retornou correções para IDs estranhos."
        )
    if complete and corrections:
        raise DeepSeekFormatError(
            "A revisão declarou conclusão, mas também enviou correções."
        )
    if not complete and not corrections:
        raise DeepSeekFormatError(
            "A revisão declarou problemas sem fornecer as correções."
        )

    validated: Dict[int, str] = {}
    for raw_id, value in corrections.items():
        cue_id = int(raw_id)
        if not isinstance(value, str):
            raise DeepSeekFormatError(
                f"A correção da entrada {cue_id} não é texto."
            )
        value = restore_formatting(
            value, formatting_replacements[cue_id], cue_id
        )
        value = normalize_ai_text(value)
        if not value:
            raise DeepSeekFormatError(
                f"A correção da entrada {cue_id} ficou vazia."
            )
        if formatting_signature(value) != formatting_signature(
            current_texts[cue_id]
        ):
            raise DeepSeekFormatError(
                f"A revisão alterou tags da entrada {cue_id}."
            )
        validated[cue_id] = value
    return validated


def request_review_chunk(
    client: DeepSeekClient,
    *,
    batch_number: int,
    batch_ids: Sequence[int],
    texts: Mapping[int, str],
    source_texts: Mapping[int, str],
    analysis: Mapping[str, Any],
    cycle: int,
) -> Dict[int, str]:
    protected_items = {
        cue_id: protect_formatting(texts[cue_id])
        for cue_id in batch_ids
    }
    protected_texts = {
        cue_id: protected_items[cue_id][0] for cue_id in batch_ids
    }
    formatting_replacements = {
        cue_id: protected_items[cue_id][1] for cue_id in batch_ids
    }
    system_prompt = """
Você é o auditor final de legendas em português brasileiro. Use raciocínio
cuidadoso e verifique CADA entrada individualmente, palavra por palavra, sem
concluir que o lote está correto apenas porque a maioria está em português.

Objetivo prioritário:
- comparar, no MESMO ID, "source_text" com "translated_text";
- garantir que "translated_text" seja uma tradução completa e exclusiva de
  "source_text" daquele ID, sem fragmentos omitidos, antecipados ou empurrados
  para IDs vizinhos;
- se houver deslocamento entre IDs, regenerar integralmente cada ID afetado
  usando somente o seu próprio "source_text"; nunca redistribuir uma frase
  entre entradas por conveniência de leitura;
- localizar qualquer frase, oração, expressão ou fragmento que permaneceu no
  idioma de origem ou em outro idioma e que um espectador brasileiro esperaria
  ler traduzido;
- em entradas multilíngues, preservar o trecho já em português e traduzir
  somente cada trecho estrangeiro indevido;
- corrigir traduções linguisticamente incoerentes;
- devolver o texto INTEGRAL corrigido de toda entrada afetada.

Não traduza nem "corrija":
- nomes próprios de pessoas e lugares;
- nomes de empresas, marcas, produtos e obras;
- siglas, comandos, código e termos técnicos;
- estrangeirismos consagrados no português;
- expressões deliberadamente estrangeiras exigidas pelo contexto.

Não classifique automaticamente como nome próprio ou expressão deliberada uma
frase estrangeira só para preservá-la. Em caso de dúvida entre "texto
esquecido" e "expressão deliberada", use o contexto e prefira traduzir quando
a frase tiver sentido de diálogo comum. Nomes próprios dentro de uma frase não
justificam deixar o restante da frase sem tradução.

Preserve literalmente todas as tags HTML/ASS e não crie nem remova entradas.
Marcadores como __SRT_FORMAT_TAG_000__ representam tags protegidas: preserve-os
literalmente, sem traduzir, remover, duplicar, renumerar ou modificar.
Se tudo estiver adequadamente em português, responda:
{"complete":true,"corrections":{}}
Se houver problemas, responda apenas com os IDs afetados e o texto integral
corrigido:
{"complete":false,"corrections":{"ID":"texto corrigido"}}
Retorne SOMENTE o objeto JSON.
""".strip()

    user_data = {
        "task": "verify_brazilian_portuguese_and_correct_if_needed",
        "audit_priority": (
            "Examine cada ID separadamente. Confirme fidelidade semântica "
            "entre source_text e translated_text e procure qualquer "
            "fragmento ainda fora do português brasileiro."
        ),
        "review_cycle": cycle,
        "analysis": compact_analysis(analysis),
        "cues": [
            {
                "id": cue_id,
                "source_text": source_texts[cue_id],
                "translated_text": protected_texts[cue_id],
            }
            for cue_id in batch_ids
        ],
    }

    last_error: Optional[BaseException] = None
    format_attempts = (
        2
        if len(batch_ids) > 1
        else MAX_TENTATIVAS_FORMATO + 3
    )
    for format_attempt in range(1, format_attempts + 1):
        if format_attempt > 1:
            user_data["strict_retry"] = (
                f"A resposta anterior violou o esquema: {last_error}. "
                "Use exatamente complete e corrections e preserve todos os "
                "marcadores __SRT_FORMAT_TAG_NNN__."
            )
        LOG.info(
            "Revisão %d, lote %d, entradas %d-%d: tentativa de formato %d/%d.",
            cycle,
            batch_number,
            batch_ids[0],
            batch_ids[-1],
            format_attempt,
            format_attempts,
        )
        try:
            response = client.request_json(
                system_prompt=system_prompt,
                user_data=user_data,
                operation=(
                    f"Auditoria reasoning {cycle}, lote {batch_number} "
                    f"[{batch_ids[0]}-{batch_ids[-1]}]"
                ),
                max_tokens=MAX_TOKENS_TRADUCAO,
                thinking=True,
                reasoning_effort="max",
            )
            return validate_review_response(
                response,
                batch_ids,
                texts,
                formatting_replacements,
            )
        except DeepSeekRequestTimeoutError:
            # Não repete o mesmo pedido grande: review_batch o dividirá.
            raise
        except DeepSeekFormatError as error:
            last_error = error
            if format_attempt < format_attempts:
                LOG.warning(
                    "Resposta inválida na revisão do lote %d, entradas %d-%d "
                    "(%d/%d): %s O trecho será solicitado novamente.",
                    batch_number,
                    batch_ids[0],
                    batch_ids[-1],
                    format_attempt,
                    format_attempts,
                    error,
                )

    raise DeepSeekFormatError(
        f"A revisão do lote {batch_number}, entradas "
        f"{batch_ids[0]}-{batch_ids[-1]}, permaneceu inválida: {last_error}"
    )


def review_batch(
    client: DeepSeekClient,
    *,
    batch_number: int,
    batch_ids: Sequence[int],
    texts: Mapping[int, str],
    source_texts: Mapping[int, str],
    analysis: Mapping[str, Any],
    cycle: int,
) -> Dict[int, str]:
    try:
        return request_review_chunk(
            client,
            batch_number=batch_number,
            batch_ids=batch_ids,
            texts=texts,
            source_texts=source_texts,
            analysis=analysis,
            cycle=cycle,
        )
    except DeepSeekFormatError as error:
        if len(batch_ids) == 1:
            raise DeepSeekError(
                "A API não conseguiu revisar estruturalmente a entrada "
                f"{batch_ids[0]} após todas as tentativas: {error}"
            ) from error

        midpoint = len(batch_ids) // 2
        left_ids = batch_ids[:midpoint]
        right_ids = batch_ids[midpoint:]
        LOG.warning(
            "Recuperação automática da revisão %d, lote %d: entradas %d-%d "
            "serão divididas em %d-%d e %d-%d.",
            cycle,
            batch_number,
            batch_ids[0],
            batch_ids[-1],
            left_ids[0],
            left_ids[-1],
            right_ids[0],
            right_ids[-1],
        )
        corrections = review_batch(
            client,
            batch_number=batch_number,
            batch_ids=left_ids,
            texts=texts,
            source_texts=source_texts,
            analysis=analysis,
            cycle=cycle,
        )
        corrections.update(
            review_batch(
                client,
                batch_number=batch_number,
                batch_ids=right_ids,
                texts=texts,
                source_texts=source_texts,
                analysis=analysis,
                cycle=cycle,
            )
        )
        return corrections


def translate_all(
    client: DeepSeekClient,
    cues: Sequence[Cue],
    batches: Sequence[Sequence[int]],
    analysis: Mapping[str, Any],
) -> Dict[int, str]:
    translated: Dict[int, str] = {}
    if analysis["is_portuguese"]:
        LOG.info(
            "O texto já está predominantemente em português; "
            "a tradução inicial será ignorada."
        )
        return {
            cue_id: cue.text for cue_id, cue in enumerate(cues, start=1)
        }

    total = len(batches)
    total_cues = len(cues)
    started_at = time.monotonic()
    for number, batch_ids in enumerate(batches, start=1):
        LOG.info(
            "Traduzindo lote %d/%d (entradas %d-%d)...",
            number,
            total,
            batch_ids[0],
            batch_ids[-1],
        )
        translated.update(
            translate_batch(
                client,
                batch_number=number,
                batch_ids=batch_ids,
                cues=cues,
                translated=translated,
                analysis=analysis,
            )
        )
        LOG.info(
            "Progresso da tradução: %d/%d entradas (%.1f%%), "
            "%d/%d lotes, tempo decorrido %.1f min.",
            len(translated),
            total_cues,
            len(translated) * 100 / total_cues,
            number,
            total,
            (time.monotonic() - started_at) / 60,
        )
    return translated


def final_review(
    client: DeepSeekClient,
    batches: Sequence[Sequence[int]],
    texts: Dict[int, str],
    source_texts: Mapping[int, str],
    analysis: Mapping[str, Any],
) -> Dict[int, str]:
    """
    Audita todos os lotes no primeiro ciclo e, depois de uma correção, confirma
    novamente apenas o lote alterado e seus vizinhos. Os lotes de cada ciclo
    são independentes e executados em paralelo com concorrência conservadora.

    Se a revisão continuar propondo mudanças subjetivas no último ciclo, aplica
    a última rodada validada em vez de descartar toda a tradução.
    """
    numbered_batches = list(enumerate(batches, start=1))
    active_batches = numbered_batches

    for cycle in range(1, MAX_CICLOS_DE_CORRECAO + 2):
        LOG.info(
            "Revisão final de português: ciclo %d/%d; "
            "%d lote(s) selecionado(s).",
            cycle,
            MAX_CICLOS_DE_CORRECAO + 1,
            len(active_batches),
        )
        corrections: Dict[int, str] = {}

        def audit_one(
            number: int, batch_ids: Sequence[int]
        ) -> Dict[int, str]:
            LOG.info(
                "Verificando lote %d/%d (entradas %d-%d)...",
                number,
                len(batches),
                batch_ids[0],
                batch_ids[-1],
            )
            return review_batch(
                client,
                batch_number=number,
                batch_ids=batch_ids,
                texts=texts,
                source_texts=source_texts,
                analysis=analysis,
                cycle=cycle,
            )

        workers = min(MAX_TRABALHADORES_REVISAO, len(active_batches))
        if workers <= 1:
            for number, batch_ids in active_batches:
                corrections.update(audit_one(number, batch_ids))
        else:
            LOG.info(
                "Auditoria do ciclo %d em paralelo: até %d solicitações "
                "simultâneas; tradução inicial continua sequencial.",
                cycle,
                workers,
            )
            completed_batches = 0
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="srt-review",
            ) as executor:
                futures: Dict[Future[Dict[int, str]], int] = {
                    executor.submit(audit_one, number, batch_ids): number
                    for number, batch_ids in active_batches
                }
                try:
                    for future in as_completed(futures):
                        corrections.update(future.result())
                        completed_batches += 1
                        LOG.info(
                            "Progresso da auditoria: %d/%d lote(s) "
                            "selecionado(s) concluído(s).",
                            completed_batches,
                            len(active_batches),
                        )
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise

        if not corrections:
            LOG.info(
                "DeepSeek confirmou que não há trechos pendentes de tradução."
            )
            return texts

        if cycle > MAX_CICLOS_DE_CORRECAO:
            texts.update(corrections)
            LOG.warning(
                "A revisão ainda sugeriu %d ajuste(s) no ciclo final. "
                "As correções estruturalmente válidas foram aplicadas; "
                "o arquivo seguirá para validação e gravação.",
                len(corrections),
            )
            return texts

        LOG.warning(
            "A revisão corrigiu %d entrada(s); uma nova verificação será feita.",
            len(corrections),
        )
        texts.update(corrections)

        changed_batch_positions = {
            position
            for position, (_, batch_ids) in enumerate(numbered_batches)
            if any(cue_id in corrections for cue_id in batch_ids)
        }
        selected_positions = {
            neighbor
            for position in changed_batch_positions
            for neighbor in (position - 1, position, position + 1)
            if 0 <= neighbor < len(numbered_batches)
        }
        active_batches = [
            numbered_batches[position]
            for position in sorted(selected_positions)
        ]
        LOG.info(
            "Próximo ciclo: %d/%d lote(s) serão reavaliados "
            "(lotes corrigidos e vizinhos).",
            len(active_batches),
            len(numbered_batches),
        )

    raise AssertionError("Fluxo de revisão inalcançável.")


# ---------------------------------------------------------------------------
# DESCOBERTA DE ARQUIVO, GRAVAÇÃO E PROGRAMA PRINCIPAL
# ---------------------------------------------------------------------------

def find_inputs(folder: Path) -> List[Path]:
    candidates = sorted(
        (
            item
            for item in folder.iterdir()
            if item.is_file()
            and item.suffix.casefold() == ".srt"
            and not item.stem.casefold().endswith("_traduzido")
        ),
        key=lambda path: path.name.casefold(),
    )

    if not candidates:
        raise SRTError(
            "Nenhum arquivo .srt de entrada foi encontrado na pasta atual."
        )
    return candidates


def output_path_for(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_traduzido.srt")


def audit_checkpoint_path_for(output_path: Path) -> Path:
    return output_path.with_name(
        f".{output_path.stem}.auditoria_pendente.json"
    )


def atomic_write(path: Path, content: str) -> None:
    temporary_name: Optional[str] = None
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
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    except BaseException:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def source_signature(cues: Sequence[Cue]) -> str:
    digest = hashlib.sha256()
    for cue in cues:
        digest.update(cue.timestamp.encode("utf-8"))
        digest.update(b"\0")
        digest.update(cue.text.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def write_audit_checkpoint(
    path: Path,
    *,
    input_path: Path,
    output_path: Path,
    cues: Sequence[Cue],
    analysis: Mapping[str, Any],
) -> None:
    data = {
        "version": VERSAO_CHECKPOINT_AUDITORIA,
        "stage": "audit_pending",
        "input_name": input_path.name,
        "output_name": output_path.name,
        "source_signature": source_signature(cues),
        "analysis": dict(analysis),
    }
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def load_audit_checkpoint(
    path: Path,
    *,
    input_path: Path,
    output_path: Path,
    cues: Sequence[Cue],
) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SRTError(
            f"O checkpoint de auditoria {path.name} está ilegível: {error}"
        ) from error

    if not isinstance(data, dict):
        raise SRTError(
            f"O checkpoint de auditoria {path.name} não é um objeto JSON."
        )

    expected = {
        "version": VERSAO_CHECKPOINT_AUDITORIA,
        "stage": "audit_pending",
        "input_name": input_path.name,
        "output_name": output_path.name,
        "source_signature": source_signature(cues),
    }
    mismatched = [
        key for key, value in expected.items() if data.get(key) != value
    ]
    if mismatched:
        raise SRTError(
            "O checkpoint de auditoria não corresponde ao arquivo-fonte "
            f"atual (campos: {', '.join(mismatched)})."
        )

    analysis = data.get("analysis")
    if not isinstance(analysis, dict):
        raise SRTError(
            "O checkpoint de auditoria não contém a análise de idioma."
        )
    required_strings = (
        "language_name",
        "language_code",
        "regional_variant",
        "content_summary",
        "register",
    )
    if any(not isinstance(analysis.get(key), str) for key in required_strings):
        raise SRTError(
            "A análise de idioma salva no checkpoint está incompleta."
        )
    if not isinstance(analysis.get("is_portuguese"), bool):
        raise SRTError(
            "O idioma salvo no checkpoint não possui is_portuguese válido."
        )
    for key in ("proper_names", "preserve_terms"):
        if not isinstance(analysis.get(key), list):
            raise SRTError(
                f"A análise salva no checkpoint não possui {key} válido."
            )
    return analysis


def load_output_texts_for_audit(
    output_path: Path, source_cues: Sequence[Cue]
) -> Dict[int, str]:
    content, encoding = read_text_with_fallback(output_path)
    output_cues = parse_srt(content)
    texts = {
        cue_id: cue.text
        for cue_id, cue in enumerate(output_cues, start=1)
    }
    validate_rendered_srt(content, source_cues, texts)
    LOG.info(
        "Versão a auditar recarregada de %s (%s): %d entradas validadas.",
        output_path.name,
        encoding,
        len(texts),
    )
    return texts


def log_cleaning(stats: CleaningStats) -> None:
    LOG.info(
        "Limpeza: %d entradas lidas; %d mantidas; %d removidas por ficarem "
        "sem fala.",
        stats.cues_read,
        stats.cues_kept,
        stats.cues_without_text,
    )
    LOG.info(
        "Música: %d linha(s) e %d entrada(s) musical(is) reconhecidas.",
        stats.music_lines_removed,
        stats.music_cues_removed,
    )
    if stats.nonverbal_lines_removed:
        LOG.info(
            "Descrições não verbais removidas: %d linha(s).",
            stats.nonverbal_lines_removed,
        )
    LOG.info(
        "Índices: sequência reconstruída de 1 a %d; %d índice(s) original(is) "
        "estavam ausentes ou fora da nova sequência.",
        stats.cues_kept,
        stats.indexes_rebuilt,
    )


def process_file(client: DeepSeekClient, input_path: Path) -> Path:
    started_at = time.monotonic()
    output_path = output_path_for(input_path)
    checkpoint_path = audit_checkpoint_path_for(output_path)

    content, encoding = read_text_with_fallback(input_path)
    LOG.info(
        "Arquivo lido: %.1f KiB; codificação detectada: %s.",
        len(content.encode("utf-8")) / 1024,
        encoding,
    )

    parsed = parse_srt(content)
    warn_about_timeline(parsed)
    cues, stats = clean_cues(parsed)
    log_cleaning(stats)
    if not cues:
        raise SRTError(
            "Nenhuma fala permaneceu depois da limpeza; nada será enviado "
            "à API nem gravado."
        )

    translation_batches = build_batches(cues, MAX_CARACTERES_POR_LOTE)
    review_batches = build_batches(
        cues, MAX_CARACTERES_POR_LOTE_REVISAO
    )
    source_texts = {
        cue_id: cue.text for cue_id, cue in enumerate(cues, start=1)
    }

    resume_audit = (
        checkpoint_path.exists()
        and output_path.exists()
        and not SOBRESCREVER_SAIDA
    )
    if resume_audit:
        LOG.info(
            "Checkpoint encontrado: retomando diretamente a auditoria de %s.",
            output_path.name,
        )
        analysis = load_audit_checkpoint(
            checkpoint_path,
            input_path=input_path,
            output_path=output_path,
            cues=cues,
        )
        translated = load_output_texts_for_audit(output_path, cues)
        LOG.info(
            "Plano de retomada da API: 0 lotes de tradução; "
            "auditoria em %d lote(s).",
            len(review_batches),
        )
    else:
        LOG.info(
            "Plano da API: 1 análise de idioma, %d lote(s) de tradução e "
            "revisão final em %d lote(s).",
            len(translation_batches),
            len(review_batches),
        )

        LOG.info("Identificando idioma original e contexto com %s...", MODELO)
        analysis = analyze_language_and_context(client, cues)
        LOG.info(
            "Idioma identificado: %s (%s), variante: %s.",
            analysis["language_name"],
            analysis["language_code"],
            analysis["regional_variant"],
        )

        translated = translate_all(
            client, cues, translation_batches, analysis
        )
        initial_content = render_srt(cues, translated)
        validate_rendered_srt(initial_content, cues, translated)

        # O marcador é criado primeiro. Assim, depois que o SRT inicial existir,
        # uma interrupção nunca fará essa versão ser confundida com a auditada.
        write_audit_checkpoint(
            checkpoint_path,
            input_path=input_path,
            output_path=output_path,
            cues=cues,
            analysis=analysis,
        )
        atomic_write(output_path, initial_content)
        LOG.info(
            "Tradução inicial gravada e validada: %s. "
            "A auditoria começará agora sobre esse arquivo.",
            output_path.name,
        )
        LOG.info(
            "Se o processo for interrompido durante a auditoria, a próxima "
            "execução retomará deste checkpoint."
        )
        translated = load_output_texts_for_audit(output_path, cues)

    reviewed = final_review(
        client,
        review_batches,
        translated,
        source_texts,
        analysis,
    )

    output_content = render_srt(cues, reviewed)
    validate_rendered_srt(output_content, cues, reviewed)

    LOG.info(
        "Validação estrutural concluída: %d índices sequenciais e "
        "%d timestamps preservados.",
        len(cues),
        len(cues),
    )
    atomic_write(output_path, output_content)
    checkpoint_path.unlink(missing_ok=True)
    LOG.info(
        "Auditoria concluída: %s foi substituído atomicamente pela versão "
        "auditada e corrigida.",
        output_path.name,
    )
    LOG.info(
        "Arquivo concluído em %.1f min: %s",
        (time.monotonic() - started_at) / 60,
        output_path.name,
    )
    return output_path


def run() -> Tuple[int, int, int]:
    folder = Path.cwd()
    LOG.info("Pasta de trabalho: %s", folder)
    LOG.info("Procurando arquivos SRT de entrada...")

    input_paths = find_inputs(folder)
    pending: List[Path] = []
    skipped = 0
    for input_path in input_paths:
        output_path = output_path_for(input_path)
        checkpoint_path = audit_checkpoint_path_for(output_path)
        if output_path.exists() and not SOBRESCREVER_SAIDA:
            if checkpoint_path.exists():
                pending.append(input_path)
                LOG.info(
                    "Retomada pendente: %s existe, mas ainda precisa passar "
                    "pela auditoria final.",
                    output_path.name,
                )
            else:
                skipped += 1
                LOG.info(
                    "Ignorando %s: a saída auditada %s já existe.",
                    input_path.name,
                    output_path.name,
                )
        else:
            pending.append(input_path)

    LOG.info(
        "%d entrada(s) encontrada(s): %d pendente(s) e %d já concluída(s).",
        len(input_paths),
        len(pending),
        skipped,
    )
    if not pending:
        return 0, skipped, 0

    completed = 0
    failed = 0
    client = DeepSeekClient(get_api_key())
    try:
        for number, input_path in enumerate(pending, start=1):
            LOG.info(
                "===== Arquivo %d/%d: %s =====",
                number,
                len(pending),
                input_path.name,
            )
            try:
                process_file(client, input_path)
                completed += 1
            except (OSError, SRTError, DeepSeekError) as error:
                failed += 1
                LOG.error("Falha em %s: %s", input_path.name, error)
            except Exception:
                failed += 1
                LOG.exception("Falha inesperada em %s.", input_path.name)
    finally:
        client.close()

    if client.prompt_tokens or client.completion_tokens:
        LOG.info(
            "Uso total informado pela API: %d tokens de entrada; "
            "%d tokens de saída.",
            client.prompt_tokens,
            client.completion_tokens,
        )
    return completed, skipped, failed


def main() -> int:
    setup_logging()
    LOG.info("Iniciando limpeza e tradução sequencial de arquivos SRT.")
    try:
        completed, skipped, failed = run()
    except KeyboardInterrupt:
        LOG.error(
            "Processo interrompido pelo usuário; o arquivo em andamento "
            "não foi gravado."
        )
        return 130
    except (OSError, SRTError, DeepSeekError) as error:
        LOG.error("%s", error)
        return 1
    except Exception:
        LOG.exception("Falha inesperada; saída não gravada.")
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
