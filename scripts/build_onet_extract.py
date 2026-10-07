"""Extract the O*NET ratings used by the DESMAP career catalog.

Downloads the O*NET database text files for one release, standardises every element across all
rated occupations, groups the elements into the 22 non-Desire DESMAP dimensions and keeps the six
Work Values (Extent) used as Desire targets. Only occupations listed in
data/onet-career-mapping.json are written, so the extract stays small and reproducible.

Includes information from the O*NET Database by the U.S. Department of Labor, Employment and
Training Administration (USDOL/ETA), used under the CC BY 4.0 license. The DESMAP team has
modified all or some of this information. USDOL/ETA has not approved, endorsed, or tested these
modifications.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://www.onetcenter.org/dl_files/database/db_{version}_text/{name}.txt"

# Source file, scale used, and element names per DESMAP dimension. Skills use whole O*NET skill
# categories (by element-id prefix); the others name individual elements.
SOURCES = {
    "Skills": ("Skills", "IM"),
    "WorkActivities": ("Work Activities", "IM"),
    "WorkStyles": ("Work Styles", "WI"),
    "WorkContext": ("Work Context", "CX"),
    "WorkValues": ("Work Values", "EX"),
}
SKILL_PREFIXES = {
    "E1": ("2.A.1.", "2.A.2."),  # Basic Skills (content and process)
    "E2": ("2.B.2.",),  # Complex Problem Solving Skills
    "E3": ("2.B.1.",),  # Social Skills
    "E4": ("2.B.3.",),  # Technical Skills
    "E5": ("2.B.4.",),  # Systems Skills
    "E6": ("2.B.5.",),  # Resource Management Skills
}
ELEMENTS = {
    "S1": [("WorkActivities", "Developing Objectives and Strategies"),
           ("WorkActivities", "Interpreting the Meaning of Information for Others"),
           ("WorkActivities", "Communicating with Supervisors, Peers, or Subordinates")],
    "S2": [("WorkActivities", "Establishing and Maintaining Interpersonal Relationships"),
           ("WorkActivities", "Developing and Building Teams"),
           ("WorkStyles", "Cooperation")],
    "S3": [("WorkStyles", "Leadership Orientation"),
           ("WorkActivities", "Selling or Influencing Others"),
           ("WorkContext", "Freedom to Make Decisions")],
    "M1": [("WorkActivities", "Analyzing Data or Information"),
           ("WorkActivities", "Processing Information")],
    "M2": [("WorkActivities", "Thinking Creatively"),
           ("WorkStyles", "Innovation")],
    "M3": [("WorkActivities", "Making Decisions and Solving Problems"),
           ("WorkStyles", "Adaptability")],
    "A1": [("WorkActivities", "Updating and Using Relevant Knowledge"),
           ("WorkStyles", "Achievement Orientation")],
    "A2": [("WorkStyles", "Initiative"),
           ("WorkStyles", "Dependability")],
    "A3": [("WorkStyles", "Intellectual Curiosity"),
           ("WorkStyles", "Tolerance for Ambiguity")],
    "A4": [("WorkStyles", "Perseverance"),
           ("WorkStyles", "Self-Confidence")],
    "P1": [("WorkContext", "Time Pressure")],
    "P2": [("WorkActivities", "Organizing, Planning, and Prioritizing Work"),
           ("WorkActivities", "Scheduling Work and Activities")],
    "P3": [("WorkContext", "Frequency of Decision Making"),
           ("WorkContext", "Impact of Decisions on Co-workers or Company Results")],
    "P4": [("WorkStyles", "Stress Tolerance"),
           ("WorkStyles", "Self-Control")],
    "P5": [("WorkContext", "Conflict Situations"),
           ("WorkContext", "Dealing With Unpleasant, Angry, or Discourteous People")],
    "P6": [("WorkContext", "Consequence of Error"),
           ("WorkContext", "Work Outcomes and Results of Other Workers"),
           ("WorkContext", "Health and Safety of Other Workers")],
}
# DESMAP Desire dimensions follow what the questionnaire items measure.
DESIRE_VALUES = {"D1": "Achievement", "D2": "Support", "D3": "Independence",
                 "D4": "Relationships", "D5": "Recognition", "D6": "Working Conditions"}


def download(version: str, cache: Path) -> dict[str, Path]:
    cache.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, (name, _) in SOURCES.items():
        path = cache / f"{version}-{key}.txt"
        if not path.exists():
            url = BASE_URL.format(version=version.replace(".", "_"), name=urllib.parse.quote(name))
            with urllib.request.urlopen(url, timeout=120) as response:
                path.write_bytes(response.read())
        paths[key] = path
    return paths


def read_ratings(path: Path, scale: str) -> dict[tuple[str, str], dict[str, float]]:
    """Return {(element id, element name): {occupation code: value}} for one scale."""
    ratings: dict[tuple[str, str], dict[str, float]] = {}
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file, delimiter="\t"):
            if row["Scale ID"] != scale or row.get("Recommend Suppress") == "Y":
                continue
            ratings.setdefault((row["Element ID"], row["Element Name"]), {})[row["O*NET-SOC Code"]] = float(row["Data Value"])
    return ratings


def zscores(values: dict[str, float]) -> dict[str, float]:
    mean = statistics.fmean(values.values())
    sd = statistics.pstdev(values.values())
    return {code: (value - mean) / sd for code, value in values.items()}


def build_extract(version: str, cache: Path) -> dict:
    mapping = json.loads((ROOT / "data/onet-career-mapping.json").read_text(encoding="utf-8"))
    fallback: dict[str, dict[str, str]] = mapping["fallback"]
    paths = download(version, cache)
    sources = {key: read_ratings(paths[key], scale) for key, (_, scale) in SOURCES.items()}

    elements: dict[str, list[tuple[str, tuple[str, str]]]] = {}
    for dimension, prefixes in SKILL_PREFIXES.items():
        elements[dimension] = [("Skills", element) for element in sources["Skills"]
                               if element[0].startswith(prefixes)]
    for dimension, items in ELEMENTS.items():
        elements[dimension] = []
        for source, name in items:
            match = [element for element in sources[source] if element[1] == name]
            if len(match) != 1:
                raise ValueError(f"O*NET element not found: {source} / {name}")
            elements[dimension].append((source, match[0]))

    # Element z-scores use every rated occupation, so weights mean "more or less important than in
    # a typical O*NET occupation" rather than raw ratings that are high for almost every job.
    element_z = {(source, element): zscores(values)
                 for dimension in elements.values() for source, element in dimension
                 for values in [sources[source][element]]}
    codes = {item["code"] for career in mapping["careers"] for item in career["onetSoc"]}

    def element_value(code: str, source: str, element: tuple[str, str], table: dict) -> float | None:
        values = table[(source, element)] if (source, element) in table else table[element]
        if code in values:
            return values[code]
        substitute = fallback.get(code, {}).get(source)
        return values.get(substitute) if substitute else None

    all_codes = set.intersection(*({code for values in sources[key].values() for code in values}
                                   for key in ("Skills", "WorkActivities", "WorkStyles", "WorkContext")))

    def group_mean(code: str, dimension: str) -> float:
        scores = [element_value(code, source, element, element_z) for source, element in elements[dimension]]
        if None in scores:
            raise ValueError(f"Missing O*NET rating for {code} in {dimension}")
        return statistics.fmean(scores)

    group_z = {dimension: zscores({code: group_mean(code, dimension) for code in all_codes | codes})
               for dimension in elements}
    occupations = {}
    for code in sorted(codes):
        values = {}
        for dimension, value_name in DESIRE_VALUES.items():
            element = next(element for element in sources["WorkValues"] if element[1] == value_name)
            value = element_value(code, "WorkValues", element, sources["WorkValues"])
            if value is None:
                raise ValueError(f"Missing O*NET work value for {code}: {value_name}")
            values[dimension] = value
        occupations[code] = {"groups": {dimension: round(group_z[dimension][code], 4) for dimension in elements},
                             "workValues": values}
    return {
        "onetVersion": version,
        "attribution": (f"Includes information from the O*NET {version} Database by the U.S. Department of Labor, "
                        "Employment and Training Administration (USDOL/ETA). Used under the CC BY 4.0 license. "
                        "The DESMAP team has modified all or some of this information. USDOL/ETA has not "
                        "approved, endorsed, or tested these modifications."),
        "method": "Element ratings are z-scored across all rated O*NET occupations, averaged per DESMAP "
                  "dimension and z-scored again. Desire targets use Work Values Extent (1-7).",
        "dimensionElements": {dimension: [f"{source}: {element[1]}" for source, element in items]
                              for dimension, items in elements.items()},
        "desireValues": DESIRE_VALUES,
        "occupations": occupations,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="30.2")
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache/onet")
    args = parser.parse_args()
    extract = build_extract(args.version, args.cache)
    (ROOT / "data/onet-extract.json").write_text(json.dumps(extract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Extracted {len(extract['occupations'])} O*NET occupations from release {args.version}.")
