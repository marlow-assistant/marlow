import { memo, type ReactElement } from "react";
import { parseMarkdown, renderTokens } from "./render";
import "./markdown.css";

/** Parses on every render with no cache. Use for the streaming bubble: a
 *  growing string never hits earlier cache entries (STR-12). */
export function Markdown({ text }: { text: string }): ReactElement {
  return <div className="md">{renderTokens(parseMarkdown(text))}</div>;
}

/** Settled messages only: memoised on text. */
export const SettledMarkdown = memo(Markdown);
