from collections import defaultdict

def project_key_for(file_path: str, scan_root: Path) -> str:
    """
    Defines "project" as the folder that contains the defining file,
    relative to scan_root. Root-level files become ".".
    """
    try:
        p = Path(file_path).resolve()
        rel = p.relative_to(scan_root.resolve())
        proj = rel.parent.as_posix()
        return proj if proj else "."
    except Exception:
        # Fallback: just show the parent folder string
        return str(Path(file_path).parent)


def write_report(
    out_path: Path,
    scan_root: Path,
    maven_poms: Sequence[Path],
    gradle_builds: Sequence[Path],
    version_catalogs: Sequence[Path],
    requirements: Sequence[Path],
    updates: Sequence[UpdateInfo],
) -> None:
    now = _dt.datetime.now().astimezone()

    # Summary counts
    updatable = [u for u in updates if u.status == "updatable"]
    unknown = [u for u in updates if u.status in ("unknown", "error")]
    uptodate = [u for u in updates if u.status == "uptodate"]

    # Group discovered files by project
    files_by_project = defaultdict(lambda: {"maven_pom": [], "gradle": [], "gradle_catalog": [], "requirements": []})

    def add_file(kind: str, p: Path):
        key = project_key_for(str(p), scan_root)
        files_by_project[key][kind].append(p)

    for p in maven_poms:
        add_file("maven_pom", p)
    for p in gradle_builds:
        add_file("gradle", p)
    for p in version_catalogs:
        add_file("gradle_catalog", p)
    for p in requirements:
        add_file("requirements", p)

    # Group update results by project and ecosystem
    # structure: project -> ecosystem -> list[UpdateInfo]
    grouped = defaultdict(lambda: defaultdict(list))
    for u in updates:
        proj = project_key_for(u.dep.file_path, scan_root)
        grouped[proj][u.dep.ecosystem].append(u)

    def eco_label(eco: str) -> str:
        return {"maven": "Java (Maven/Gradle → Maven Central)", "pypi": "Python (pip → PyPI)"}.get(eco, eco)

    def write_updates_block(lines: List[str], items: List[UpdateInfo]) -> None:
        upd = [x for x in items if x.status == "updatable"]
        unk = [x for x in items if x.status in ("unknown", "error")]
        ok = [x for x in items if x.status == "uptodate"]

        lines.append(f"- Dependencies checked: {len(items)}")
        lines.append(f"- Updatable: {len(upd)}")
        lines.append(f"- Up-to-date: {len(ok)}")
        lines.append(f"- Unknown/Error: {len(unk)}")
        lines.append("")

        lines.append("Updates available:")
        if not upd:
            lines.append("- None")
        else:
            for u in sorted(upd, key=lambda x: (x.dep.identifier, x.dep.file_path)):
                lines.append(f"- File: `{u.dep.file_path}`")
                lines.append(f"  - Library: `{u.dep.identifier}`")
                lines.append(f"  - Current: `{u.dep.current}`")
                lines.append(f"  - New: `{u.latest}`")
                if u.source_url:
                    lines.append(f"  - Source: {u.source_url}")
                if u.details:
                    lines.append(f"  - Notes: {u.details}")
        lines.append("")

        lines.append("Not checked / unknown / errors:")
        if not unk:
            lines.append("- None")
        else:
            for u in sorted(unk, key=lambda x: (x.dep.identifier, x.dep.file_path)):
                lines.append(f"- File: `{u.dep.file_path}`")
                lines.append(f"  - Library: `{u.dep.identifier}`")
                if u.dep.current:
                    lines.append(f"  - Current: `{u.dep.current}`")
                if u.source_url:
                    lines.append(f"  - Source: {u.source_url}")
                if u.details:
                    lines.append(f"  - Notes: {u.details}")
        lines.append("")

    lines: List[str] = []
    lines.append("# LibUpdate report")
    lines.append("")
    lines.append(f"- Generated: {now.isoformat(timespec='seconds')}")
    lines.append(f"- Scan root: {scan_root.resolve()}")
    lines.append(f"- Output: {out_path.resolve()}")
    lines.append("")

    lines.append("## Summary (all projects)")
    lines.append("")
    lines.append(f"- Dependencies checked: {len(updates)}")
    lines.append(f"- Updatable: {len(updatable)}")
    lines.append(f"- Up-to-date: {len(uptodate)}")
    lines.append(f"- Unknown/Error: {len(unknown)}")
    lines.append("")

    lines.append("## Discovered files (by project)")
    lines.append("")
    for proj in sorted(files_by_project.keys()):
        lines.append(f"### Project: `{proj}`")
        bucket = files_by_project[proj]
        # De-dup and sort paths
        def fmt_list(ps: List[Path]) -> List[str]:
            return [str(p) for p in sorted(set(ps), key=lambda x: str(x))]

        mp = fmt_list(bucket["maven_pom"])
        gb = fmt_list(bucket["gradle"])
        vc = fmt_list(bucket["gradle_catalog"])
        rq = fmt_list(bucket["requirements"])

        lines.append(f"- Maven POMs: {len(mp)}")
        for p in mp:
            lines.append(f"  - {p}")
        lines.append(f"- Gradle build files: {len(gb)}")
        for p in gb:
            lines.append(f"  - {p}")
        lines.append(f"- Gradle version catalogs: {len(vc)}")
        for p in vc:
            lines.append(f"  - {p}")
        lines.append(f"- Python requirements.txt: {len(rq)}")
        for p in rq:
            lines.append(f"  - {p}")
        lines.append("")

    lines.append("## Update results (by project → ecosystem)")
    lines.append("")
    for proj in sorted(grouped.keys()):
        lines.append(f"### Project: `{proj}`")
        ecos = grouped[proj]

        # Always show both sections if present; otherwise skip cleanly
        for eco in sorted(ecos.keys(), key=lambda x: (x != "maven", x != "pypi", x)):
            lines.append(f"#### {eco_label(eco)}")
            lines.append("")
            write_updates_block(lines, ecos[eco])

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

