import React from "react";
import { Tag } from "@carbon/react";

const TYPE_COLORS = {
  SectionHeaderItem: "blue",
  TextItem: "gray",
  TableItem: "teal",
  ListItem: "green",
  TitleItem: "purple",
  CaptionItem: "cyan",
};

export default function TextEntriesPanel({
  entries,
  onHoverEntry,
  loading,
}) {
  if (loading || !entries) {
    return (
      <div className="text-entries-panel">
        <h4 className="text-entries-panel__title">Text Entries</h4>
        <p className="text-entries-panel__empty">
          {loading ? "Processing document..." : "No document selected."}
        </p>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="text-entries-panel">
        <h4 className="text-entries-panel__title">Text Entries</h4>
        <p className="text-entries-panel__empty">No text entries found.</p>
      </div>
    );
  }

  return (
    <div className="text-entries-panel">
      <h4 className="text-entries-panel__title">
        Text Entries ({entries.length})
      </h4>
      <ul className="text-entries-panel__list">
        {entries.map((entry) => (
          <li
            key={entry.id}
            className="text-entries-panel__item"
            data-testid={`text-entry-${entry.id}`}
            onMouseEnter={() => onHoverEntry(entry.id)}
            onMouseLeave={() => onHoverEntry(null)}
          >
            <div className="text-entries-panel__item-header">
              <Tag
                size="sm"
                type={TYPE_COLORS[entry.type] || "gray"}
              >
                {entry.type?.replace("Item", "") || "Text"}
              </Tag>
              {entry.page > 0 && (
                <span className="text-entries-panel__page-badge">
                  p.{entry.page}
                </span>
              )}
            </div>
            <p className="text-entries-panel__item-text">{entry.text}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}
