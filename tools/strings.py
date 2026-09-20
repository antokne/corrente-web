#!/usr/bin/env python3
"""Extract user-visible strings from the site's HTML, and write edits back.

    python3 tools/strings.py extract      # HTML  -> strings.json
    python3 tools/strings.py apply        # strings.json -> HTML
    python3 tools/strings.py apply --dry-run

Each entry is keyed by its position in the document tree, so a key stays valid
while you edit the text. "was" records the text at extraction time: on apply,
anything whose "was" no longer matches the file is skipped and reported, so an
edit made directly in the HTML is never silently clobbered.

Strings keep their inline markup (<strong>, <a>, entities such as &rarr;) —
edit the words around it and leave the tags in place.
"""

from html.parser import HTMLParser
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "strings.json"

PAGES = [
    "index.html",
    "circuit-cost.html",
    "prediction.html",
    "about.html",
    "contact.html",
    "privacy-policy.html",
]

# Elements whose inner HTML is treated as one editable string. The outermost
# match wins, so a <p> containing an <a> is captured once, with the link inside.
TEXT_TAGS = {"title", "h1", "h2", "h3", "h4", "p", "li", "span", "a",
             "figcaption", "button", "em", "strong"}

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

SKIP_TAGS = {"script", "style"}

# Attributes worth translating: (tag, attr, predicate on the tag's attrs)
META_KEYS = ("description", "twitter:title", "twitter:description",
             "og:title", "og:description")


class Extractor(HTMLParser):
    """Records the source range of every translatable string in a document."""

    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_offsets = [0]
        for line in source.splitlines(keepends=True):
            self.line_offsets.append(self.line_offsets[-1] + len(line))
        self.stack = []          # [tag, inner_start, child_counts, collected]
        self.path = []           # ["section[2]", "div[1]", ...]
        self.root_counts = {}
        self.entries = []        # {key, where, was, start, end}
        self.skip_depth = 0

    # -- helpers ----------------------------------------------------------
    def abs_offset(self):
        line, col = self.getpos()
        return self.line_offsets[line - 1] + col

    def counts(self):
        return self.stack[-1][2] if self.stack else self.root_counts

    def inside_collected(self):
        return any(frame[3] for frame in self.stack)

    def record(self, key, where, start, end):
        # Narrow the range to the text itself so applying an edit leaves the
        # surrounding indentation and line breaks untouched.
        raw = self.source[start:end]
        text = raw.strip()
        if text:
            start += len(raw) - len(raw.lstrip())
            self.entries.append({"key": key, "where": where, "was": text,
                                 "start": start, "end": start + len(text)})

    # -- parser callbacks -------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if self.skip_depth or tag in SKIP_TAGS:
            if tag not in VOID_TAGS:
                self.skip_depth += 1
            return

        counts = self.counts()
        counts[tag] = counts.get(tag, 0) + 1
        segment = f"{tag}[{counts[tag]}]"

        if tag == "meta":
            attr = dict(attrs)
            name = attr.get("name") or attr.get("property")
            if name in META_KEYS and "content" in attr:
                value = attr["content"]
                start = self.source.index(value, self.abs_offset())
                self.record(f"{'/'.join(self.path + [segment])}@content",
                            f"meta {name}", start, start + len(value))
            return

        if tag == "img":
            attr = dict(attrs)
            if attr.get("alt"):
                value = attr["alt"]
                start = self.source.index(value, self.abs_offset())
                self.record(f"{'/'.join(self.path + [segment])}@alt",
                            "image alt text", start, start + len(value))
            return

        if tag in VOID_TAGS:
            return

        inner_start = self.abs_offset() + len(self.get_starttag_text())
        collect = tag in TEXT_TAGS and not self.inside_collected()
        self.stack.append([tag, inner_start, {}, collect])
        self.path.append(segment)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.skip_depth:
            if tag in SKIP_TAGS:
                self.skip_depth -= 1
            return
        if tag in VOID_TAGS:
            return
        # Unwind to the matching open tag (tolerates unclosed markup).
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] != tag:
                continue
            frame = self.stack[index]
            if frame[3]:
                path = "/".join(self.path[:index + 1])
                where = " > ".join(seg.split("[")[0] for seg in self.path[:index + 1][-3:])
                self.record(path, where, frame[1], self.abs_offset())
            del self.stack[index:]
            del self.path[index:]
            return


def extract_file(path):
    source = path.read_text()
    parser = Extractor(source)
    parser.feed(source)
    parser.close()
    return parser.entries


def cmd_extract():
    catalogue = {}
    total = 0
    for name in PAGES:
        path = ROOT / name
        if not path.exists():
            print(f"  skip {name} (missing)")
            continue
        page = {}
        for entry in extract_file(path):
            page[entry["key"]] = {"where": entry["where"],
                                  "text": entry["was"],
                                  "was": entry["was"]}
        catalogue[name] = page
        total += len(page)
        print(f"  {name}: {len(page)} strings")
    CATALOGUE.write_text(json.dumps(catalogue, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {total} strings to {CATALOGUE.relative_to(ROOT)}")
    print("Edit the \"text\" values, then: python3 tools/strings.py apply")


def cmd_apply(dry_run=False):
    if not CATALOGUE.exists():
        sys.exit("strings.json not found — run 'extract' first.")
    catalogue = json.loads(CATALOGUE.read_text())

    changed_total = 0
    for name, page in catalogue.items():
        path = ROOT / name
        if not path.exists():
            print(f"  skip {name} (missing)")
            continue

        source = path.read_text()
        entries = {e["key"]: e for e in extract_file(path)}
        edits, stale, missing = [], [], []

        for key, value in page.items():
            if value["text"] == value["was"]:
                continue
            entry = entries.get(key)
            if entry is None:
                missing.append(key)
            elif entry["was"] != value["was"]:
                stale.append(key)
            else:
                edits.append((entry["start"], entry["end"], value))

        # Apply back-to-front so earlier offsets stay valid.
        for start, end, value in sorted(edits, key=lambda e: e[0], reverse=True):
            source = source[:start] + value["text"] + source[end:]

        if edits and not dry_run:
            path.write_text(source)
            for key, value in page.items():
                value["was"] = value["text"]

        changed_total += len(edits)
        status = f"  {name}: {len(edits)} updated"
        if stale:
            status += f", {len(stale)} SKIPPED (HTML changed since extract)"
        if missing:
            status += f", {len(missing)} SKIPPED (key no longer in page)"
        print(status)
        for key in stale + missing:
            print(f"      {key}")

    if dry_run:
        print(f"\nDry run — {changed_total} string(s) would change.")
    else:
        CATALOGUE.write_text(json.dumps(catalogue, indent=2, ensure_ascii=False) + "\n")
        print(f"\nApplied {changed_total} string(s).")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in {"extract", "apply"}:
        sys.exit(__doc__)
    if args[0] == "extract":
        cmd_extract()
    else:
        cmd_apply(dry_run="--dry-run" in args)
