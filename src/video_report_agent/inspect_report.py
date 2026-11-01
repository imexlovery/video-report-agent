"""Deterministic report inspection. Findings are not a semantic quality score."""

import argparse
import json
import re
import shutil
import time
from pathlib import Path

from .diagram import embed_diagram
from .report_content import fill_video_description
from .report_image import render_report_image

PAGE_CHECK = r"""() => {
  const findings = [];
  const refs = [];
  const frameRefs = [];
  const label = el => ({tag: el.tagName.toLowerCase(), id: el.id,
    text: (el.textContent || '').trim().slice(0, 160)});
  for (const el of document.querySelectorAll('body *')) {
    if (el.hasAttribute('data-source-units')) {
      refs.push({...label(el), ids: el.getAttribute('data-source-units')});
    }
    if (el.hasAttribute('data-source-frames')) {
      frameRefs.push({...label(el), ids: el.getAttribute('data-source-frames')});
    }
    const box = el.getBoundingClientRect();
    const css = getComputedStyle(el);
    if (!box.width || !box.height || css.visibility === 'hidden' ||
        el.closest('details:not([open])') && !el.closest('summary')) continue;
    if (box.left < -2 || box.right > innerWidth + 2) {
      findings.push({kind:'horizontal_overflow', ...label(el),
        left:Math.round(box.left), right:Math.round(box.right)});
    }
    // These are candidates, not proof: deliberate clipping can be valid design.
    if (['hidden','clip'].includes(css.overflowY) &&
        el.scrollHeight > el.clientHeight + 3 && el.clientHeight > 0 &&
        (el.textContent || '').trim()) {
      findings.push({kind:'possible_vertical_clipping', ...label(el),
        clientHeight:el.clientHeight, scrollHeight:el.scrollHeight});
    }
    if (el.tagName === 'IMG' && (!el.complete || el.naturalWidth === 0)) {
      findings.push({kind:'broken_image', ...label(el)});
    }
  }
  // Collect text once per reading block, including SVG labels. Skip source metadata,
  // navigation and hidden text; these contain legitimate author names and repeated titles.
  const blocks = new Map();
  const ignored = 'script,style,noscript,nav,footer,.meta,.brief-meta,.video-description-text';
  const headings = [...document.querySelectorAll('h1,h2,h3')]
    .filter(el => !el.closest('nav,details,footer'));
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const parent = node.parentElement;
    if (!node.textContent.trim() || parent.closest(ignored)) continue;
    const details = parent.closest('details');
    if (details && /^(来源说明|视频简介)/.test(
        details.querySelector('summary')?.textContent.trim() || '')) continue;
    if (parent.closest('[hidden],[aria-hidden="true"]')) continue;
    const css = getComputedStyle(parent);
    if (!parent.getClientRects().length || css.visibility === 'hidden' ||
        parent.closest('details:not([open])')) continue;
    const el = parent.closest(
      'p,li,td,th,dd,dt,blockquote,figcaption,h1,h2,h3,h4,text,div,section,header,main,body');
    if (!blocks.has(el)) blocks.set(el, []);
    blocks.get(el).push(node.textContent);
  }
  const selector = el => {
    const parts = [];
    while (el && el !== document.body) {
      const tag = el.tagName.toLowerCase();
      const peers = [...el.parentElement.children].filter(p => p.tagName === el.tagName);
      parts.unshift(`${tag}:nth-of-type(${peers.indexOf(el) + 1})`);
      el = el.parentElement;
    }
    return parts.length ? 'body > ' + parts.join(' > ') : 'body';
  };
  const textBlocks = [...blocks].map(([el, parts]) => {
    let section = '题头', subsection = '';
    for (const heading of headings) {
      if (heading !== el && !(heading.compareDocumentPosition(el) &
          Node.DOCUMENT_POSITION_FOLLOWING)) break;
      if (heading.tagName === 'H3') subsection = heading.textContent.trim();
      else { section = heading.textContent.trim(); subsection = ''; }
    }
    return {tag: el.tagName.toLowerCase(), text: parts.join('').trim(),
      section, subsection, selector: selector(el),
      source_units: el.closest('[data-source-units]')?.getAttribute('data-source-units') || ''};
  });
  return {findings, refs, frameRefs, title:document.title,
    textBlocks,
    diagram_count:document.querySelectorAll('figure[data-report-diagram]').length,
    width:innerWidth, height:document.documentElement.scrollHeight};
}"""


def content_candidates(blocks: list[dict], uploader: str = "") -> list[dict]:
    """Locate surface patterns only. Attribution and information gain need source review."""
    patterns = {
        "possible_third_person_narration": re.compile(
            r"(?:讲者|视频作者)|(?:作者|UP\s*主|视频)"
            r"(?:中|里|在视频中|在这里|接着|随后|同时|还|又)?"
            r"(?:提到|指出|认为|表示|强调|介绍|解释|讲述|叙述|转述|列举|交代|引述|讲到|提出|给出|称|说)"
        ),
        "possible_meta_narration": re.compile(r"本报告|本文|本图|这份报告|报告中的|本节将"),
    }
    if 2 <= len(uploader.strip()) <= 80:
        name = re.sub(r"\s+", "", uploader)
        patterns["possible_author_as_subject"] = re.compile(re.escape(name))
    findings = []
    seen = {}
    for block in blocks:
        text = block["text"]
        # HTML formatting can split a Chinese phrase across inline tags/newlines.
        # Keep offsets into the original text so excerpts still locate the actual source.
        characters = [(index, char) for index, char in enumerate(text) if not char.isspace()]
        searchable = "".join(char for _, char in characters)
        location = {key: block.get(key, "")
                    for key in ("tag", "section", "subsection", "selector", "source_units")}
        for kind, pattern in patterns.items():
            matches = list(pattern.finditer(searchable))
            if matches:
                start = characters[matches[0].start()][0]
                findings.append({"kind": kind, "certainty": "candidate", **location,
                                 "text": text[max(0, start - 35):start + 125],
                                 "matches": sorted({m.group() for m in matches}),
                                 "hint": "结合原文判断是否多余；保留经历、引用与争议观点的"
                                         "必要归属，不机械删词。"})
        # Exact repeated sentences/blocks, ignoring whitespace and terminal punctuation only.
        # Keep numbers, negation, names and conditions; paraphrases are out of scope.
        sentences = [s for s in re.split(r"(?<=[。！？!?])", text) if s.strip()]
        units = [text, *sentences] if len(sentences) > 1 else [text]
        reported = set()
        for unit in units:
            key = re.sub(r"\s+", "", unit).strip("。！？!?；;")
            if len(key) < 24:
                continue
            previous = seen.get(key)
            if previous:
                if previous["selector"] not in reported:
                    findings.append({"kind": "possible_repeated_text", "certainty": "candidate",
                                     **location, "text": unit.strip()[:240],
                                     "previous": previous,
                                     "hint": "相同文本重复出现；核对是否有独立用途及新增条件，"
                                             "再合并，保留独有事实。"})
                    reported.add(previous["selector"])
            else:
                seen[key] = {**location, "text": unit.strip()[:240]}
    return findings


def inspect_report(run: Path, label: str = "final") -> dict:
    run = run.resolve()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", label):
        raise ValueError("Invalid inspection label")
    target = run / "inspection" / label
    target.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = run / "report.html"
    shutil.copy2(report, target / "source.html")
    shutil.copy2(report, target / "report.html")
    # Preview the same postprocessing as delivery without changing the agent's working file.
    # Metadata is deliberately read from the original run, while source.html stays raw.
    embed_diagram(target / "report.html", run)
    fill_video_description(target / "report.html", run)
    source_ids = set(re.findall(r"^\[([^ |]+) \|", (run / "transcript.md").read_text(), re.M))
    observations = run / "visual" / "observations.jsonl"
    frame_ids = ({json.loads(line)["frame_id"] for line in observations.read_text().splitlines()
                  if line.strip()} if observations.is_file() else set())
    metadata_path = run / "input.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    uploader = metadata.get("uploader")
    result = {"status": "error", "inspector_version": "1.2",
              "scope": "layout_reference_integrity_and_content_candidates",
              "preview": "delivery-filled copy; working report.html is unchanged",
              "semantic_review": "not_performed", "visual_quality": "not_scored",
              "content_review": "candidates_only; no semantic, attribution or completeness verdict",
              "source_id_count": len(source_ids), "screenshots": []}

    def collect(page, loading_errors):
        data = page.evaluate(PAGE_CHECK)
        findings = data.pop("findings")
        findings.extend(content_candidates(data.pop("textBlocks"),
                                           uploader if isinstance(uploader, str) else ""))
        refs = data.pop("refs")
        frame_refs = data.pop("frameRefs")
        for ref in refs:
            ids = [s for s in re.split(r"[\s,;]+", ref.pop("ids").strip()) if s]
            if source_ids and (not ids or set(ids) - source_ids):
                findings.append({"kind": "invalid_source_ids", **ref,
                                 "invalid_ids": sorted(set(ids) - source_ids)})
        for ref in frame_refs:
            ids = ref.pop("ids").split()
            if not ids or set(ids) - frame_ids:
                findings.append({"kind": "invalid_frame_ids", **ref,
                                 "invalid_ids": sorted(set(ids) - frame_ids)})
        findings.extend({"kind": "resource_or_script_error", "detail": item}
                        for item in loading_errors)
        result.update(data, status="checked", findings=findings,
                      reference_elements=len(refs), reference_check=(
                          "checked" if source_ids else "unavailable_no_source_ids"))
        result.update(frame_id_count=len(frame_ids), frame_reference_elements=len(frame_refs))
        if not refs:
            findings.append({"kind": "missing_source_bindings"})
        height = data["height"]
        for name, y in [
            ("top", 0), ("middle", max(0, (height - 900) // 2)),
            ("bottom", max(0, height - 900)),
        ]:
            page.evaluate("y => window.scrollTo(0, y)", y)
            path = target / f"{name}.png"
            page.screenshot(path=str(path), animations="disabled", timeout=15000)
            result["screenshots"].append(str(path.relative_to(run)))
        page.evaluate("window.scrollTo(0, 0)")

    try:
        render_report_image(run, refresh=True,
                            output_name=f"inspection/{label}/full.png", on_page=collect,
                            report_file=target / "report.html")
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    (target / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="final")
    args = parser.parse_args()
    result = inspect_report(Path.cwd(), args.label)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "checked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
