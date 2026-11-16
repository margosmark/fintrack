import { Link, useParams } from "react-router-dom";
import { ArrowLeft, BookOpen, Download } from "lucide-react";
import { Card } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { getGuide } from "./helpGuides";

/** One guide page: numbered steps with screenshots. Route: /help/:slug. */
export function HelpGuide() {
  const { slug } = useParams();
  const guide = getGuide(slug);

  if (!guide) {
    return (
      <div className="mx-auto max-w-3xl p-4">
        <EmptyState
          icon={<BookOpen className="size-6" aria-hidden />}
          title="Guide not found"
          hint="That help topic does not exist."
          preview={{
            none: "a guide that does not exist has no real capture",
          }}
          action={{
            label: "Back to all guides",
            to: "/help",
            ariaLabel: "Back to all guides — Guide not found",
          }}
        />
        <p className="mt-3 text-sm">
          <Link to="/help" className="text-accent hover:underline">
            Back to all guides
          </Link>
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      <p className="text-sm">
        <Link
          to="/help"
          className="inline-flex items-center gap-1 text-muted hover:text-accent"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          All guides
        </Link>
      </p>
      <h1 className="text-xl font-semibold tracking-tight">{guide.title}</h1>
      <p className="text-sm text-muted">{guide.blurb}</p>
      <ol className="space-y-4">
        {guide.steps.map((step, i) => (
          <li key={step.heading}>
            <Card className="p-4">
              <h2 className="font-medium">
                <span className="mr-2 text-muted tabular">{i + 1}.</span>
                {step.heading}
              </h2>
              <p className="mt-1 text-sm text-muted">{step.body}</p>
              {step.links && (
                <ul className="mt-2 flex flex-wrap gap-2">
                  {step.links.map((link) => (
                    <li key={link.href}>
                      <a
                        href={link.href}
                        download
                        className="inline-flex items-center gap-1 rounded-lg border border-veil/[0.08] px-2.5 py-1.5 text-xs text-accent outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        <Download className="size-3.5" aria-hidden />
                        {link.label}
                      </a>
                    </li>
                  ))}
                </ul>
              )}
              {step.image && (
                <figure className="mt-3">
                  <img
                    src={step.image}
                    alt={step.imageAlt ?? step.heading}
                    loading="lazy"
                    className="w-full rounded-lg border border-veil/[0.08]"
                  />
                </figure>
              )}
            </Card>
          </li>
        ))}
      </ol>
      {guide.tables?.map((table) => (
        <Card key={table.caption} className="p-4">
          <h2 className="font-medium">{table.caption}</h2>
          <div role="region" aria-label={table.caption} tabIndex={0} className="mt-2 overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-veil/[0.08]">
                  {table.columns.map((column) => (
                    <th
                      key={column}
                      scope="col"
                      className="py-1.5 pr-4 font-medium text-text"
                    >
                      {column}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {table.rows.map((row) => (
                  <tr
                    key={row[0]}
                    className="border-b border-veil/[0.08] align-top last:border-b-0"
                  >
                    {row.map((cell, j) => (
                      <td
                        key={j}
                        className={`py-1.5 pr-4 ${j === 0 ? "text-text" : "text-muted"}`}
                      >
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ))}
    </div>
  );
}
