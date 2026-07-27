#!/usr/bin/env python3
"""
Remove dos arquivos os sufixos acrescentados pelos utilitários de legendas.

Exemplos:
    filme_traduzido.srt                    -> filme.srt
    filme_traduzido_limpo.srt              -> filme.srt
    filme_limpo_sincronizado.srt            -> filme.srt

Por padrão, atua somente nos arquivos da pasta de execução, sem percorrer
subpastas. Nunca sobrescreve um arquivo existente nem resolve colisões
silenciosamente.

Uso:
    python remove_suffixes.py --dry-run
    python remove_suffixes.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


SUFIXOS = (
    "_sincronizado",
    "_traduzido",
    "_translated",
    "_limpo",
    "_clean",
)

LOG = logging.getLogger("remove-suffixes")


class RenameError(RuntimeError):
    """Erro que impede renomear um arquivo com segurança."""


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def remove_known_suffixes(stem: str) -> str:
    result = stem
    while result:
        changed = False
        folded = result.casefold()
        for suffix in SUFIXOS:
            if folded.endswith(suffix.casefold()):
                result = result[: -len(suffix)].rstrip(" ._-")
                changed = True
                break
        if not changed:
            break
    return result


def target_path_for(path: Path) -> Path:
    new_stem = remove_known_suffixes(path.stem)
    if not new_stem:
        raise RenameError(
            f"O nome de {path.name} ficaria vazio após remover os sufixos."
        )
    return path.with_name(f"{new_stem}{path.suffix}")


def build_rename_plan(folder: Path) -> Tuple[List[Tuple[Path, Path]], List[str]]:
    candidates: List[Tuple[Path, Path]] = []
    errors: List[str] = []

    for source in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        if not source.is_file():
            continue
        try:
            target = target_path_for(source)
        except RenameError as error:
            errors.append(str(error))
            continue
        if target.name == source.name:
            continue
        candidates.append((source, target))

    by_target: Dict[str, List[Path]] = {}
    for source, target in candidates:
        by_target.setdefault(target.name.casefold(), []).append(source)

    plan: List[Tuple[Path, Path]] = []
    for source, target in candidates:
        conflicts = by_target[target.name.casefold()]
        if len(conflicts) > 1:
            names = ", ".join(item.name for item in conflicts)
            errors.append(
                f"Colisão: {names} produziriam o mesmo nome {target.name}."
            )
            continue
        if target.exists() and target != source:
            errors.append(
                f"Destino existente: {source.name} não pode virar {target.name}."
            )
            continue
        plan.append((source, target))

    # Evita repetir a mesma mensagem de colisão para cada origem.
    errors = list(dict.fromkeys(errors))
    return plan, errors


def execute_plan(
    plan: Sequence[Tuple[Path, Path]], *, dry_run: bool
) -> Tuple[int, int]:
    renamed = 0
    failed = 0
    for source, target in plan:
        if dry_run:
            LOG.info("[SIMULAÇÃO] %s -> %s", source.name, target.name)
            continue
        try:
            source.rename(target)
            renamed += 1
            LOG.info("%s -> %s", source.name, target.name)
        except OSError as error:
            failed += 1
            LOG.error("Falha ao renomear %s: %s", source.name, error)
    return renamed, failed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove sufixos de processamento dos arquivos da pasta atual."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="mostra as alterações sem renomear arquivos",
    )
    return parser.parse_args(argv)


def run(*, dry_run: bool = False) -> Tuple[int, int, int]:
    folder = Path.cwd()
    LOG.info("Pasta de trabalho: %s", folder)
    plan, errors = build_rename_plan(folder)

    for error in errors:
        LOG.error("%s", error)

    if not plan:
        LOG.info("Nenhum arquivo pode ou precisa ser renomeado.")
        return 0, 0, len(errors)

    LOG.info(
        "%d arquivo(s) pronto(s) para renomear; %d conflito(s).",
        len(plan),
        len(errors),
    )
    renamed, failed = execute_plan(plan, dry_run=dry_run)
    return renamed, failed, len(errors)


def main() -> int:
    setup_logging()
    args = parse_args()
    try:
        renamed, failed, conflicts = run(dry_run=args.dry_run)
    except KeyboardInterrupt:
        LOG.error("Processo interrompido pelo usuário.")
        return 130
    except OSError as error:
        LOG.error("%s", error)
        return 1

    if args.dry_run:
        LOG.info(
            "Simulação concluída: %d renomeação(ões) possível(is) e "
            "%d conflito(s).",
            len(build_rename_plan(Path.cwd())[0]),
            conflicts,
        )
    else:
        LOG.info(
            "Resumo: %d arquivo(s) renomeado(s), %d falha(s) e "
            "%d conflito(s) ignorado(s).",
            renamed,
            failed,
            conflicts,
        )
    return 1 if failed or conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
