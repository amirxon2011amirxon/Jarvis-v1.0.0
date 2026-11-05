import { useState, type ReactNode } from "react";

import type {
  ResearchItem,
  ResearchReport,
  ResearchSource,
  ResearchState,
} from "../types";
import { cn } from "../lib/utils";
import { XIcon } from "./ui/Icons";

function formatDate(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function confidenceTone(value: string): "good" | "warn" | "bad" | "neutral" {
  if (value === "high" || value === "cross-referenced") return "good";
  if (value === "medium") return "warn";
  if (value === "low" || value === "conflicting") return "bad";
  if (value === "single-source") return "warn";
  return "neutral";
}

function sourceLabel(source: ResearchSource): string {
  return source.title || source.domain || source.url;
}

function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "good" | "warn" | "bad";
}) {
  return (
    <span className={cn("aze-research-badge", `aze-research-badge--${tone}`)}>
      {children}
    </span>
  );
}

function SourceLinks({
  ids,
  sources,
}: {
  ids: number[];
  sources: ResearchSource[];
}) {
  const byId = new Map(sources.map((source) => [source.id, source]));
  const cited = ids
    .map((id) => byId.get(id))
    .filter((source): source is ResearchSource => Boolean(source));
  if (cited.length === 0) return null;
  return (
    <span className="aze-research-citations">
      {cited.map((source) => (
        <a
          key={source.id}
          href={source.url}
          target="_blank"
          rel="noopener noreferrer"
          title={source.url}
        >
          [{source.id}] {source.domain}
        </a>
      ))}
    </span>
  );
}

function ResearchItemRow({
  item,
  sources,
}: {
  item: ResearchItem;
  sources: ResearchSource[];
}) {
  return (
    <li className="aze-research-item">
      <div className="aze-research-item-head">
        <span className="aze-research-item-text">{item.text}</span>
        <div className="aze-research-item-meta">
          {item.theme ? <span className="aze-research-theme">{item.theme}</span> : null}
          {item.confidence ? (
            <Badge tone={confidenceTone(item.confidence)}>
              {item.confidence.replace(/-/g, " ").toUpperCase()}
            </Badge>
          ) : null}
          {item.support_count ? (
            <span className="aze-research-support">
              {item.support_count} source{item.support_count === 1 ? "" : "s"}
            </span>
          ) : null}
          <SourceLinks ids={item.source_ids ?? []} sources={sources} />
        </div>
      </div>
      {item.contrasting_text ? (
        <p className="aze-research-contrast">
          <span>Contrasting evidence</span>
          {item.contrasting_text}
          <SourceLinks ids={item.contrast_source_ids ?? []} sources={sources} />
        </p>
      ) : null}
      {item.note ? <p className="aze-research-note">{item.note}</p> : null}
    </li>
  );
}

function ProgressView({ state }: { state: ResearchState }) {
  const progress = Math.max(0, Math.min(100, state.progress));
  const events = [...state.events].slice(-7).reverse();
  return (
    <div className="aze-research-progress">
      <div className="aze-research-progress-head">
        <div>
          <p className="aze-eyebrow">ACTIVE RESEARCH</p>
          <p className="aze-research-topic">{state.topic || "Researching…"}</p>
        </div>
        <Badge tone="accent">{state.phase || "WORKING"}</Badge>
      </div>
      <div
        className="aze-research-bar"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
        aria-label="Research progress"
      >
        <span style={{ width: `${progress}%` }} />
      </div>
      <div className="aze-research-progress-meta">
        <span>{state.message || "Working…"}</span>
        <span>{progress}%</span>
      </div>
      <div className="aze-research-counts">
        <span>{state.sources_found} candidates discovered</span>
        <span>{state.sources_read} sources read</span>
      </div>
      {events.length ? (
        <ol className="aze-research-events" aria-label="Research activity">
          {events.map((event, index) => (
            <li key={`${event.ts}:${index}`}>
              <span>{event.phase}</span>
              {event.message}
            </li>
          ))}
        </ol>
      ) : null}
      {state.warnings.length ? (
        <div className="aze-research-warnings">
          {state.warnings.slice(-4).map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ReportView({ report }: { report: ResearchReport }) {
  const sections = report.sections.filter(
    (section) => section.id !== "key-findings",
  );
  return (
    <div className="aze-research-report">
      <header className="aze-research-report-head">
        <div>
          <p className="aze-eyebrow">RESEARCH REPORT</p>
          <h2 className="aze-research-report-title">{report.topic}</h2>
          <p className="aze-research-report-date">
            {formatDate(report.generated_at)} · {report.sources.length} sources
            ·{" "}
            {report.methodology.domains
              ? String((report.methodology.domains as string[]).length)
              : new Set(report.sources.map((source) => source.domain)).size}{" "}
            domains
          </p>
        </div>
        <Badge tone={confidenceTone(report.confidence.level)}>
          {report.confidence.level.toUpperCase()} CONFIDENCE
        </Badge>
      </header>

      <p className="aze-research-confidence-reason">
        {report.confidence.reason}
      </p>

      <section className="aze-research-section">
        <h3>Summary</h3>
        {report.prose ? (
          <div className="aze-research-prose">
            {report.prose.split(/\n{2,}/).filter(Boolean).map((paragraph) => (
              <p key={paragraph}>{paragraph}</p>
            ))}
          </div>
        ) : (
          <p className="aze-research-summary">{report.summary}</p>
        )}
      </section>

      {report.key_findings.length ? (
        <section className="aze-research-section">
          <h3>Key findings</h3>
          <ul className="aze-research-items">
            {report.key_findings.map((item) => (
              <ResearchItemRow key={item.id} item={item} sources={report.sources} />
            ))}
          </ul>
        </section>
      ) : null}

      {sections.map((section) => (
        <section key={section.id} className="aze-research-section">
          <h3>{section.heading}</h3>
          {section.items.length ? (
            <ul className="aze-research-items">
              {section.items.map((item) => (
                <ResearchItemRow key={item.id} item={item} sources={report.sources} />
              ))}
            </ul>
          ) : (
            <p className="aze-research-empty-line">
              No {section.heading.toLowerCase()} were found in the readable
              sources.
            </p>
          )}
        </section>
      ))}

      {report.limitations.length ? (
        <section className="aze-research-section">
          <h3>Limitations</h3>
          <ul className="aze-research-limitations">
            {report.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="aze-research-section">
        <h3>Sources</h3>
        <ol className="aze-research-sources">
          {report.sources.map((source) => (
            <li key={source.id}>
              <div className="aze-research-source-head">
                <a
                  href={source.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={source.url}
                >
                  <span className="aze-research-source-id">[{source.id}]</span>
                  {sourceLabel(source)}
                </a>
                <Badge tone={confidenceTone(source.credibility.label)}>
                  {source.credibility.label.toUpperCase()}
                </Badge>
              </div>
              <p className="aze-research-source-meta">
                {source.domain}
                {source.published ? ` · ${source.published}` : ""}
                {` · credibility ${source.credibility.score.toFixed(1)}/10`}
              </p>
              <p className="aze-research-source-reasons">
                {source.credibility.reasons.join(" · ")}
              </p>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

interface ResearchPanelProps {
  research: ResearchState;
  onClose: () => void;
}

export function ResearchPanel({ research, onClose }: ResearchPanelProps) {
  const [copyNotice, setCopyNotice] = useState("");

  const copyReport = async () => {
    if (!research.report?.report_markdown) {
      setCopyNotice("There is no report to copy yet.");
      return;
    }
    try {
      await navigator.clipboard.writeText(research.report.report_markdown);
      setCopyNotice("Report copied to the clipboard.");
    } catch {
      setCopyNotice("The clipboard is unavailable.");
    }
  };

  let body;
  if (research.status === "idle" && !research.report) {
    body = (
      <div className="aze-research-empty">
        <p className="aze-research-empty-title">No research report yet</p>
        <p className="aze-research-empty-copy">
          Ask Jarvis to research a topic in depth. It will search multiple
          angles, read independent sources and open the complete report here.
        </p>
      </div>
    );
  } else if (research.status === "cancelled") {
    body = (
      <div className="aze-research-cancelled" role="status">
        <p className="aze-eyebrow">RESEARCH CANCELLED</p>
        <p className="aze-research-error-text">
          {research.message || "The research job was cancelled."}
        </p>
        <p className="aze-research-empty-copy">
          No report was published. Ask again when you want a fresh run.
        </p>
      </div>
    );
  } else if (research.status === "error") {
    body = (
      <div className="aze-research-error" role="alert">
        <p className="aze-eyebrow">RESEARCH FAILED</p>
        <p className="aze-research-error-text">
          {research.error || "The research job could not be completed."}
        </p>
        {research.report ? <ReportView report={research.report} /> : null}
      </div>
    );
  } else if (research.status === "running" && !research.report) {
    body = <ProgressView state={research} />;
  } else if (research.report) {
    body = <ReportView report={research.report} />;
  } else {
    body = <ProgressView state={research} />;
  }

  return (
    <div
      className="aze-bay-overlay"
      role="dialog"
      aria-modal="false"
      aria-label="Research report"
    >
      <section className="aze-bay-sheet aze-research-sheet">
        <header className="aze-bay-head">
          <div className="min-w-0">
            <p className="aze-eyebrow">J.A.R.V.I.S. // DEEP RESEARCH</p>
            <h2 className="aze-bay-title">Research</h2>
          </div>
          <div className="flex shrink-0 items-center gap-2.5">
            {research.status === "running" ? (
              <span className="aze-bay-pill aze-bay-pill--live">
                {research.sources_read} / {research.sources_found || "…"} SOURCES
              </span>
            ) : research.status === "complete" ? (
              <span className="aze-bay-pill aze-bay-pill--live">COMPLETE</span>
            ) : research.status === "cancelled" ? (
              <span className="aze-bay-pill">CANCELLED</span>
            ) : null}
            {research.report ? (
              <button
                type="button"
                className="aze-research-copy-button"
                onClick={() => void copyReport()}
                title="Copy the Markdown report"
              >
                COPY REPORT
              </button>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              aria-label="Close research report"
              title="Close research"
              className="aze-icon-button"
            >
              <XIcon className="h-4 w-4" />
            </button>
          </div>
        </header>

        {copyNotice ? (
          <p className="aze-research-copy" role="status">{copyNotice}</p>
        ) : null}

        <div className="aze-research-body">{body}</div>
      </section>
    </div>
  );
}
