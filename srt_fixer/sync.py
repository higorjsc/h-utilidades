#!/usr/bin/env python3
"""
Sincroniza, sequencialmente, os arquivos SRT da pasta atual com seus vídeos.

Para cada legenda de entrada, procura primeiro um vídeo com o mesmo nome-base.
Sufixos produzidos por outras etapas, como "_traduzido" e "_limpo", são
desconsiderados na associação. Se houver somente um vídeo na pasta, ele é
usado como alternativa para as legendas sem correspondência nominal.

O alinhamento é calculado diretamente contra a atividade de voz do áudio com
WebRTC VAD. Resultados de baixa qualidade são rejeitados pelo ffsubsync, e o
script informa o offset e quanto os timestamps realmente mudaram.

Cada resultado é gravado como <nome>_sincronizado.srt. Arquivos já
sincronizados e saídas existentes não são processados novamente.

Requisito:
    python -m pip install ffsubsync
    FFmpeg instalado e disponível no PATH.

Uso:
    Abra o terminal na pasta dos vídeos e das legendas e execute:
    python sync.py
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Sequence, Tuple


VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}

# Sufixos que podem ser acrescentados antes da sincronização.
DERIVED_SUFFIXES = ("_traduzido", "_limpo", "_clean", "_translated")

# Por segurança, uma saída existente não é sobrescrita silenciosamente.
SOBRESCREVER_SAIDA = False
INTERVALO_LOG_ESPERA = 30

# Força a referência acústica. O padrão "subs_then_webrtc" pode preferir uma
# faixa de legenda incorporada ao vídeo, sem conferir diretamente as vozes.
VAD_REFERENCIA = "webrtc"

LOG = logging.getLogger("srt-sync")

TIMESTAMP_START_RE = re.compile(
    r"(?m)^\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s+-->"
)


class SyncError(RuntimeError):
    """Erro que impede a sincronização segura de uma legenda."""


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def normalized_stem(path: Path) -> str:
    stem = path.stem.casefold()
    changed = True
    while changed:
        changed = False
        for suffix in DERIVED_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                changed = True
                break
    return stem


def find_subtitles(folder: Path) -> List[Path]:
    subtitles = sorted(
        (
            item
            for item in folder.iterdir()
            if item.is_file()
            and item.suffix.casefold() == ".srt"
            and not item.stem.casefold().endswith("_sincronizado")
        ),
        key=lambda path: path.name.casefold(),
    )
    if not subtitles:
        raise SyncError(
            "Nenhum arquivo .srt de entrada foi encontrado na pasta atual."
        )
    return subtitles


def find_videos(folder: Path) -> List[Path]:
    videos = sorted(
        (
            item
            for item in folder.iterdir()
            if item.is_file()
            and item.suffix.casefold() in VIDEO_EXTENSIONS
        ),
        key=lambda path: path.name.casefold(),
    )
    if not videos:
        raise SyncError(
            "Nenhum arquivo de vídeo compatível foi encontrado na pasta atual."
        )
    return videos


def find_video_for(subtitle: Path, videos: Sequence[Path]) -> Path:
    subtitle_stem = normalized_stem(subtitle)
    matches = [
        video for video in videos if normalized_stem(video) == subtitle_stem
    ]

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(video.name for video in matches)
        raise SyncError(
            f"Há mais de um vídeo correspondente a {subtitle.name}: {names}."
        )
    if len(videos) == 1:
        return videos[0]

    raise SyncError(
        f"Nenhum vídeo com o nome-base de {subtitle.name} foi encontrado."
    )


def output_path_for(subtitle: Path) -> Path:
    return subtitle.with_name(f"{subtitle.stem}_sincronizado.srt")


def subtitle_start_times(path: Path) -> List[float]:
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    starts: List[float] = []
    for match in TIMESTAMP_START_RE.finditer(text):
        hours, minutes, seconds, milliseconds = match.groups()
        starts.append(
            int(hours) * 3600
            + int(minutes) * 60
            + int(seconds)
            + int(milliseconds.ljust(3, "0")) / 1000
        )
    return starts


def log_ffsubsync_summary(stdout: str, stderr: str) -> None:
    combined = f"{stdout}\n{stderr}"
    useful_terms = (
        "score:",
        "offset seconds:",
        "framerate scale factor:",
        "alignment skipped",
        "low quality",
    )
    shown = set()
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        folded = line.casefold()
        if any(term in folded for term in useful_terms) and line not in shown:
            shown.add(line)
            LOG.info("ffsubsync | %s", line)


def log_timestamp_changes(source: Path, result: Path) -> None:
    before = subtitle_start_times(source)
    after = subtitle_start_times(result)
    if not before or len(before) != len(after):
        LOG.warning(
            "Não foi possível comparar todos os timestamps: "
            "%d entrada(s) antes e %d depois.",
            len(before),
            len(after),
        )
        return

    deltas = [new - old for old, new in zip(before, after)]
    median = statistics.median(deltas)
    minimum = min(deltas)
    maximum = max(deltas)
    LOG.info(
        "Alteração dos inícios: mediana %+.3fs; intervalo %+.3fs a %+.3fs "
        "em %d entrada(s).",
        median,
        minimum,
        maximum,
        len(deltas),
    )
    if max(abs(value) for value in deltas) < 0.050:
        LOG.warning(
            "A legenda já estava alinhada à referência acústica: "
            "nenhum início mudou 50 ms ou mais."
        )


def synchronize_subtitles(
    video_path: Path | str,
    srt_path: Path | str,
    output_path: Path | str,
) -> Path:
    """Sincroniza uma legenda com o áudio do vídeo usando ffsubsync."""
    video_path = Path(video_path)
    srt_path = Path(srt_path)
    output_path = Path(output_path)

    if not video_path.is_file():
        raise SyncError(f"Vídeo não encontrado: {video_path}.")
    if not srt_path.is_file():
        raise SyncError(f"Legenda não encontrada: {srt_path}.")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.stem}_",
            suffix=".srt",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)

        command = [
            sys.executable,
            "-c",
            (
                "import sys; from ffsubsync import main; "
                "sys.exit(main())"
            ),
            str(video_path),
            "-i",
            str(srt_path),
            "-o",
            str(temporary_path),
            "--vad",
            VAD_REFERENCIA,
            "--skip-sync-on-low-quality",
            "--min-score",
            "0",
            "--quality-max-offset-seconds",
            "60",
        ]
        started_at = time.monotonic()
        LOG.info("Iniciando análise de áudio e sincronização...")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(
                        timeout=INTERVALO_LOG_ESPERA
                    )
                    break
                except subprocess.TimeoutExpired:
                    LOG.info(
                        "ffsubsync trabalhando há %.1f min; processo ativo...",
                        (time.monotonic() - started_at) / 60,
                    )
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise

        elapsed = time.monotonic() - started_at
        log_ffsubsync_summary(stdout, stderr)
        if process.returncode != 0:
            details = (stderr or stdout).strip()
            if details:
                details = f" Detalhes: {details[-1000:]}"
            raise SyncError(
                f"O ffsubsync terminou com código {process.returncode}."
                f"{details}"
            )
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise SyncError("O ffsubsync não produziu uma legenda válida.")

        log_timestamp_changes(srt_path, temporary_path)
        os.replace(temporary_path, output_path)
        temporary_path = None
        LOG.info(
            "Sincronização concluída pelo ffsubsync em %.1f min.",
            elapsed / 60,
        )
        return output_path
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def ffsubsync_is_available() -> bool:
    return importlib.util.find_spec("ffsubsync") is not None


def run() -> Tuple[int, int, int]:
    folder = Path.cwd()
    LOG.info("Pasta de trabalho: %s", folder)

    subtitles = find_subtitles(folder)
    videos = find_videos(folder)
    pending: List[Path] = []
    skipped = 0

    for subtitle in subtitles:
        output_path = output_path_for(subtitle)
        if output_path.exists() and not SOBRESCREVER_SAIDA:
            skipped += 1
            LOG.info(
                "Ignorando %s: a saída %s já existe.",
                subtitle.name,
                output_path.name,
            )
        else:
            pending.append(subtitle)

    LOG.info(
        "%d legenda(s) encontrada(s): %d pendente(s) e %d já concluída(s).",
        len(subtitles),
        len(pending),
        skipped,
    )
    if not pending:
        return 0, skipped, 0
    if not ffsubsync_is_available():
        raise SyncError(
            "O módulo ffsubsync não está instalado. Execute: "
            "python -m pip install ffsubsync"
        )
    if shutil.which("ffmpeg") is None:
        raise SyncError(
            "O executável ffmpeg não foi encontrado. Instale o FFmpeg e "
            "certifique-se de que ele esteja disponível no PATH."
        )

    completed = 0
    failed = 0
    for number, subtitle in enumerate(pending, start=1):
        LOG.info(
            "===== Legenda %d/%d: %s =====",
            number,
            len(pending),
            subtitle.name,
        )
        try:
            video = find_video_for(subtitle, videos)
            output_path = output_path_for(subtitle)
            LOG.info("Vídeo correspondente: %s", video.name)
            synchronize_subtitles(video, subtitle, output_path)
            completed += 1
            LOG.info("Arquivo concluído: %s", output_path.name)
        except (OSError, SyncError) as error:
            failed += 1
            LOG.error("Falha em %s: %s", subtitle.name, error)
        except Exception:
            failed += 1
            LOG.exception("Falha inesperada em %s.", subtitle.name)

    return completed, skipped, failed


def main() -> int:
    setup_logging()
    LOG.info("Iniciando sincronização sequencial de arquivos SRT.")
    try:
        completed, skipped, failed = run()
    except KeyboardInterrupt:
        LOG.error(
            "Processo interrompido pelo usuário; o arquivo em andamento "
            "não foi gravado."
        )
        return 130
    except (OSError, SyncError) as error:
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
