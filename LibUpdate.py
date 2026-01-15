"""
LibUpdate - Dependency update checker for Java and Python projects.

Scans project directories for dependency files (Maven, Gradle, SBT, Mill, Ivy,
Grape, Leiningen, Buildr, pip) and generates markdown reports showing which
libraries have updates available.
"""

import argparse
import datetime as _dt
import re
import urllib.request
import urllib.error
import urllib.parse
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional, Sequence, Tuple


@dataclass
class DependencyInfo:
    """Information about a single dependency found in a project file."""
    file_path: str  # Path to the file containing this dependency
    identifier: str  # Package identifier (e.g., "org.example:library" or "requests")
    current: str  # Current version string
    ecosystem: str  # Package ecosystem: "maven" or "pypi"


@dataclass
class UpdateInfo:
    """Result of checking a dependency for available updates."""
    dep: DependencyInfo  # The dependency that was checked
    status: str  # "updatable", "uptodate", "unknown", or "error"
    latest: str = ""  # Latest available version (if updatable)
    source_url: str = ""  # URL to package page or release notes
    details: str = ""  # Additional notes (e.g., error messages)


# =============================================================================
# File Scanning Functions
# =============================================================================

def scan_for_dependency_files(scan_root: Path) -> dict:
    """
    Recursively scan a directory for all supported dependency files.

    Returns a dict with keys for each file type containing lists of Paths.
    """
    result = {"maven_poms": [], "gradle_builds": [], "version_catalogs": [], "sbt_builds": [], "mill_builds": [], "ivy_files": [], "grape_files": [],
              "leiningen_projects": [], "buildr_files": [], "requirements": [], }

    for path in scan_root.rglob("*"):
        if not path.is_file():
            continue

        name = path.name.lower()

        # Maven
        if name == "pom.xml":
            result["maven_poms"].append(path)

        # Gradle
        elif name in ("build.gradle", "build.gradle.kts"):
            result["gradle_builds"].append(path)
        elif name == "libs.versions.toml" or (name.endswith(".toml") and "gradle" in str(path).lower() and "versions" in name):
            result["version_catalogs"].append(path)

        # SBT (Scala)
        elif name == "build.sbt":
            result["sbt_builds"].append(path)

        # Mill (Scala)
        elif name == "build.sc":
            result["mill_builds"].append(path)

        # Ivy
        elif name == "ivy.xml":
            result["ivy_files"].append(path)

        # Leiningen (Clojure)
        elif name == "project.clj":
            result["leiningen_projects"].append(path)

        # Buildr (Ruby)
        elif name == "buildfile":
            result["buildr_files"].append(path)

        # Python pip
        elif name == "requirements.txt" or name.startswith("requirements") and name.endswith(".txt"):
            result["requirements"].append(path)

        # Grape - check .groovy files for @Grab annotations
        elif name.endswith(".groovy"):
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                if "@Grab" in content or "@Grapes" in content:
                    result["grape_files"].append(path)
            except Exception:
                pass

    return result


# =============================================================================
# Dependency Parsers
# =============================================================================

def parse_maven_pom(pom_path: Path) -> List[DependencyInfo]:
    """
    Parse a Maven pom.xml file and extract dependencies.

    Returns a list of DependencyInfo objects.
    """
    deps = []
    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()

        # Handle Maven namespace
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        ns_prefix = "{http://maven.apache.org/POM/4.0.0}"

        # Check if namespace is used
        if root.tag.startswith(ns_prefix):
            dep_xpath = ".//m:dependency"
            group_tag = "m:groupId"
            artifact_tag = "m:artifactId"
            version_tag = "m:version"
        else:
            dep_xpath = ".//dependency"
            group_tag = "groupId"
            artifact_tag = "artifactId"
            version_tag = "version"
            ns = {}

        # Extract properties for variable resolution
        properties = {}
        props_elem = root.find("m:properties", ns) if ns else root.find("properties")
        if props_elem is not None:
            for prop in props_elem:
                # Strip namespace from tag
                tag = prop.tag.replace(ns_prefix, "") if ns_prefix in prop.tag else prop.tag
                properties[tag] = prop.text or ""

        # Also check parent version
        parent = root.find("m:parent", ns) if ns else root.find("parent")
        if parent is not None:
            pv = parent.find("m:version", ns) if ns else parent.find("version")
            if pv is not None and pv.text:
                properties["project.parent.version"] = pv.text

        def resolve_property(value: str) -> str:
            """Resolve ${property} references."""
            if not value or "${" not in value:
                return value
            for key, val in properties.items():
                value = value.replace(f"${{{key}}}", val)
            return value

        for dep in root.findall(dep_xpath, ns) if ns else root.findall(dep_xpath):
            group_elem = dep.find(group_tag, ns) if ns else dep.find("groupId")
            artifact_elem = dep.find(artifact_tag, ns) if ns else dep.find("artifactId")
            version_elem = dep.find(version_tag, ns) if ns else dep.find("version")

            if group_elem is not None and artifact_elem is not None:
                group_id = resolve_property(group_elem.text or "")
                artifact_id = resolve_property(artifact_elem.text or "")
                version = resolve_property(version_elem.text if version_elem is not None else "")

                if group_id and artifact_id:
                    deps.append(
                        DependencyInfo(file_path=str(pom_path), identifier=f"{group_id}:{artifact_id}", current=version or "unknown", ecosystem="maven"))

    except Exception as e:
        # Return empty list on parse error
        pass

    return deps


def parse_gradle_build(gradle_path: Path) -> List[DependencyInfo]:
    """
    Parse a Gradle build file and extract dependencies.

    Supports both Groovy (build.gradle) and Kotlin (build.gradle.kts) DSL.
    """
    deps = []
    try:
        content = gradle_path.read_text(encoding="utf-8", errors="ignore")

        # Pattern for: implementation 'group:artifact:version'
        # Also matches: api, compile, testImplementation, etc.
        patterns = [  # String notation: 'group:artifact:version' or "group:artifact:version"
            r'''(?:implementation|api|compile|testImplementation|testCompile|runtimeOnly|compileOnly|annotationProcessor)\s*[\(\s]*['"]([^'"]+:[^'"]+:[^'"]+)['"]''',
            # Map notation: group: 'x', name: 'y', version: 'z'
            r'''group\s*[:=]\s*['"]([^'"]+)['"]\s*,\s*name\s*[:=]\s*['"]([^'"]+)['"]\s*,\s*version\s*[:=]\s*['"]([^'"]+)['"]''', ]

        # String notation
        for match in re.finditer(patterns[0], content, re.IGNORECASE):
            parts = match.group(1).split(":")
            if len(parts) >= 3:
                deps.append(DependencyInfo(file_path=str(gradle_path), identifier=f"{parts[0]}:{parts[1]}", current=parts[2], ecosystem="maven"))

        # Map notation
        for match in re.finditer(patterns[1], content, re.IGNORECASE):
            deps.append(DependencyInfo(file_path=str(gradle_path), identifier=f"{match.group(1)}:{match.group(2)}", current=match.group(3), ecosystem="maven"))

    except Exception:
        pass

    return deps


def parse_requirements_txt(req_path: Path) -> List[DependencyInfo]:
    """
    Parse a Python requirements.txt file and extract dependencies.

    Handles various version specifiers: ==, >=, <=, ~=, !=, etc.
    """
    deps = []
    try:
        content = req_path.read_text(encoding="utf-8", errors="ignore")

        for line in content.splitlines():
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith("#") or line.startswith("-"):
                continue

            # Skip URLs and file paths
            if line.startswith("http") or line.startswith("/") or line.startswith("."):
                continue

            # Parse package==version, package>=version, etc.
            # Also handle extras: package[extra]==version
            match = re.match(r'^([a-zA-Z0-9_-]+)(?:\[[^\]]+\])?\s*([=<>!~]+)\s*([^\s;#]+)', line)
            if match:
                package = match.group(1)
                version = match.group(3)
                deps.append(DependencyInfo(file_path=str(req_path), identifier=package, current=version, ecosystem="pypi"))
            else:
                # Package without version specifier
                match = re.match(r'^([a-zA-Z0-9_-]+)(?:\[[^\]]+\])?$', line)
                if match:
                    deps.append(DependencyInfo(file_path=str(req_path), identifier=match.group(1), current="any", ecosystem="pypi"))

    except Exception:
        pass

    return deps


def parse_sbt_build(sbt_path: Path) -> List[DependencyInfo]:
    """Parse SBT build.sbt file for dependencies."""
    deps = []
    try:
        content = sbt_path.read_text(encoding="utf-8", errors="ignore")

        # Pattern: "group" %% "artifact" % "version" or "group" % "artifact" % "version"
        pattern = r'"([^"]+)"\s*%%?\s*"([^"]+)"\s*%\s*"([^"]+)"'

        for match in re.finditer(pattern, content):
            deps.append(DependencyInfo(file_path=str(sbt_path), identifier=f"{match.group(1)}:{match.group(2)}", current=match.group(3), ecosystem="maven"))

    except Exception:
        pass

    return deps


def parse_ivy_xml(ivy_path: Path) -> List[DependencyInfo]:
    """Parse Ivy ivy.xml file for dependencies."""
    deps = []
    try:
        tree = ET.parse(ivy_path)
        root = tree.getroot()

        for dep in root.findall(".//dependency"):
            org = dep.get("org", "")
            name = dep.get("name", "")
            rev = dep.get("rev", "unknown")

            if org and name:
                deps.append(DependencyInfo(file_path=str(ivy_path), identifier=f"{org}:{name}", current=rev, ecosystem="maven"))

    except Exception:
        pass

    return deps


def parse_leiningen_project(clj_path: Path) -> List[DependencyInfo]:
    """Parse Leiningen project.clj file for dependencies."""
    deps = []
    try:
        content = clj_path.read_text(encoding="utf-8", errors="ignore")

        # Pattern: [group/artifact "version"] or [artifact "version"]
        pattern = r'\[([a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)?)\s+"([^"]+)"\]'

        for match in re.finditer(pattern, content):
            artifact = match.group(1)
            version = match.group(2)

            # Convert clojure notation to maven notation
            if "/" in artifact:
                group, name = artifact.split("/", 1)
            else:
                group = artifact
                name = artifact

            deps.append(DependencyInfo(file_path=str(clj_path), identifier=f"{group}:{name}", current=version, ecosystem="maven"))

    except Exception:
        pass

    return deps


def parse_grape_annotations(groovy_path: Path) -> List[DependencyInfo]:
    """Parse Groovy files for @Grab annotations."""
    deps = []
    try:
        content = groovy_path.read_text(encoding="utf-8", errors="ignore")

        # Pattern: @Grab('group:artifact:version') or @Grab(group='x', module='y', version='z')
        patterns = [r"@Grab\s*\(\s*['\"]([^'\"]+:[^'\"]+:[^'\"]+)['\"]\s*\)",
                    r"@Grab\s*\(\s*group\s*=\s*['\"]([^'\"]+)['\"]\s*,\s*module\s*=\s*['\"]([^'\"]+)['\"]\s*,\s*version\s*=\s*['\"]([^'\"]+)['\"]\s*\)", ]

        # Simple notation
        for match in re.finditer(patterns[0], content):
            parts = match.group(1).split(":")
            if len(parts) >= 3:
                deps.append(DependencyInfo(file_path=str(groovy_path), identifier=f"{parts[0]}:{parts[1]}", current=parts[2], ecosystem="maven"))

        # Named parameters notation
        for match in re.finditer(patterns[1], content):
            deps.append(DependencyInfo(file_path=str(groovy_path), identifier=f"{match.group(1)}:{match.group(2)}", current=match.group(3), ecosystem="maven"))

    except Exception:
        pass

    return deps


# =============================================================================
# Version Checking APIs
# =============================================================================

def check_maven_central(group_id: str, artifact_id: str) -> Tuple[Optional[str], str]:
    """
    Query Maven Central for the latest version of an artifact.

    Args:
        group_id: Maven group ID (e.g., "org.apache.commons")
        artifact_id: Maven artifact ID (e.g., "commons-lang3")

    Returns:
        Tuple of (latest_version or None, source_url)
    """
    url = f"https://search.maven.org/solrsearch/select?q=g:{urllib.parse.quote(group_id)}+AND+a:{urllib.parse.quote(artifact_id)}&rows=1&wt=json"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LibUpdate/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

            if data.get("response", {}).get("numFound", 0) > 0:
                doc = data["response"]["docs"][0]
                latest = doc.get("latestVersion") or doc.get("v")
                source_url = f"https://search.maven.org/artifact/{group_id}/{artifact_id}"
                return latest, source_url

    except Exception:
        pass

    return None, f"https://search.maven.org/artifact/{group_id}/{artifact_id}"


def check_pypi(package_name: str) -> Tuple[Optional[str], str]:
    """
    Query PyPI for the latest version of a package.

    Args:
        package_name: Python package name (e.g., "requests")

    Returns:
        Tuple of (latest_version or None, source_url)
    """
    url = f"https://pypi.org/pypi/{urllib.parse.quote(package_name)}/json"
    source_url = f"https://pypi.org/project/{package_name}/"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LibUpdate/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            latest = data.get("info", {}).get("version")
            return latest, source_url

    except Exception:
        pass

    return None, source_url


def check_dependency_updates(deps: List[DependencyInfo], verbose: bool = False) -> List[UpdateInfo]:
    """
    Check all dependencies for available updates.

    Args:
        deps: List of dependencies to check
        verbose: If True, print progress to stderr

    Returns:
        List of UpdateInfo with check results
    """
    import sys

    results = []
    total = len(deps)

    for i, dep in enumerate(deps, 1):
        if verbose:
            print(f"\rChecking dependencies... {i}/{total}", end="", file=sys.stderr)

        if dep.ecosystem == "maven":
            # Parse group:artifact
            parts = dep.identifier.split(":")
            if len(parts) >= 2:
                group_id, artifact_id = parts[0], parts[1]
                latest, source_url = check_maven_central(group_id, artifact_id)
            else:
                latest, source_url = None, ""

        elif dep.ecosystem == "pypi":
            latest, source_url = check_pypi(dep.identifier)

        else:
            latest, source_url = None, ""

        # Determine status
        if latest is None:
            status = "unknown"
            details = "Could not fetch latest version"
        elif dep.current in ("unknown", "any", ""):
            status = "unknown"
            details = "Current version not specified"
        elif compare_versions(dep.current, latest) < 0:
            status = "updatable"
            details = ""
        else:
            status = "uptodate"
            details = ""

        results.append(UpdateInfo(dep=dep, status=status, latest=latest or "", source_url=source_url, details=details))

    if verbose:
        print(file=sys.stderr)  # Newline after progress

    return results


def compare_versions(current: str, latest: str) -> int:
    """
    Compare two version strings.

    Returns:
        -1 if current < latest
         0 if current == latest
         1 if current > latest
    """

    def normalize(v: str) -> List:
        """Normalize version string to comparable parts."""
        # Remove common prefixes
        v = re.sub(r'^[vV]', '', v)

        parts = []
        for part in re.split(r'[.\-_]', v):
            # Try to convert to int, otherwise keep as string
            try:
                parts.append((0, int(part)))
            except ValueError:
                parts.append((1, part.lower()))
        return parts

    try:
        curr_parts = normalize(current)
        latest_parts = normalize(latest)

        # Pad shorter version with zeros
        max_len = max(len(curr_parts), len(latest_parts))
        curr_parts.extend([(0, 0)] * (max_len - len(curr_parts)))
        latest_parts.extend([(0, 0)] * (max_len - len(latest_parts)))

        for c, l in zip(curr_parts, latest_parts):
            if c < l:
                return -1
            elif c > l:
                return 1
        return 0

    except Exception:
        # Fall back to string comparison
        if current < latest:
            return -1
        elif current > latest:
            return 1
        return 0


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


def write_report(out_path: Path, scan_root: Path, maven_poms: Sequence[Path], gradle_builds: Sequence[Path], version_catalogs: Sequence[Path],
                 sbt_builds: Sequence[Path], mill_builds: Sequence[Path], ivy_files: Sequence[Path], grape_files: Sequence[Path],
                 leiningen_projects: Sequence[Path], buildr_files: Sequence[Path], requirements: Sequence[Path], updates: Sequence[UpdateInfo],
                 overwrite: bool = False, output_format: Literal["markdown", "text"] = "markdown", dry_run: bool = False, ) -> bool:
    """
    Generate a report of dependency updates.

    Args:
        out_path: Path where the report will be written (ignored if dry_run=True).
        scan_root: Root directory that was scanned for dependencies.
        maven_poms: List of discovered pom.xml files.
        gradle_builds: List of discovered build.gradle/build.gradle.kts files.
        version_catalogs: List of discovered Gradle version catalog files.
        sbt_builds: List of discovered build.sbt files.
        mill_builds: List of discovered build.sc files.
        ivy_files: List of discovered ivy.xml files.
        grape_files: List of discovered Groovy files with @Grab annotations.
        leiningen_projects: List of discovered project.clj files.
        buildr_files: List of discovered buildfile files.
        requirements: List of discovered requirements.txt files.
        updates: List of UpdateInfo objects with version check results.
        overwrite: If True, overwrite existing file without prompting.
                   Use --overwrite flag to set this from command line.
        output_format: "markdown" for .md format (default), "text" for plain text.
                       Use --text flag to select plain text output.
        dry_run: If True, print report to stdout instead of writing to file.
                 Use --dry-run flag to preview output without creating a file.

    Returns:
        True if report was written (or printed in dry-run mode),
        False if user declined to overwrite.
    """
    # Check if output file already exists (skip check in dry-run mode)
    if not dry_run and out_path.exists() and not overwrite:
        response = input(f"Warning: '{out_path}' already exists. Overwrite? [y/N] ")
        if response.lower() not in ('y', 'yes'):
            print("Aborted. Use --overwrite to skip this prompt.")
            return False

    # Format helpers based on output format (markdown vs plain text)
    is_md = output_format == "markdown"

    def h1(text: str) -> str:
        """Format level 1 header."""
        return f"# {text}" if is_md else f"{text}\n{'=' * len(text)}"

    def h2(text: str) -> str:
        """Format level 2 header."""
        return f"## {text}" if is_md else f"{text}\n{'-' * len(text)}"

    def h3(text: str) -> str:
        """Format level 3 header."""
        return f"### {text}" if is_md else f"[{text}]"

    def h4(text: str) -> str:
        """Format level 4 header."""
        return f"#### {text}" if is_md else text

    def code(text: str) -> str:
        """Format inline code."""
        return f"`{text}`" if is_md else text

    now = _dt.datetime.now().astimezone()

    # Categorize updates by their status for the summary section
    updatable = [u for u in updates if u.status == "updatable"]
    unknown = [u for u in updates if u.status in ("unknown", "error")]
    uptodate = [u for u in updates if u.status == "uptodate"]

    # Group discovered dependency files by project directory.
    # Each project gets a bucket containing lists of each file type found.
    files_by_project = defaultdict(
        lambda: {"maven_pom": [], "gradle": [], "gradle_catalog": [], "sbt": [], "mill": [], "ivy": [], "grape": [], "leiningen": [], "buildr": [],
                 "requirements": []})

    def add_file(kind: str, p: Path):
        """Helper to add a file to its project's bucket."""
        key = project_key_for(str(p), scan_root)
        files_by_project[key][kind].append(p)

    # Categorize all discovered files by their build tool type
    for p in maven_poms:
        add_file("maven_pom", p)
    for p in gradle_builds:
        add_file("gradle", p)
    for p in version_catalogs:
        add_file("gradle_catalog", p)
    for p in sbt_builds:
        add_file("sbt", p)
    for p in mill_builds:
        add_file("mill", p)
    for p in ivy_files:
        add_file("ivy", p)
    for p in grape_files:
        add_file("grape", p)
    for p in leiningen_projects:
        add_file("leiningen", p)
    for p in buildr_files:
        add_file("buildr", p)
    for p in requirements:
        add_file("requirements", p)

    # Group update results by project and ecosystem for organized output.
    # Structure: project_key -> ecosystem_name -> list[UpdateInfo]
    # This allows the report to show updates grouped by project first,
    # then by package ecosystem (maven/pypi) within each project.
    grouped = defaultdict(lambda: defaultdict(list))
    for u in updates:
        proj = project_key_for(u.dep.file_path, scan_root)
        grouped[proj][u.dep.ecosystem].append(u)

    def eco_label(eco: str) -> str:
        """Convert ecosystem identifier to human-readable label for report headers."""
        labels = {"maven": "Java (Maven/Gradle/SBT/Mill/Ivy/Grape/Leiningen/Buildr → Maven Central)", "pypi": "Python (pip → PyPI)"}
        return labels.get(eco, eco)

    def write_updates_block(lines: List[str], items: List[UpdateInfo]) -> None:
        """
        Write a block of update information for a single ecosystem.

        Outputs statistics followed by detailed lists of:
        - Libraries with updates available (showing current and new versions)
        - Libraries that couldn't be checked (unknown/error status)
        """
        upd = [x for x in items if x.status == "updatable"]
        unk = [x for x in items if x.status in ("unknown", "error")]
        ok = [x for x in items if x.status == "uptodate"]

        # Statistics for this ecosystem
        lines.append(f"- Dependencies checked: {len(items)}")
        lines.append(f"- Updatable: {len(upd)}")
        lines.append(f"- Up-to-date: {len(ok)}")
        lines.append(f"- Unknown/Error: {len(unk)}")
        lines.append("")

        # List all libraries that have newer versions available
        lines.append("Updates available:")
        if not upd:
            lines.append("- None")
        else:
            for u in sorted(upd, key=lambda x: (x.dep.identifier, x.dep.file_path)):
                lines.append(f"- File: {code(u.dep.file_path)}")
                lines.append(f"  - Library: {code(u.dep.identifier)}")
                lines.append(f"  - Current: {code(u.dep.current)}")
                lines.append(f"  - New: {code(u.latest)}")
                if u.source_url:
                    lines.append(f"  - Source: {u.source_url}")
                if u.details:
                    lines.append(f"  - Notes: {u.details}")
        lines.append("")

        # List libraries that couldn't be checked (not found in registry, network errors, etc.)
        lines.append("Not checked / unknown / errors:")
        if not unk:
            lines.append("- None")
        else:
            for u in sorted(unk, key=lambda x: (x.dep.identifier, x.dep.file_path)):
                lines.append(f"- File: {code(u.dep.file_path)}")
                lines.append(f"  - Library: {code(u.dep.identifier)}")
                if u.dep.current:
                    lines.append(f"  - Current: {code(u.dep.current)}")
                if u.source_url:
                    lines.append(f"  - Source: {u.source_url}")
                if u.details:
                    lines.append(f"  - Notes: {u.details}")
        lines.append("")

    # === Build the report ===
    lines: List[str] = []

    # Report header with metadata
    lines.append(h1("LibUpdate report"))
    lines.append("")
    lines.append(f"- Generated: {now.isoformat(timespec='seconds')}")
    lines.append(f"- Scan root: {scan_root.resolve()}")
    lines.append(f"- Output: {out_path.resolve()}")
    lines.append("")

    # Overall summary across all projects
    lines.append(h2("Summary (all projects)"))
    lines.append("")
    lines.append(f"- Dependencies checked: {len(updates)}")
    lines.append(f"- Updatable: {len(updatable)}")
    lines.append(f"- Up-to-date: {len(uptodate)}")
    lines.append(f"- Unknown/Error: {len(unknown)}")
    lines.append("")

    # List all discovered dependency files, organized by project
    lines.append(h2("Discovered files (by project)"))
    lines.append("")
    for proj in sorted(files_by_project.keys()):
        lines.append(h3(f"Project: {code(proj)}"))
        bucket = files_by_project[proj]

        # De-dup and sort paths
        def fmt_list(ps: List[Path]) -> List[str]:
            return [str(p) for p in sorted(set(ps), key=lambda x: str(x))]

        mp = fmt_list(bucket["maven_pom"])
        gb = fmt_list(bucket["gradle"])
        vc = fmt_list(bucket["gradle_catalog"])
        sb = fmt_list(bucket["sbt"])
        ml = fmt_list(bucket["mill"])
        iv = fmt_list(bucket["ivy"])
        gr = fmt_list(bucket["grape"])
        le = fmt_list(bucket["leiningen"])
        bu = fmt_list(bucket["buildr"])
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
        lines.append(f"- SBT build files: {len(sb)}")
        for p in sb:
            lines.append(f"  - {p}")
        lines.append(f"- Mill build files: {len(ml)}")
        for p in ml:
            lines.append(f"  - {p}")
        lines.append(f"- Ivy files: {len(iv)}")
        for p in iv:
            lines.append(f"  - {p}")
        lines.append(f"- Grape files: {len(gr)}")
        for p in gr:
            lines.append(f"  - {p}")
        lines.append(f"- Leiningen projects: {len(le)}")
        for p in le:
            lines.append(f"  - {p}")
        lines.append(f"- Buildr files: {len(bu)}")
        for p in bu:
            lines.append(f"  - {p}")
        lines.append(f"- Python requirements.txt: {len(rq)}")
        for p in rq:
            lines.append(f"  - {p}")
        lines.append("")

    # Detailed update results organized by project, then by ecosystem
    lines.append(h2("Update results (by project → ecosystem)"))
    lines.append("")
    for proj in sorted(grouped.keys()):
        lines.append(h3(f"Project: {code(proj)}"))
        ecos = grouped[proj]

        # Sort ecosystems: maven first, then pypi, then any others alphabetically
        for eco in sorted(ecos.keys(), key=lambda x: (x != "maven", x != "pypi", x)):
            lines.append(h4(eco_label(eco)))
            lines.append("")
            write_updates_block(lines, ecos[eco])

    # Output the report
    content = "\n".join(lines).rstrip() + "\n"
    if dry_run:
        # Print to stdout for preview
        print(content)
    else:
        # Write the final report to disk
        out_path.write_text(content, encoding="utf-8")
    return True


# Default output filename
DEFAULT_OUTPUT_FILE = "libupdate-report.md"


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the command-line argument parser."""
    parser = argparse.ArgumentParser(prog="libupdate", description="Scan projects for outdated dependencies and generate update reports.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""
Examples:
  libupdate .                      Scan current directory, output to libupdate-report.md
  libupdate . --file report.md     Scan current directory, output to report.md
  libupdate . --text               Output plain text instead of Markdown
  libupdate . --dry-run            Preview report without creating a file
  libupdate . --overwrite          Overwrite existing file without prompting
        """, )

    parser.add_argument("scan_path", type=Path, nargs="?", default=Path("."), help="Directory to scan for dependency files (default: current directory)", )

    parser.add_argument("--file", "-f", type=Path, default=None, metavar="PATH",
                        help=f"Output file path (default: {DEFAULT_OUTPUT_FILE}, or .txt with --text)", )

    parser.add_argument("--text", "-t", action="store_true", help="Output plain text format instead of Markdown", )

    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview report output without creating a file", )

    parser.add_argument("--overwrite", "-y", action="store_true", help="Overwrite existing output file without prompting", )

    parser.add_argument("--verbose", "-v", action="store_true", help="Show progress while checking dependencies", )

    return parser


def main() -> int:
    """
    Main entry point for the CLI.

    Returns:
        Exit code: 0 for success, 1 for failure.
    """
    parser = create_argument_parser()
    args = parser.parse_args()

    # Determine output format and default filename
    output_format: Literal["markdown", "text"] = "text" if args.text else "markdown"

    if args.file is not None:
        out_path = args.file
    else:
        # Use default filename based on format
        default_ext = ".txt" if args.text else ".md"
        out_path = Path(f"libupdate-report{default_ext}")

    scan_root = args.scan_path.resolve()

    if args.verbose:
        print(f"Scanning {scan_root} for dependency files...", file=__import__('sys').stderr)

    # Scan for dependency files
    files = scan_for_dependency_files(scan_root)

    maven_poms = files["maven_poms"]
    gradle_builds = files["gradle_builds"]
    version_catalogs = files["version_catalogs"]
    sbt_builds = files["sbt_builds"]
    mill_builds = files["mill_builds"]
    ivy_files = files["ivy_files"]
    grape_files = files["grape_files"]
    leiningen_projects = files["leiningen_projects"]
    buildr_files = files["buildr_files"]
    requirements = files["requirements"]

    if args.verbose:
        total_files = sum(len(v) for v in files.values())
        print(f"Found {total_files} dependency files", file=__import__('sys').stderr)

    # Parse all dependency files to extract dependencies
    all_deps: List[DependencyInfo] = []

    for pom in maven_poms:
        all_deps.extend(parse_maven_pom(pom))

    for gradle in gradle_builds:
        all_deps.extend(parse_gradle_build(gradle))

    for sbt in sbt_builds:
        all_deps.extend(parse_sbt_build(sbt))

    for ivy in ivy_files:
        all_deps.extend(parse_ivy_xml(ivy))

    for clj in leiningen_projects:
        all_deps.extend(parse_leiningen_project(clj))

    for groovy in grape_files:
        all_deps.extend(parse_grape_annotations(groovy))

    for req in requirements:
        all_deps.extend(parse_requirements_txt(req))

    if args.verbose:
        print(f"Found {len(all_deps)} dependencies to check", file=__import__('sys').stderr)

    # Check for updates
    updates = check_dependency_updates(all_deps, verbose=args.verbose)

    # Generate the report
    success = write_report(out_path=out_path, scan_root=scan_root, maven_poms=maven_poms, gradle_builds=gradle_builds, version_catalogs=version_catalogs,
                           sbt_builds=sbt_builds, mill_builds=mill_builds, ivy_files=ivy_files, grape_files=grape_files, leiningen_projects=leiningen_projects,
                           buildr_files=buildr_files, requirements=requirements, updates=updates, overwrite=args.overwrite, output_format=output_format,
                           dry_run=args.dry_run, )

    if success and not args.dry_run:
        print(f"Report written to: {out_path}")

    # Print summary statistics
    if success:
        total_files = sum(len(v) for v in files.values())
        updatable = sum(1 for u in updates if u.status == "updatable")
        uptodate = sum(1 for u in updates if u.status == "uptodate")
        unknown = sum(1 for u in updates if u.status in ("unknown", "error"))

        print()
        print("=" * 50)
        print("SCAN SUMMARY")
        print("=" * 50)
        print(f"  Files scanned:        {total_files}")
        print(f"  Dependencies found:   {len(all_deps)}")
        print("-" * 50)
        print(f"  Updatable:            {updatable}")
        print(f"  Up-to-date:           {uptodate}")
        print(f"  Unknown/Error:        {unknown}")
        print("=" * 50)

        if updatable > 0:
            print(f"\n{updatable} dependencies have updates available.")
        else:
            print("\nAll dependencies are up-to-date!")

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
