"""Канонические пути vault (порт runnerPaths.ts + VAULT_PATHS из Fragilich Suite)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VaultPaths:
    root: Path
    home: Path
    daily: Path
    projects: Path
    sort: Path
    media: Path
    freakywiki: Path
    tm_tasks: Path
    tm_comments: Path
    tm_templates: Path
    ao_engine: Path
    enriched: Path
    enrichment_jsonl: Path


def resolve_paths(settings: dict) -> VaultPaths:
    root = Path(settings["vault_root"])

    def p(rel: str) -> Path:
        return root / rel

    return VaultPaths(
        root=root,
        home=p("01 Home"),
        daily=p(settings["daily_folder"]),
        projects=p("03 Projects"),
        sort=p("05 Sort"),
        media=p("06 Media"),
        freakywiki=p("04 FreakyWiki"),
        tm_tasks=p(settings["tm_tasks_folder"]),
        tm_comments=p(settings["tm_comments_folder"]),
        tm_templates=p(settings["tm_templates_root"]),
        ao_engine=p("_System/ArchiveOrganism/ao-engine"),
        enriched=p("_System/ArchiveOrganism/Enriched"),
        enrichment_jsonl=p("_System/ArchiveOrganism/Enriched/enrichment.jsonl"),
    )
