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
  return {findings, refs, frameRefs, title:document.title,
    diagram_count:document.querySelectorAll('figure[data-report-diagram]').length,
    width:innerWidth, height:document.documentElement.scrollHeight};
}"""


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
    result = {"status": "error", "inspector_version": "1.1",
              "scope": "layout_and_reference_integrity_only",
              "preview": "delivery-filled copy; working report.html is unchanged",
              "semantic_review": "not_performed", "visual_quality": "not_scored",
              "source_id_count": len(source_ids), "screenshots": []}

    def collect(page, loading_errors):
        data = page.evaluate(PAGE_CHECK)
        findings = data.pop("findings")
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
